"""Background stamina top-up — reacts to manga chapters (+2 per chapter, 1000/day cap).

Runs as a concurrent task alongside the battle worker. Periodically checks if
stamina is below max; if so, POSTs reactions to chapters to gain stamina.
Stops when stamina is full or the daily farmed cap is reached.
"""

import asyncio
import logging

from veyra.game.client import GameClient
from veyra.game.endpoints import STAMINA_PER_REACTION
from veyra.engine.wave_farmer import FarmerState

logger = logging.getLogger("veyra.stamina_farmer")

# How often to re-check stamina when it's full (seconds)
CHECK_INTERVAL = 30
# Delay between reactions (each one visits a page + POSTs)
REACTION_DELAY = 2.0


async def reaction_topup_loop(
    game: GameClient,
    state: FarmerState,
) -> None:
    """
    Background loop that runs alongside battles.
    Checks if stamina is below max and daily cap isn't reached,
    then POSTs reactions to chapters to farm stamina.
    Stops when daily cap reached, chapters exhausted, or state.running is False.
    """
    # Brief startup delay so the main worker can begin first
    await asyncio.sleep(3)

    # Discover all chapters once upfront
    chapters: list[tuple[str, str]] = []
    chapter_idx = 0

    while state.running:
        # Skip if site is down — main worker handles recovery
        if game.is_site_down:
            await asyncio.sleep(CHECK_INTERVAL)
            continue

        # 1. Check current stamina + farmed today
        try:
            player = await game.fetch_player_stats(1)
            farmed, cap = await game.fetch_farmed_today()
        except Exception as e:
            logger.debug(f"Reaction topup check failed: {e}")
            await asyncio.sleep(CHECK_INTERVAL)
            continue

        # Daily cap reached — done for today
        if farmed >= cap:
            state.log(f"[Reactions] Daily cap reached ({farmed:,}/{cap:,}) — stopping")
            return

        # Already full — wait and check again later
        if player.stamina_current >= player.stamina_max:
            await asyncio.sleep(CHECK_INTERVAL)
            continue

        # 2. Discover chapters once (or re-fetch if exhausted)
        if not chapters or chapter_idx >= len(chapters):
            try:
                state.log("[Reactions] Discovering chapters...")
                chapters = await game.discover_chapters()
                chapter_idx = 0
            except Exception as e:
                state.log(f"[Reactions] Failed to discover chapters: {e}")
                await asyncio.sleep(CHECK_INTERVAL)
                continue

            if not chapters:
                state.log("[Reactions] No chapters found — stopping")
                return

            state.log(f"[Reactions] Found {len(chapters)} chapters to react to")

        # 3. React to chapters until stamina is full or cap reached
        reactions_this_batch = 0
        while state.running and chapter_idx < len(chapters):
            # Re-check stamina every 10 reactions
            if reactions_this_batch > 0 and reactions_this_batch % 10 == 0:
                try:
                    player = await game.fetch_player_stats(1)
                    farmed, cap = await game.fetch_farmed_today()
                except Exception:
                    break

                if player.stamina_current >= player.stamina_max:
                    state.log(
                        f"[Reactions] Stamina full ({player.stamina_current}/{player.stamina_max}) "
                        f"— pausing (+{reactions_this_batch * STAMINA_PER_REACTION} this batch)"
                    )
                    break

                if farmed >= cap:
                    state.log(f"[Reactions] Daily cap reached ({farmed:,}/{cap:,}) — done")
                    return

            manga_id, chapter = chapters[chapter_idx]
            chapter_idx += 1

            try:
                ok, resp_info = await game.react_to_chapter(manga_id, chapter)
                if ok:
                    reactions_this_batch += 1
                # Log first 5 reactions (pass or fail) for debugging, then every 10th
                if chapter_idx <= 5 or reactions_this_batch % 10 == 0:
                    tag = "OK" if ok else "FAIL"
                    state.log(
                        f"[Reactions] {tag} manga={manga_id} ch={chapter} -> {resp_info}"
                    )
            except Exception as e:
                state.log(f"[Reactions] ERROR manga={manga_id} ch={chapter}: {e}")

            await asyncio.sleep(REACTION_DELAY)

        # All chapters exhausted
        if chapter_idx >= len(chapters):
            state.log(
                f"[Reactions] All {len(chapters)} chapters visited "
                f"(+{reactions_this_batch * STAMINA_PER_REACTION} stamina) — done"
            )
            return

        # Stamina was full — sleep before next check
        await asyncio.sleep(CHECK_INTERVAL)
