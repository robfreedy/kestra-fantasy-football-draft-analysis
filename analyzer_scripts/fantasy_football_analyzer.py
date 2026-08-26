#!/usr/bin/env python3
"""
Fantasy Football Draft Decision Assistant

Polls a live Sleeper draft and reports the best-value players still on the
board, so an orchestrator (Kestra) can alert you when something good falls.

Configuration is entirely via environment variables:

  SLEEPER_LEAGUE_OR_DRAFT_ID
                      Required. The draft or league to watch. Either kind of id
                      works - which one you gave is detected automatically - so
                      a mock draft (which has no league) is configured exactly
                      like a league draft.
  SLEEPER_USER_ID     Your Sleeper user id or username. When set, the analysis
                      reports whether the draft is currently on your clock.

Both id settings also accept the Sleeper URL you copied them from.
  SLEEPER_BASE_URL    API root. Overridable for testing against a fixture.
  ALERT_THRESHOLD     Value score above which a player is flagged. Default 10.
  TOP_N               How many recommendations to return. Default 5.
  EXCLUDE_POSITIONS   Comma-separated positions to drop, e.g. "K,DEF".
  OUTPUT_FILE         Where to write the analysis JSON. Default draft_analysis.json.
  PLAYERS_CACHE_PATH  Where to cache the player pool. Default sleeper_players_cache.json.

Writes the analysis to OUTPUT_FILE and emits it to stdout in Kestra's
script-output format so downstream tasks can branch on it.
"""

import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Set, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

DEFAULT_BASE_URL = "https://api.sleeper.app/v1"

# Positions Sleeper uses for standard fantasy rosters. Everything else in the
# player pool (OL, IDP, etc.) is noise for a redraft league.
FANTASY_POSITIONS = frozenset({"QB", "RB", "WR", "TE", "K", "DEF"})

# Sleeper stores "no meaningful rank" as this sentinel rather than null.
UNRANKED_SENTINEL = 9999999

# Sleeper asks that the ~14MB player endpoint be hit at most once per day.
PLAYERS_CACHE_MAX_AGE_SECONDS = 24 * 60 * 60
PLAYERS_CACHE_VERSION = 2

REQUEST_TIMEOUT_SECONDS = 30


class SleeperError(RuntimeError):
    """Raised when Sleeper cannot give us what we need to run an analysis."""


def _log(message: str) -> None:
    """Human-readable progress.

    Deliberately stdout, not stderr: Kestra logs a script's stderr at ERROR
    level, which would paint routine progress lines red in the UI. Kestra
    only treats stdout lines wrapped in ``::...::`` as output directives and
    logs everything else at INFO, so plain text is safe here as long as it
    never begins with ``::``.
    """
    print(message, flush=True)


def coerce_sleeper_id(value: str) -> str:
    """Accept either a bare Sleeper id or the URL it was copied out of.

    People copy the address bar rather than digging the id out of it, and a
    mock draft is only ever reachable by its URL
    (https://sleeper.com/draft/nfl/<draft_id>), so a pasted link is the
    common case rather than the exotic one. League URLs
    (https://sleeper.com/leagues/<league_id>/team) work the same way.

    Anything unrecognizable is handed back untouched so the caller can put
    the original value in its error message.
    """
    value = (value or "").strip()
    if not value or value.isdigit():
        return value

    match = re.search(r"\d{6,}", value)
    return match.group(0) if match else value


def emit_kestra(payload: Dict) -> None:
    """Emit a Kestra script-output/metric directive on stdout."""
    print("::" + json.dumps(payload) + "::", flush=True)


