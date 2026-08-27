#!/usr/bin/env python3
"""
Provider-agnostic fantasy draft analysis.

Everything here is independent of which fantasy platform the data came from:
the value score, the roster-need penalties, snake-draft slot arithmetic, the
Kestra output plumbing, and the disk cache.

A provider module (Sleeper, Yahoo, ...) is responsible only for talking to its
API and normalizing the result into the shapes below; it then composes these
helpers to produce the analysis payload.

  roster_spec   What a full roster looks like, normalized away from any one
                platform's settings format:
                    {
                      "dedicated":     {"QB": 1, "RB": 2, ...},
                      "flex_slots":    2,
                      "flex_eligible": {"RB", "WR", "TE"},
                      "bench_slots":   5,
                    }

  roster        My drafted players: {"QB": ["Josh Allen"], "RB": [...], ...}

  player        An available player:
                    {
                      "id": ..., "name": ..., "position": ...,
                      "nfl_team": ..., "expected_pick": 34.7,
                    }
                `expected_pick` is where the player was expected to go. Its
                units are overall pick numbers, so it is directly comparable
                to the current pick.
"""

import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from typing import Callable, Dict, Iterable, List, Optional, Set

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Positions used by standard fantasy football rosters. Everything else a
# platform lists (offensive line, IDP, ...) is noise for a redraft league.
FANTASY_POSITIONS = frozenset({"QB", "RB", "WR", "TE", "K", "DEF"})

REQUEST_TIMEOUT_SECONDS = 30

CACHE_VERSION = 3


class DraftAnalysisError(RuntimeError):
    """Raised when the provider cannot supply what an analysis needs."""


# ----------------------------------------------------------------------
# Logging / Kestra plumbing
# ----------------------------------------------------------------------


def log(message: str) -> None:
    """Human-readable progress.

    Deliberately stdout, not stderr: Kestra logs a script's stderr at ERROR
    level, which would paint routine progress lines red in the UI. Kestra only
    treats stdout lines wrapped in ``::...::`` as directives and logs the rest
    at INFO, so plain text is safe as long as it never begins with ``::``.
    """
    print(message, flush=True)


def emit_kestra(payload: Dict) -> None:
    """Emit a Kestra script-output/metric directive on stdout."""
    print("::" + json.dumps(payload) + "::", flush=True)


def build_session() -> requests.Session:
    """A session that retries transient failures instead of dying on them.

    A draft poll runs every few seconds during a live draft; one 502 from the
    provider should not take the whole run down.
    """
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


# ----------------------------------------------------------------------
# Environment helpers
# ----------------------------------------------------------------------


def env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        log(f"{name}='{raw}' is not a number; using {default}")
        return default


def env_int(name: str, default: int) -> int:
    return int(env_float(name, float(default)))


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw not in ("false", "0", "no")


def env_positions(name: str) -> Set[str]:
    return {
        part.strip().upper()
        for part in os.getenv(name, "").split(",")
        if part.strip()
    }


# ----------------------------------------------------------------------
# Disk cache
# ----------------------------------------------------------------------


def read_cache(cache_path: str, max_age_seconds: int) -> Optional[Dict]:
    """Load a cached payload, ignoring anything stale, corrupt or foreign.

    The age is stored inside the payload rather than read from the file mtime,
    so copying or restoring a cache cannot silently make stale data look fresh.
    """
    if not cache_path or not os.path.exists(cache_path):
        return None

    try:
        with open(cache_path, "r") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        log("Cache is unreadable or corrupt; refetching")
        return None

    if not isinstance(payload, dict) or payload.get("version") != CACHE_VERSION:
        return None

    fetched_at = payload.get("fetched_at")
    if not isinstance(fetched_at, (int, float)):
        return None
    if time.time() - fetched_at > max_age_seconds:
        return None

    data = payload.get("data")
    return data if data else None


