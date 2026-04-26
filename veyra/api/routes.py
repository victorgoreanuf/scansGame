"""All API routes — connect, start, stop, status, logs, profiles."""

import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from veyra.engine.account_manager import manager
from veyra.engine.loot_database import loot_db
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
        "team_pvp_running": manager.is_team_pvp_running,
        "team_pvp_stats": manager.get_team_pvp_stats(),
        "stat_running": manager.is_stat_running,
        "stat_stats": manager.get_stat_stats(),
        "quest_running": manager.is_quest_running,
        "quest_stats": manager.get_quest_stats(),
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
        "team_pvp_running": manager.is_team_pvp_running,
        "team_pvp_stats": manager.get_team_pvp_stats(),
        "stat_running": manager.is_stat_running,
        "stat_stats": manager.get_stat_stats(),
        "quest_running": manager.is_quest_running,
        "quest_stats": manager.get_quest_stats(),
        "collection_running": manager.is_collection_running,
        "collection_status": manager.get_collection_status(),
        "achievement_running": manager.is_achievement_running,
        "achievement_status": manager.get_achievement_status(),
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


# ── Team PvP (party) ─────────────────────────────────────────────────────────


@router.get("/pvp/team/status")
async def pvp_team_status():
    """Return live party info so the UI can render the Team PvP card."""
    if not manager.is_connected:
        return {"ok": False, "error": "Not connected"}
    party = await manager.fetch_party_status()
    return {
        "ok": True,
        "party": party,
        "running": manager.is_team_pvp_running,
        "stats": manager.get_team_pvp_stats(),
    }


@router.post("/pvp/team/start")
async def pvp_team_start():
    if manager.is_team_pvp_running:
        return {"ok": False, "error": "Team PvP already running"}
    if not manager.is_connected:
        return {"ok": False, "error": "Not connected"}
    party = await manager.fetch_party_status()
    if not party.get("in_party"):
        return {"ok": False, "error": "You are not in a party"}
    if not party.get("is_leader"):
        return {"ok": False, "error": "Only the party leader can start team matches"}
    if int(party.get("tokens", 0)) <= 0:
        return {"ok": False, "error": "No party tokens available"}
    ok, err = await manager.start_team_pvp()
    return {"ok": ok, "error": err} if not ok else {"ok": True}


@router.post("/pvp/team/stop")
async def pvp_team_stop():
    manager.stop_team_pvp()
    return {"ok": True}


# ── Stat Allocator ───────────────────────────────────────────────────────────


@router.post("/stats/start")
async def stats_start(request: Request):
    body = await request.json()

    # New format: goals list + default_stat
    raw_goals = body.get("goals", [])
    default_stat = body.get("default_stat", "stamina").lower()
    if default_stat not in ("attack", "defense", "stamina"):
        return {"ok": False, "error": "Invalid default stat"}

    from veyra.engine.stat_allocator import StatGoal
    goals = []
    for g in raw_goals:
        stat = g.get("stat", "").lower()
        target = int(g.get("target", 0))
        if stat not in ("attack", "defense", "stamina") or target <= 0:
            continue
        goals.append(StatGoal(stat=stat, target=target))

    if manager.is_stat_running:
        return {"ok": False, "error": "Stat allocator already running"}
    if not manager.is_connected:
        return {"ok": False, "error": "Not connected"}
    ok = await manager.start_stat_allocator(goals, default_stat)
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
        team_pvp_last_id = 0
        stat_last_id = 0
        quest_last_id = 0
        col_last_id = 0
        ach_last_id = 0
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
            team_pvp_state = manager.get_team_pvp_state()
            if team_pvp_state:
                new = [l for l in team_pvp_state.logs if l["id"] > team_pvp_last_id]
                if new:
                    team_pvp_last_id = new[-1]["id"]
                for entry in new:
                    tagged = {"id": entry["id"], "msg": f"[Team PvP] {entry['msg']}"}
                    yield f"data: {json.dumps(tagged)}\n\n"
            stat_state = manager.get_stat_state()
            if stat_state:
                new = [l for l in stat_state.logs if l["id"] > stat_last_id]
                if new:
                    stat_last_id = new[-1]["id"]
                for entry in new:
                    yield f"data: {json.dumps(entry)}\n\n"
            quest_state = manager.get_quest_state()
            if quest_state:
                new = [l for l in quest_state.logs if l["id"] > quest_last_id]
                if new:
                    quest_last_id = new[-1]["id"]
                for entry in new:
                    tagged = {"id": entry["id"], "msg": f"[Quest] {entry['msg']}"}
                    yield f"data: {json.dumps(tagged)}\n\n"
            col_state = manager.get_collection_state()
            if col_state:
                new = [l for l in col_state.logs if l["id"] > col_last_id]
                if new:
                    col_last_id = new[-1]["id"]
                for entry in new:
                    tagged = {"id": entry["id"], "msg": f"[Collection] {entry['msg']}"}
                    yield f"data: {json.dumps(tagged)}\n\n"
            ach_state = manager.get_achievement_state()
            if ach_state:
                new = [l for l in ach_state.logs if l["id"] > ach_last_id]
                if new:
                    ach_last_id = new[-1]["id"]
                for entry in new:
                    tagged = {"id": entry["id"], "msg": f"[Achievements] {entry['msg']}"}
                    yield f"data: {json.dumps(tagged)}\n\n"
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