class SleeperFantasyAnalyzer:
    def __init__(
        self,
        identifier: str,
        base_url: str = DEFAULT_BASE_URL,
        user_id: Optional[str] = None,
    ):
        """
        Args:
            identifier: A Sleeper draft id or league id - either is accepted,
                and which one it is gets worked out on the first request.
            base_url: API root, overridable for tests.
            user_id: Your Sleeper user id or username, for on-the-clock
                detection.
        """
        if not identifier:
            raise SleeperError("A Sleeper draft id or league id is required")

        self.identifier = identifier
        self.draft_id: Optional[str] = None
        self.user_id = user_id
        self.base_url = base_url.rstrip("/")
        self.session = self._build_session()
        # Set when the player pool came from Sleeper rather than the cache, so
        # the caller knows the on-disk cache is worth persisting.
        self.cache_refreshed = False

    @staticmethod
    def _build_session() -> requests.Session:
        """A session that retries transient failures instead of dying on them.

        A draft poll runs every few seconds during a live draft; a single
        502 from Sleeper should not take the whole run down.
        """
        session = requests.Session()
        retry = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session

    def _get(self, path: str):
        """GET a Sleeper endpoint, returning None when the resource is absent.

        Sleeper answers unknown ids with ``200 OK`` and a literal ``null``
        body rather than a 404, so an empty body has to be treated as
        "not found" just like a real error status.
        """
        url = f"{self.base_url}{path}"
        try:
            response = self.session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            raise SleeperError(f"Request to {url} failed: {exc}") from exc

        if response.status_code == 404:
            return None
        if not response.ok:
            raise SleeperError(f"{url} returned HTTP {response.status_code}")

        try:
            return response.json()
        except ValueError as exc:
            raise SleeperError(f"{url} returned a non-JSON body") from exc

    # ------------------------------------------------------------------
    # Fetching
    # ------------------------------------------------------------------

    def resolve_draft(self) -> Tuple[str, Dict]:
        """Resolve the configured id to a draft, whichever kind it is.

        Leagues and drafts have separate id spaces on Sleeper and a mock draft
        has no league at all, so the only way to accept one id for both is to
        try it as each. Drafts are probed first: a mock is reachable no other
        way, and a league id does not resolve as a draft.

        Returns the draft id and the draft object, so the caller does not pay
        for a second request to fetch what was just looked up.
        """
        draft = self._get(f"/draft/{self.identifier}")
        if draft:
            self.draft_id = self.identifier
            return self.identifier, draft

        league = self._get(f"/league/{self.identifier}")
        if not league:
            raise SleeperError(
                f"'{self.identifier}' is not a Sleeper draft id or league id. "
                "For a mock draft, use the id in "
                "https://sleeper.com/draft/nfl/<draft_id>; for a league, the "
                "number in https://sleeper.com/leagues/<league_id>/... . A "
                "user id or username will not work here."
            )

        draft_id = league.get("draft_id")
        if not draft_id:
            raise SleeperError(
                f"League '{league.get('name') or self.identifier}' has no draft yet"
            )

        draft = self._get(f"/draft/{draft_id}")
        if not draft:
            raise SleeperError(
                f"League '{league.get('name') or self.identifier}' points at "
                f"draft '{draft_id}', which Sleeper cannot find"
            )

        self.draft_id = draft_id
        return draft_id, draft

    def resolve_user_id(self) -> Optional[str]:
        """Turn a Sleeper username into the numeric id draft_order is keyed by.

        ``draft_order`` maps numeric user ids to draft slots, but people know
        themselves by their username - so without this lookup, on-the-clock
        detection would quietly never match and every pick would look like
        someone else's.
        """
        if not self.user_id or self.user_id.isdigit():
            return self.user_id

        username = self.user_id.rstrip("/").rsplit("/", 1)[-1].lstrip("@")
        user = self._get(f"/user/{username}")
        resolved = (user or {}).get("user_id")
        if not resolved:
            _log(
                f"Sleeper has no user '{username}', so the flow cannot tell "
                "whose pick it is"
            )
            self.user_id = None
            return None

        _log(f"Resolved Sleeper user '{username}' to id {resolved}")
        self.user_id = resolved
        return resolved

    def fetch_draft_picks(self, draft_id: str) -> List[Dict]:
        return self._get(f"/draft/{draft_id}/picks") or []

    def fetch_all_players(self, cache_path: str) -> Dict[str, Dict]:
        """Return the fantasy-relevant player pool, keyed by player_id.

        The upstream endpoint is ~14MB of every player Sleeper knows about,
        so the response is pruned to the fields and positions this tool
        actually uses before being cached to disk.
        """
        cached = self._read_players_cache(cache_path)
        if cached is not None:
            _log(f"Using cached player pool ({len(cached)} players)")
            return cached

        self.cache_refreshed = True

        _log("Fetching player pool from Sleeper (this endpoint is large)...")
        raw = self._get("/players/nfl")
        if not raw:
            raise SleeperError("Sleeper returned an empty player pool")

        players = self._prune_players(raw)
        self._write_players_cache(cache_path, players)
        _log(f"Fetched {len(players)} fantasy-relevant players")
        return players

    @staticmethod
    def _prune_players(raw: Dict[str, Dict]) -> Dict[str, Dict]:
        """Keep only fantasy-relevant players and the fields we score on."""
        pruned = {}
        for player_id, player in raw.items():
            if not isinstance(player, dict):
                continue

            positions = set(player.get("fantasy_positions") or ())
            if not positions & FANTASY_POSITIONS:
                continue

            full_name = player.get("full_name") or " ".join(
                part for part in (player.get("first_name"), player.get("last_name")) if part
            ).strip()

            pruned[player_id] = {
                "id": player_id,
                "name": full_name or player_id,
                "position": player.get("position") or "",
                "nfl_team": player.get("team") or "FA",
                "search_rank": player.get("search_rank"),
                "status": player.get("status"),
                "active": player.get("active"),
            }
        return pruned

    @staticmethod
    def _read_players_cache(cache_path: str) -> Optional[Dict[str, Dict]]:
        """Load the cached pool, ignoring anything stale, corrupt or foreign.

        The age is stored inside the payload rather than read from the file
        mtime so that copying or restoring the cache cannot silently make a
        months-old pool look fresh.
        """
        if not cache_path or not os.path.exists(cache_path):
            return None

        try:
            with open(cache_path, "r") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            _log("Player cache is unreadable or corrupt; refetching")
            return None

        if not isinstance(payload, dict):
            return None
        if payload.get("version") != PLAYERS_CACHE_VERSION:
            return None

        fetched_at = payload.get("fetched_at")
        if not isinstance(fetched_at, (int, float)):
            return None
        if time.time() - fetched_at > PLAYERS_CACHE_MAX_AGE_SECONDS:
            return None

        players = payload.get("players")
        return players if isinstance(players, dict) and players else None

    @staticmethod
    def _write_players_cache(cache_path: str, players: Dict[str, Dict]) -> None:
        """Cache the pool atomically so a crash can't leave a half-written file.

        Caching is a nicety, not a requirement, so a read-only or full disk
        is logged and otherwise ignored.
        """
        if not cache_path:
            return

        payload = {
            "version": PLAYERS_CACHE_VERSION,
            "fetched_at": time.time(),
            "players": players,
        }
        directory = os.path.dirname(os.path.abspath(cache_path))
        try:
            with tempfile.NamedTemporaryFile(
                "w", dir=directory, delete=False, suffix=".tmp"
            ) as handle:
                json.dump(payload, handle)
                temp_path = handle.name
            os.replace(temp_path, cache_path)
        except OSError as exc:
            _log(f"Could not write player cache to {cache_path}: {exc}")

    # ------------------------------------------------------------------
    # Ranking
    # ------------------------------------------------------------------

    @staticmethod
    def build_overall_ranks(players: Dict[str, Dict]) -> Dict[str, int]:
        """Turn Sleeper's search_rank into a dense 1..N ordering.

        ``search_rank`` cannot be compared to a pick number directly: it is a
        search-popularity rank, it is heavily tied (a single rank can be
        shared by 100+ players), and it leaves gaps. Sorting the pool and
        taking each player's ordinal position yields a rank on the same
        scale as "overall pick number", which is what the value score needs.

        Ties are broken by name so that the ordering is stable between runs
        rather than depending on dict iteration order.
        """
        ranked = []
        for player_id, player in players.items():
            rank = player.get("search_rank")
            if isinstance(rank, bool) or not isinstance(rank, (int, float)):
                continue
            if rank >= UNRANKED_SENTINEL:
                continue
            ranked.append((rank, player.get("name") or "", player_id))

        ranked.sort()
        return {player_id: i + 1 for i, (_, _, player_id) in enumerate(ranked)}

    @staticmethod
    def get_drafted_player_ids(picks: Iterable[Dict]) -> Set[str]:
        """Sleeper player_ids already taken.

        Picks that are on the board but not yet filled carry a null
        ``player_id`` and are skipped.
        """
        return {pick.get("player_id") for pick in picks if pick.get("player_id")}

    def get_available_players(
        self,
        players: Dict[str, Dict],
        drafted: Set[str],
        overall_ranks: Dict[str, int],
        exclude_positions: Set[str],
    ) -> List[Dict]:
        """Undrafted, rankable players, best first.

        Unranked players are dropped rather than pushed to the back: without
        a rank there is no value to estimate, and Sleeper marks thousands of
        deep-roster players this way.
        """
        available = []
        for player_id, player in players.items():
            if player_id in drafted:
                continue
            if player_id not in overall_ranks:
                continue
            if player.get("position") in exclude_positions:
                continue
            available.append({**player, "overall_rank": overall_ranks[player_id]})

        available.sort(key=lambda p: p["overall_rank"])
        return available

    @staticmethod
    def score_player_value(player: Dict, current_pick: int) -> float:
        """How many picks past their expected slot a player has fallen.

        Positive means the player is still on the board later than their rank
        says they should be, i.e. value. Negative means taking them now would
        be a reach. The score is deliberately not clamped at zero: clamping
        made every player score 0.0 at the top of the draft, which left the
        recommendations indistinguishable and no alert could ever fire.
        """
        return float(current_pick - player["overall_rank"])

    def get_recommendations(
        self, available: List[Dict], current_pick: int, top_n: int
    ) -> List[Dict]:
        """Top-N available players by value score.

        ``available`` is already sorted by rank and the score is a strictly
        decreasing function of rank at a fixed pick number, so the best
        values are simply the best-ranked players still on the board.
        """
        scored = [
            {**player, "value_score": self.score_player_value(player, current_pick)}
            for player in available[: max(top_n, 0)]
        ]
        return scored

    # ------------------------------------------------------------------
    # Draft state
    # ------------------------------------------------------------------

    @staticmethod
    def describe_draft(draft: Dict) -> Dict:
        """Identify the draft being watched.

        A mock draft belongs to no league, so Sleeper leaves ``league_id``
        null on it - that absence is what distinguishes a mock from a real
        league draft, since the two are otherwise the same shape.
        """
        metadata = draft.get("metadata") or {}
        league_id = draft.get("league_id")
        return {
            "league_id": league_id,
            "is_mock": league_id is None,
            "name": metadata.get("name") or "",
            "scoring_type": metadata.get("scoring_type") or "",
            "season": draft.get("season") or "",
            "sport": draft.get("sport") or "",
        }

    @staticmethod
    def get_draft_clock(draft: Dict, picks: List[Dict]) -> Dict:
        """Where the draft currently stands."""
        settings = draft.get("settings") or {}
        rounds = settings.get("rounds") or 0
        teams = settings.get("teams") or 0
        total_picks = rounds * teams

        picks_made = len(picks)
        # Once the board is full the "next" pick would run off the end of the
        # draft, so report the final pick rather than an impossible one.
        current_pick = picks_made + 1
        if total_picks:
            current_pick = min(current_pick, total_picks)
        current_round = (((current_pick - 1) // teams) + 1) if teams else 0

        return {
            "status": draft.get("status", "unknown"),
            "type": draft.get("type", "unknown"),
            "rounds": rounds,
            "teams": teams,
            "total_picks": total_picks,
            "picks_made": picks_made,
            "picks_remaining": max(0, total_picks - picks_made) if total_picks else 0,
            "current_pick": current_pick,
            "current_round": current_round,
        }

    def get_on_the_clock(self, draft: Dict, clock: Dict) -> Dict:
        """Which draft slot is picking, and whether it is yours.

        Snake drafts reverse the slot order on even rounds, so the slot on
        the clock is not simply the position within the round.
        """
        teams = clock["teams"]
        result = {"slot": None, "roster_id": None, "is_my_pick": None}
        if not teams or clock["status"] != "drafting":
            return result

        index_in_round = (clock["current_pick"] - 1) % teams
        slot = index_in_round + 1
        if draft.get("type") == "snake" and clock["current_round"] % 2 == 0:
            slot = teams - index_in_round

        result["slot"] = slot

        slot_to_roster = draft.get("slot_to_roster_id") or {}
        result["roster_id"] = slot_to_roster.get(str(slot), slot_to_roster.get(slot))

        if self.user_id:
            draft_order = draft.get("draft_order") or {}
            my_slot = draft_order.get(self.user_id)
            result["is_my_pick"] = (my_slot == slot) if my_slot is not None else False

        return result

    @staticmethod
    def should_alert(value_score: float, threshold: float) -> bool:
        """Whether a player has fallen far enough past their rank to flag."""
        return value_score > threshold


# ----------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        _log(f"{name}='{raw}' is not a number; using {default}")
        return default


def _env_int(name: str, default: int) -> int:
    return int(_env_float(name, float(default)))


def run_analysis() -> Dict:
    """Run one polling cycle and return the analysis payload."""
    identifier = coerce_sleeper_id(os.getenv("SLEEPER_LEAGUE_OR_DRAFT_ID", ""))
    user_id = coerce_sleeper_id(os.getenv("SLEEPER_USER_ID", ""))
    base_url = os.getenv("SLEEPER_BASE_URL", "").strip() or DEFAULT_BASE_URL
    cache_path = os.getenv("PLAYERS_CACHE_PATH", "sleeper_players_cache.json").strip()

    threshold = _env_float("ALERT_THRESHOLD", 10.0)
    top_n = _env_int("TOP_N", 5)
    exclude_positions = {
        part.strip().upper()
        for part in os.getenv("EXCLUDE_POSITIONS", "").split(",")
        if part.strip()
    }

    analyzer = SleeperFantasyAnalyzer(
        identifier=identifier,
        base_url=base_url,
        user_id=user_id or None,
    )

    resolved_draft_id, draft = analyzer.resolve_draft()
    analyzer.resolve_user_id()
    picks = analyzer.fetch_draft_picks(resolved_draft_id)
    players = analyzer.fetch_all_players(cache_path)

    draft_info = analyzer.describe_draft(draft)
    overall_ranks = analyzer.build_overall_ranks(players)
    drafted = analyzer.get_drafted_player_ids(picks)
    clock = analyzer.get_draft_clock(draft, picks)
    on_the_clock = analyzer.get_on_the_clock(draft, clock)

    available = analyzer.get_available_players(
        players, drafted, overall_ranks, exclude_positions
    )
    recommendations = analyzer.get_recommendations(
        available, clock["current_pick"], top_n
    )

    formatted = [
        {
            "rank": i + 1,
            "player_id": player["id"],
            "name": player["name"],
            "position": player["position"],
            "nfl_team": player["nfl_team"],
            "overall_rank": player["overall_rank"],
            "value_score": round(player["value_score"], 2),
            "alert": analyzer.should_alert(player["value_score"], threshold),
        }
        for i, player in enumerate(recommendations)
    ]
    alerts = [rec for rec in formatted if rec["alert"]]

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "league_id": draft_info["league_id"],
        "draft_id": resolved_draft_id,
        "draft_info": draft_info,
        "draft_status": {
            "status": clock["status"],
            "type": clock["type"],
            "rounds": clock["rounds"],
            "teams": clock["teams"],
            "picks_made": clock["picks_made"],
            "picks_remaining": clock["picks_remaining"],
            "total_picks": clock["total_picks"],
            "current_pick": clock["current_pick"],
            "current_round": clock["current_round"],
        },
        "on_the_clock": on_the_clock,
        "players_available": len(available),
        "alert_threshold": threshold,
        "recommendations": formatted,
        "high_value_alerts": len(alerts),
        "is_drafting": clock["status"] == "drafting",
        "cache_refreshed": analyzer.cache_refreshed,
        "error": None,
    }


def _summarize(result: Dict) -> str:
    """A short operator-facing summary.

    The full payload already goes to OUTPUT_FILE and to Kestra outputs, so
    dumping it into the log as well just buries the useful lines - a poll
    running every 30 seconds needs to stay readable.
    """
    if result.get("error"):
        return f"Analysis failed: {result['error']}"

    status = result["draft_status"]
    clock = result.get("on_the_clock") or {}
    info = result.get("draft_info") or {}

    kind = "Mock draft" if info.get("is_mock") else "Draft"
    label = f'"{info["name"]}" ({result["draft_id"]})' if info.get("name") else result["draft_id"]
    scoring = f" {info['scoring_type']}" if info.get("scoring_type") else ""
    lines = [
        f"{kind} {label}{scoring} is {status['status']} - "
        f"round {status['current_round']}, pick {status['current_pick']} "
        f"of {status['total_picks']}"
    ]
    if clock.get("slot"):
        mine = " (your pick)" if clock.get("is_my_pick") else ""
        lines.append(f"On the clock: slot {clock['slot']}{mine}")
    lines.append(
        f"{result['players_available']} players available, "
        f"{result['high_value_alerts']} above the alert threshold"
    )
    for rec in result["recommendations"]:
        flag = " <-- ALERT" if rec["alert"] else ""
        lines.append(
            f"  {rec['rank']}. {rec['name']} ({rec['position']}/{rec['nfl_team']}) "
            f"rank #{rec['overall_rank']} value {rec['value_score']:+}{flag}"
        )
    return "\n".join(lines)


def main() -> int:
    output_file = os.getenv("OUTPUT_FILE", "draft_analysis.json").strip()

    try:
        result = run_analysis()
        exit_code = 0
    except SleeperError as exc:
        _log(f"ERROR: {exc}")
        result = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "draft_status": {"status": "error"},
            "draft_info": {"is_mock": False, "name": "", "scoring_type": ""},
            "recommendations": [],
            "high_value_alerts": 0,
            "is_drafting": False,
            "cache_refreshed": False,
            "error": str(exc),
        }
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

    _log(_summarize(result))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