def write_cache(cache_path: str, data) -> None:
    """Cache a payload atomically, so a crash cannot leave a half-written file.

    Caching is a nicety rather than a requirement, so a read-only or full disk
    is logged and otherwise ignored.
    """
    if not cache_path:
        return

    payload = {"version": CACHE_VERSION, "fetched_at": time.time(), "data": data}
    directory = os.path.dirname(os.path.abspath(cache_path))
    try:
        with tempfile.NamedTemporaryFile(
            "w", dir=directory, delete=False, suffix=".tmp"
        ) as handle:
            json.dump(payload, handle)
            temp_path = handle.name
        os.replace(temp_path, cache_path)
    except OSError as exc:
        log(f"Could not write cache to {cache_path}: {exc}")


# ----------------------------------------------------------------------
# Draft clock
# ----------------------------------------------------------------------


def get_draft_clock(
    status: str, draft_type: str, rounds: int, teams: int, picks_made: int
) -> Dict:
    """Where the draft currently stands."""
    total_picks = (rounds or 0) * (teams or 0)

    # Once the board is full the "next" pick would run off the end of the
    # draft, so report the final pick rather than an impossible one.
    current_pick = picks_made + 1
    if total_picks:
        current_pick = min(current_pick, total_picks)
    current_round = (((current_pick - 1) // teams) + 1) if teams else 0

    return {
        "status": status or "unknown",
        "type": draft_type or "unknown",
        "rounds": rounds or 0,
        "teams": teams or 0,
        "total_picks": total_picks,
        "picks_made": picks_made,
        "picks_remaining": max(0, total_picks - picks_made) if total_picks else 0,
        "current_pick": current_pick,
        "current_round": current_round,
    }


def slot_on_the_clock(clock: Dict) -> Optional[int]:
    """Which draft slot is picking.

    Snake drafts reverse the slot order on even rounds, so the slot on the
    clock is not simply the position within the round.
    """
    teams = clock["teams"]
    if not teams or clock["status"] != "drafting":
        return None

    index_in_round = (clock["current_pick"] - 1) % teams
    if clock["type"] == "snake" and clock["current_round"] % 2 == 0:
        return teams - index_in_round
    return index_in_round + 1


# ----------------------------------------------------------------------
# Roster needs
# ----------------------------------------------------------------------


def get_positional_needs(
    roster_spec: Dict,
    roster: Dict[str, List[str]],
    exclude_positions: Optional[Set[str]] = None,
) -> Dict:
    """What my roster is still missing.

    Dedicated slots are filled first, then any surplus at a flex-eligible
    position is treated as consuming a flex slot. Flex types are pooled rather
    than solved exactly - the overlap between a standard flex and a superflex
    makes precise assignment a small optimisation problem, and pooling is
    close enough to rank picks by.
    """
    counts = {position: len(players) for position, players in roster.items()}
    dedicated = dict(roster_spec.get("dedicated") or {})
    flex_total = roster_spec.get("flex_slots") or 0
    flex_eligible = set(roster_spec.get("flex_eligible") or ())

    starter_need, surplus = {}, {}
    for position, slots in dedicated.items():
        filled = counts.get(position, 0)
        starter_need[position] = max(0, slots - filled)
        surplus[position] = max(0, filled - slots)

    flex_used = sum(surplus.get(position, 0) for position in flex_eligible)
    flex_need = max(0, flex_total - flex_used)

    # Excluded positions are left out: reporting a need that will never be
    # recommended against is just noise.
    skip = exclude_positions or set()
    still_needed = sorted(
        position
        for position, need in starter_need.items()
        if need > 0 and position not in skip
    )

    return {
        "dedicated_slots": dedicated,
        "counts": counts,
        "starter_need": starter_need,
        "flex_slots": flex_total,
        "flex_eligible": sorted(flex_eligible),
        "flex_need": flex_need,
        "bench_slots": roster_spec.get("bench_slots") or 0,
        "still_needed": still_needed,
        # Preformatted for the flow's log templates: building these in Pebble
        # would mean iterating a map, which is fragile.
        "still_needed_summary": (
            ", ".join(still_needed)
            if still_needed
            else "none - remaining picks are depth"
        ),
    }


def classify_need(position: str, needs: Dict) -> str:
    """Whether a position fills a starting slot, a flex slot, or is depth."""
    if needs["starter_need"].get(position, 0) > 0:
        return "starter"
    if position in needs["flex_eligible"] and needs["flex_need"] > 0:
        return "flex"
    return "depth"


def need_penalty(
    need: str, position: str, needs: Dict, flex_penalty: float, depth_penalty: float
) -> float:
    """How much to discount a player for not filling a need.

    Expressed in the same units as the value score - picks - so the two can
    simply be added. Depth is penalised more the deeper I already am at that
    position: a backup quarterback is a poor use of a pick, and a third is
    worse.
    """
    if need == "starter":
        return 0.0
    if need == "flex":
        return flex_penalty

    already = needs["counts"].get(position, 0)
    dedicated = needs["dedicated_slots"].get(position, 0)
    return depth_penalty * (1 + max(0, already - dedicated))


# ----------------------------------------------------------------------
# Scoring
# ----------------------------------------------------------------------


def score_player_value(player: Dict, current_pick: int) -> float:
    """How many picks past their expected slot a player has fallen.

    Positive means the player is still on the board later than expected, i.e.
    value. Negative means taking them now would be a reach. The score is
    deliberately not clamped at zero: clamping makes every player score 0.0 at
    the top of the draft, leaving the recommendations indistinguishable and no
    alert able to fire.
    """
    return float(current_pick) - float(player["expected_pick"])


def get_recommendations(
    available: List[Dict],
    current_pick: int,
    top_n: int,
    needs: Dict,
    flex_penalty: float,
    depth_penalty: float,
) -> List[Dict]:
    """Best available players once my roster is taken into account.

    Every available player has to be scored before ranking, not just the best
    few: the need penalty breaks the tie between rank and score, so a slightly
    worse player at a position I still need can - and should - outrank a great
    player at one I have already filled.
    """
    scored = []
    for player in available:
        position = player.get("position") or ""
        need = classify_need(position, needs)
        penalty = need_penalty(need, position, needs, flex_penalty, depth_penalty)
        raw = score_player_value(player, current_pick)
        scored.append(
            {
                **player,
                "value_score": raw,
                "need": need,
                "need_penalty": penalty,
                "adjusted_score": raw - penalty,
            }
        )

    # Best adjusted score first, then the better-ranked player as a tiebreak.
    scored.sort(key=lambda p: (-p["adjusted_score"], p["expected_pick"]))
    return scored[: max(top_n, 0)]


def should_alert(score: float, threshold: float) -> bool:
    """Whether a player has fallen far enough past their rank to flag."""
    return score > threshold


def format_recommendations(
    recommendations: List[Dict], threshold: float
) -> List[Dict]:
    """The recommendation list as it appears in the analysis payload."""
    return [
        {
            "rank": i + 1,
            "player_id": player.get("id"),
            "name": player.get("name"),
            "position": player.get("position"),
            "nfl_team": player.get("nfl_team"),
            "expected_pick": round(float(player["expected_pick"]), 1),
            "value_score": round(player["value_score"], 2),
            "need": player["need"],
            "need_penalty": round(player["need_penalty"], 2),
            "adjusted_score": round(player["adjusted_score"], 2),
            # Alerting is on the roster-aware score, so a player at a position
            # already filled cannot raise an alert on raw value alone.
            "alert": should_alert(player["adjusted_score"], threshold),
        }
        for i, player in enumerate(recommendations)
    ]


def roster_summary(roster: Dict[str, List[str]]) -> str:
    """Preformatted roster counts for the flow's log templates."""
    return (
        ", ".join(
            f"{position} {len(players)}"
            for position, players in sorted(roster.items())
        )
        or "nothing drafted yet"
    )


# ----------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------


def summarize(result: Dict) -> str:
    """A short operator-facing summary.

    The full payload already goes to the output file and to Kestra outputs, so
    dumping it into the log as well just buries the useful lines - a poll
    running every 30 seconds needs to stay readable.
    """
    if result.get("error"):
        return f"Analysis failed: {result['error']}"

    status = result["draft_status"]
    clock = result.get("on_the_clock") or {}
    info = result.get("draft_info") or {}

    kind = "Mock draft" if info.get("is_mock") else "Draft"
    label = (
        f'"{info["name"]}" ({result["draft_id"]})'
        if info.get("name")
        else result["draft_id"]
    )
    scoring = f" {info['scoring_type']}" if info.get("scoring_type") else ""
    lines = [
        f"{kind} {label}{scoring} is {status['status']} - "
        f"round {status['current_round']}, pick {status['current_pick']} "
        f"of {status['total_picks']}"
    ]
    if clock.get("slot"):
        mine = " (your pick)" if clock.get("is_my_pick") else ""
        lines.append(f"On the clock: slot {clock['slot']}{mine}")

    roster = result.get("my_roster") or {}
    if roster:
        total = sum(len(players) for players in roster.values())
        lines.append(f"My roster ({total} picks): {result.get('my_roster_summary')}")
    needs = result.get("roster_needs") or {}
    if needs.get("still_needed"):
        lines.append(f"Starters still needed: {', '.join(needs['still_needed'])}")
    elif roster:
        lines.append("All starting slots filled - remaining picks are depth")

    lines.append(
        f"{result['players_available']} players available, "
        f"{result['high_value_alerts']} above the alert threshold"
    )
    for rec in result["recommendations"]:
        flag = " <-- ALERT" if rec["alert"] else ""
        penalty = (
            f" -{rec['need_penalty']:g} {rec['need']}" if rec["need_penalty"] else ""
        )
        lines.append(
            f"  {rec['rank']}. {rec['name']} ({rec['position']}/{rec['nfl_team']}) "
            f"exp #{rec['expected_pick']:g} value {rec['value_score']:+g}{penalty} "
            f"=> {rec['adjusted_score']:+g}{flag}"
        )
    return "\n".join(lines)


def error_payload(message: str) -> Dict:
    """A failure payload shaped like a successful one.

    The flow references these keys unconditionally, so a failed analysis has
    to keep the shape rather than collapse to just an error string.
    """
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "draft_status": {"status": "error"},
        "draft_info": {"is_mock": False, "name": "", "scoring_type": ""},
        "my_roster": {},
        "my_roster_summary": "",
        "roster_needs": {"still_needed": [], "still_needed_summary": ""},
        "players_available": 0,
        "recommendations": [],
        "high_value_alerts": 0,
        "is_drafting": False,
        "cache_refreshed": False,
        "error": message,
    }


def run_and_report(analyze: Callable[[], Dict]) -> int:
    """Run an analysis, publish it every way the flow expects, return an exit code.

    Shared so both providers report identically: same output file, same Kestra
    output key, same metrics, same summary format.
    """
    output_file = os.getenv("OUTPUT_FILE", "draft_analysis.json").strip()

    try:
        result = analyze()
        exit_code = 0
    except DraftAnalysisError as exc:
        log(f"ERROR: {exc}")
        result = error_payload(str(exc))
        exit_code = 1

    # Always write the file: the flow declares it as an output, and a failed
    # run should still leave a readable record of why.
    if output_file:
        with open(output_file, "w") as handle:
            json.dump(result, handle, indent=2)

    emit_kestra({"outputs": {"analysis": result}})
    if not result.get("error"):
        emit_kestra(
            {
                "metrics": [
                    {
                        "name": "picks_made",
                        "type": "counter",
                        "value": result["draft_status"]["picks_made"],
                    },
                    {
                        "name": "high_value_alerts",
                        "type": "counter",
                        "value": result["high_value_alerts"],
                    },
                ]
            }
        )

    log(summarize(result))
    return exit_code
