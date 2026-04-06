"""All API routes — connect, start, stop, status, logs, profiles."""

import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from veyra.engine.account_manager import manager
from veyra.game.types import MonsterGroup, TargetConfig

router = APIRouter(prefix="/api")

# ── Profiles (file-based, backward compatible with slasher_app.py) ────────────

PROFILES_FILE = Path("profiles.json")


def _read_profiles() -> dict:
    try:
        if PROFILES_FILE.exists():
            return json.loads(PROFILES_FILE.read_text())
    except Exception:
        pass
    return {}


def _write_profiles(data: dict) -> None:
    PROFILES_FILE.write_text(json.dumps(data))


# ── Helpers ────────────────────────────────────────────────────────────────────


def _serialize_waves(waves: dict[int, list[MonsterGroup]]) -> dict[str, list]:
    waves_out: dict[str, list] = {}
    for wn, groups in waves.items():
        waves_out[str(wn)] = [
            {
                "name": g.name,
                "count": g.count,
                "ids": g.ids,
                "total_hp": g.total_hp,
                "max_hp": g.max_hp,
                "image": g.image,
                "instances": [
                    {
                        "id": m.id,
                        "current_hp": m.current_hp,
                        "your_dmg": m.your_dmg,
                        "joined": m.joined,
                    }
                    for m in g.instances
                ],
                "total_your_dmg": g.total_your_dmg,
                "avg_hp": g.avg_hp,
                "joined_count": g.joined_count,
                "new_count": g.new_count,
            }
            for g in groups
        ]
    return waves_out


COOKIE_OPTS = dict(httponly=True, samesite="lax", max_age=86400 * 30)


# ── Routes ────────────────────────────────────────────────────────────────────


@router.get("/session")
async def session(request: Request):
    """Check if the user has a valid session — returns state for UI restore."""
    token = request.cookies.get("veyra_session")
    if not token or not manager.validate_session(token):
        return {"ok": False}
    return {
        "ok": True,
        "waves": _serialize_waves(manager.get_waves()),
        "running": manager.is_running,
        "connected": manager.is_connected,
        "stats": manager.get_stats(),
        "pvp_running": manager.is_pvp_running,
        "pvp_stats": manager.get_pvp_stats(),
        "stat_running": manager.is_stat_running,
        "stat_stats": manager.get_stat_stats(),
    }


@router.post("/connect")
async def connect(request: Request):
    body = await request.json()
    email = body.get("email", "")
    password = body.get("password", "")
    if not email or not password:
        return {"ok": False, "error": "Email and password required"}

    ok, waves = await manager.connect(email, password)
    if not ok:
        return {"ok": False, "error": "Login failed"}

    token = manager.new_session()
    resp = JSONResponse({"ok": True, "waves": _serialize_waves(waves)})
    resp.set_cookie("veyra_session", token, **COOKIE_OPTS)
    return resp


@router.post("/logout")
async def logout():
    """Clear session cookie. Background tasks keep running."""
    manager.clear_session()
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("veyra_session")
    return resp


@router.post("/refresh")
async def refresh(request: Request):
    """Re-fetch wave data using existing game session."""
    token = request.cookies.get("veyra_session")
    if not token or not manager.validate_session(token):
        return {"ok": False, "error": "Not authenticated"}
    if not manager.is_connected:
        return {"ok": False, "error": "Not connected"}
    waves = await manager.refresh_waves()
    return {"ok": True, "waves": _serialize_waves(waves)}


@router.post("/start")
async def start(request: Request):
    if manager.is_running:
        return {"ok": False, "error": "Already running"}
    if not manager.is_connected:
        return {"ok": False, "error": "Not connected"}

    body = await request.json()
    raw_targets = body.get("targets", [])
    if not raw_targets:
        return {"ok": False, "error": "No targets configured"}

    targets = [
        TargetConfig(
            name=t["name"],
            wave=int(t.get("wave", 1)),
            damage_goal=int(t.get("damage_goal", 0)),
            stamina=t.get("stamina", "10 Stamina"),
            priority=int(t.get("priority", 1)),
            ids=t.get("ids", []),
        )
        for t in raw_targets
    ]

    ok = await manager.start(targets)
    return {"ok": ok}


@router.post("/stop")
async def stop():
    manager.stop()
    return {"ok": True}


@router.get("/status")
async def status():
    return {
        "running": manager.is_running,
        "connected": manager.is_connected,
        "stats": manager.get_stats(),
        "pvp_running": manager.is_pvp_running,
        "pvp_stats": manager.get_pvp_stats(),
        "stat_running": manager.is_stat_running,
        "stat_stats": manager.get_stat_stats(),
    }


@router.post("/pvp/start")
async def pvp_start():
    if manager.is_pvp_running:
        return {"ok": False, "error": "PvP already running"}
    if not manager.is_connected:
        return {"ok": False, "error": "Not connected"}
    ok = await manager.start_pvp()
    return {"ok": ok}


@router.post("/pvp/stop")
async def pvp_stop():
    manager.stop_pvp()
    return {"ok": True}


# ── Stat Allocator ───────────────────────────────────────────────────────────


@router.post("/stats/start")
async def stats_start(request: Request):
    body = await request.json()
    target = body.get("target", "").lower()
    if target not in ("attack", "defense", "stamina"):
        return {"ok": False, "error": "Invalid stat target"}
    if manager.is_stat_running:
        return {"ok": False, "error": "Stat allocator already running"}
    if not manager.is_connected:
        return {"ok": False, "error": "Not connected"}
    ok = await manager.start_stat_allocator(target)
    return {"ok": ok}


@router.post("/stats/stop")
async def stats_stop():
    manager.stop_stat_allocator()
    return {"ok": True}


@router.get("/logs")
async def logs():
    """Server-Sent Events stream for real-time logs."""

    async def generate():
        last_id = 0
        pvp_last_id = 0
        stat_last_id = 0
        while True:
            state = manager.get_state()
            if state:
                new = [l for l in state.logs if l["id"] > last_id]
                if new:
                    last_id = new[-1]["id"]
                for entry in new:
                    yield f"data: {json.dumps(entry)}\n\n"
            pvp_state = manager.get_pvp_state()
            if pvp_state:
                new = [l for l in pvp_state.logs if l["id"] > pvp_last_id]
                if new:
                    pvp_last_id = new[-1]["id"]
                for entry in new:
                    tagged = {"id": entry["id"], "msg": f"[PvP] {entry['msg']}"}
                    yield f"data: {json.dumps(tagged)}\n\n"
            stat_state = manager.get_stat_state()
            if stat_state:
                new = [l for l in stat_state.logs if l["id"] > stat_last_id]
                if new:
                    stat_last_id = new[-1]["id"]
                for entry in new:
                    yield f"data: {json.dumps(entry)}\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/profiles")
async def get_profiles():
    return _read_profiles()


@router.post("/profiles")
async def save_profile(request: Request):
    body = await request.json()
    name = body.get("name")
    profile = body.get("profile")
    if not name or not profile:
        return {"ok": False, "error": "Invalid data"}
    profiles = _read_profiles()
    profiles[name] = profile
    _write_profiles(profiles)
    return {"ok": True}


@router.delete("/profiles")
async def delete_profile(request: Request):
    body = await request.json()
    name = body.get("name")
    if not name:
        return {"ok": False}
    profiles = _read_profiles()
    profiles.pop(name, None)
    _write_profiles(profiles)
    return {"ok": True}