# ── Loot Database ──────────────────────────────────────────────────────────────


@router.get("/loot")
async def loot_list():
    """List all cached monster loot tables."""
    return {"ok": True, "monsters": loot_db.list_all(), "count": loot_db.count}


@router.get("/loot/monster/{monster_name}")
async def loot_by_monster(monster_name: str):
    """Get loot table for a specific monster."""
    ml = loot_db.get_monster_loot(monster_name)
    if not ml:
        return {"ok": False, "error": f"No loot data for '{monster_name}'"}
    from dataclasses import asdict
    return {
        "ok": True,
        "monster_name": ml.monster_name,
        "items": [asdict(it) for it in ml.items],
    }


@router.get("/loot/item/{item_name}")
async def loot_find_item(item_name: str):
    """Find which monsters drop a specific item (partial match)."""
    results = loot_db.find_item(item_name)
    return {"ok": True, "results": results, "count": len(results)}


@router.post("/loot/scrape")
async def loot_scrape(request: Request):
    """Scrape loot using the server-side dev account (no user auth needed).

    Body (optional): {"monster_id": "12345"} or {"wave": 1}
    Empty body or no body = scrape all 3 waves.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}

    try:
        if "monster_id" in body:
            ml = await loot_db.scrape_monster(body["monster_id"])
            if not ml:
                return {"ok": False, "error": "Failed to scrape loot"}
            from dataclasses import asdict
            return {
                "ok": True,
                "monster_name": ml.monster_name,
                "items": [asdict(it) for it in ml.items],
            }

        if "wave" in body:
            results = await loot_db.scrape_wave(int(body["wave"]))
            return {
                "ok": True,
                "scraped": len(results),
                "monsters": [r.monster_name for r in results],
            }

        # Default: scrape all waves
        results = await loot_db.scrape_all_waves()
        return {
            "ok": True,
            "scraped": len(results),
            "monsters": [r.monster_name for r in results],
        }
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}


# ── Quest board (debug) ────────────────────────────────────────────────────

@router.get("/quest/board/raw")
async def quest_board_raw():
    """Fetch raw HTML from the quest board using dev account and save to debug_quest_html.txt."""
    from veyra.game.client import GameClient
    from veyra.config import settings

    if not settings.dev_email or not settings.dev_password:
        return {"ok": False, "error": "Set VEYRA_DEV_EMAIL and VEYRA_DEV_PASSWORD in .env"}
    try:
        client = GameClient()
        ok = await client.login(settings.dev_email, settings.dev_password)
        if not ok:
            await client.close()
            return {"ok": False, "error": "Dev account login failed"}
        html = await client.fetch_quest_board_raw()
        await client.close()
        Path("debug_quest_html.txt").write_text(html, encoding="utf-8")
        return {"ok": True, "length": len(html), "saved_to": "debug_quest_html.txt"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.get("/quest/board")
async def quest_board():
    """Fetch and parse the current quest board."""
    if not manager.is_connected:
        return {"ok": False, "error": "Not connected"}
    try:
        quests = await manager._worker.game.fetch_quest_board()
        from dataclasses import asdict
        return {
            "ok": True,
            "quests": [asdict(q) for q in quests],
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/quest/start")
async def quest_start(request: Request):
    """Start the quest runner. Optionally pass targets for fallback wave farming."""
    if manager.is_quest_running:
        return {"ok": False, "error": "Quest runner already running"}
    if manager.is_running:
        return {"ok": False, "error": "Wave farmer is running — stop it first"}
    if not manager.is_connected:
        return {"ok": False, "error": "Not connected"}

    # Parse optional farming targets for fallback mode
    targets = []
    try:
        body = await request.json()
        raw_targets = body.get("targets", [])
        targets = [TargetConfig(**t) for t in raw_targets]
    except Exception:
        pass

    ok = await manager.start_quest_runner(targets)
    return {"ok": ok}


@router.post("/quest/stop")
async def quest_stop():
    """Stop the quest runner."""
    manager.stop_quest_runner()
    return {"ok": True}


@router.get("/quest/status")
async def quest_status():
    """Get current quest runner status."""
    return {
        "ok": True,
        "running": manager.is_quest_running,
        "stats": manager.get_quest_stats(),
    }


# ── Collection Farmer ────────────────────────────────────────────────────────


@router.get("/collections/plan")
async def collections_plan():
    """Return the plannable collections (name, reward, items, best monster)."""
    from veyra.engine.collection_farmer import plannable_collections
    return {"ok": True, "collections": plannable_collections()}


@router.post("/collections/start")
async def collections_start(request: Request):
    """Start farming a collection. Body: {"collection_id": 17, "stamina": "200 Stamina"}"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    col_id = int(body.get("collection_id", 0))
    stamina = body.get("stamina", "10 Stamina")
    ok, err = await manager.start_collection(col_id, stamina)
    return {"ok": ok, "error": err} if not ok else {"ok": True}


@router.post("/collections/stop")
async def collections_stop():
    manager.stop_collection()
    return {"ok": True}


@router.get("/collections/status")
async def collections_status():
    if not manager.is_connected:
        return {"ok": False, "error": "Not connected"}
    # Return the cached state always (ok even when not running — lets the UI
    # render last-known progress after a stop).
    return {"ok": True, **manager.get_collection_status()}


@router.post("/collections/refresh")
async def collections_refresh():
    """Force an immediate poll of /collections.php for the active collection."""
    if not manager.is_connected:
        return {"ok": False, "error": "Not connected"}
    w = manager.worker
    if not w or not w.collection_state.collection_id:
        return {"ok": False, "error": "No collection selected"}
    try:
        from veyra.engine.collection_farmer import _poll_progress
        ok = await _poll_progress(w.game, w.collection_state)
        if not ok:
            return {"ok": False, "error": "Poll failed"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, **manager.get_collection_status()}


# ── Achievement Farmer ───────────────────────────────────────────────────────


@router.get("/achievements/preview")
async def achievements_preview(request: Request):
    """Scrape /achievements.php + configured event wave and report what's farmable.

    Query params: wave (default 101).
    """
    if not manager.is_connected:
        return {"ok": False, "error": "Not connected"}
    try:
        wave = int(request.query_params.get("wave", 101))
    except ValueError:
        wave = 101
    w = manager.worker
    if not w:
        return {"ok": False, "error": "No worker"}
    try:
        achievements = await w.game.fetch_achievements()
        mobs = await w.game.fetch_wave(wave)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    wave_names = sorted({m.name for m in mobs})
    wave_set = set(wave_names)
    active = [
        a for a in achievements
        if a["monster"] in wave_set and a["kills_current"] < a["kills_required"]
    ]
    return {
        "ok": True,
        "wave": wave,
        "wave_monsters": wave_names,
        "achievements": achievements,
        "active": active,
    }


@router.post("/achievements/start")
async def achievements_start(request: Request):
    """Start the achievement farmer. Body: {"wave": 101, "stamina": "10 Stamina"}."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        wave = int(body.get("wave", 101))
    except (TypeError, ValueError):
        wave = 101
    stamina = body.get("stamina", "10 Stamina")
    ok, err = await manager.start_achievements(wave, stamina)
    return {"ok": ok, "error": err} if not ok else {"ok": True}


@router.post("/achievements/stop")
async def achievements_stop():
    manager.stop_achievements()
    return {"ok": True}


@router.get("/achievements/status")
async def achievements_status():
    if not manager.is_connected:
        return {"ok": False, "error": "Not connected"}
    return {"ok": True, **manager.get_achievement_status()}


@router.post("/achievements/refresh")
async def achievements_refresh():
    """Force an immediate poll."""
    if not manager.is_connected:
        return {"ok": False, "error": "Not connected"}
    w = manager.worker
    if not w:
        return {"ok": False, "error": "No worker"}
    s = w.achievement_state
    if not s.wave:
        s.wave = 101
    try:
        from veyra.engine.achievement_farmer import _poll_achievements
        ok = await _poll_achievements(w.game, s)
        if not ok:
            return {"ok": False, "error": "Poll failed"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, **manager.get_achievement_status()}


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
