#!/usr/bin/env python3
"""
Sleeper Fantasy Football Draft Decision Assistant

Polls a live Sleeper draft and reports the best-value players still on the
board, weighted by what my roster still needs.

Scoring, roster-need penalties and reporting all live in `draft_analysis.py`,
shared with the Yahoo analyzer. This module is only the Sleeper client and the
normalization of Sleeper's data into the shapes that module expects.

Configuration is entirely via environment variables:

  SLEEPER_LEAGUE_OR_DRAFT_ID
                      Required. The draft or league to watch. Either kind of id
                      works - which one you gave is detected automatically - so
                      a mock draft (which has no league) is configured exactly
                      like a league draft.
  SLEEPER_USER_ID     Your Sleeper user id or username. When set, the analysis
                      reports whether the draft is on your clock, and which
                      picks are yours.
  SLEEPER_BASE_URL    API root. Overridable for testing against a fixture.
  ALERT_THRESHOLD     Value score above which a player is flagged. Default 10.
  TOP_N               How many recommendations to return. Default 5.
  EXCLUDE_POSITIONS   Comma-separated positions to drop, e.g. "K,DEF".
  REQUIRE_NFL_TEAM    Drop players with no NFL team, who cannot score. Default
                      true; set false to include free agents.
  FLEX_PENALTY        Picks to discount a player who only fills a flex slot.
                      Default 8.
  DEPTH_PENALTY       Picks to discount a player at a position whose starting
                      slots are already filled. Default 20.
  OUTPUT_FILE         Where to write the analysis JSON. Default draft_analysis.json.
  PLAYERS_CACHE_PATH  Where to cache the player pool. Default sleeper_players_cache.json.

Both id settings also accept the Sleeper URL you copied them from.
"""

import os
import re
import sys
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Set, Tuple

import requests

import draft_analysis as da

DEFAULT_BASE_URL = "https://api.sleeper.app/v1"

# Sleeper describes a roster as `slots_*` counts in the draft settings. These
# must be filled by one specific position...
DEDICATED_SLOTS = {
    "slots_qb": "QB",
    "slots_rb": "RB",
    "slots_wr": "WR",
    "slots_te": "TE",
    "slots_k": "K",
    "slots_def": "DEF",
}

# ...and these take any of several positions.
FLEX_SLOTS = {
    "slots_flex": ("RB", "WR", "TE"),
    "slots_super_flex": ("QB", "RB", "WR", "TE"),
    "slots_rec_flex": ("WR", "TE"),
}

# Sleeper stores "no meaningful rank" as this sentinel rather than null.
UNRANKED_SENTINEL = 9999999

# Sleeper asks that the ~14MB player endpoint be hit at most once per day.
PLAYERS_CACHE_MAX_AGE_SECONDS = 24 * 60 * 60


def coerce_sleeper_id(value: str) -> str:
    """Accept either a bare Sleeper id or the URL it was copied out of.

    People copy the address bar rather than digging the id out of it, and a
    mock draft is only ever reachable by its URL
    (https://sleeper.com/draft/nfl/<draft_id>), so a pasted link is the common
    case rather than the exotic one.

    Anything unrecognizable is handed back untouched so the caller can put the
    original value in its error message.
    """
    value = (value or "").strip()
    if not value or value.isdigit():
        return value

    match = re.search(r"\d{6,}", value)
    return match.group(0) if match else value


class SleeperAnalyzer:
    def __init__(
        self,
        identifier: str,
        base_url: str = DEFAULT_BASE_URL,
        user_id: Optional[str] = None,
    ):
        if not identifier:
            raise da.DraftAnalysisError(
                "A Sleeper draft id or league id is required"
            )

        self.identifier = identifier
        self.draft_id: Optional[str] = None
        self.user_id = user_id
        self.base_url = base_url.rstrip("/")
        self.session = da.build_session()
        # Set when the player pool came from Sleeper rather than the cache, so
        # the caller knows the on-disk cache is worth persisting.
        self.cache_refreshed = False

    def _get(self, path: str):
        """GET a Sleeper endpoint, returning None when the resource is absent.

        Sleeper answers unknown ids with ``200 OK`` and a literal ``null`` body
        rather than a 404, so an empty body has to be treated as "not found"
        just like a real error status.
        """
        url = f"{self.base_url}{path}"
        try:
            response = self.session.get(url, timeout=da.REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            raise da.DraftAnalysisError(f"Request to {url} failed: {exc}") from exc

        if response.status_code == 404:
            return None
        if not response.ok:
            raise da.DraftAnalysisError(f"{url} returned HTTP {response.status_code}")

        try:
            return response.json()
        except ValueError as exc:
            raise da.DraftAnalysisError(f"{url} returned a non-JSON body") from exc

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    def resolve_draft(self) -> Tuple[str, Dict]:
        """Resolve the configured id to a draft, whichever kind it is.

        Leagues and drafts have separate id spaces on Sleeper and a mock draft
        has no league at all, so the only way to accept one id for both is to
        try it as each. Drafts are probed first: a mock is reachable no other
        way, and a league id does not resolve as a draft.
        """
        draft = self._get(f"/draft/{self.identifier}")
        if draft:
            self.draft_id = self.identifier
            return self.identifier, draft

        league = self._get(f"/league/{self.identifier}")
        if not league:
            raise da.DraftAnalysisError(
                f"'{self.identifier}' is not a Sleeper draft id or league id. "
                "For a mock draft, use the id in "
                "https://sleeper.com/draft/nfl/<draft_id>; for a league, the "
                "number in https://sleeper.com/leagues/<league_id>/... . A "
                "user id or username will not work here."
            )

        draft_id = league.get("draft_id")
        if not draft_id:
            raise da.DraftAnalysisError(
                f"League '{league.get('name') or self.identifier}' has no draft yet"
            )

        draft = self._get(f"/draft/{draft_id}")
        if not draft:
            raise da.DraftAnalysisError(
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
            da.log(
                f"Sleeper has no user '{username}', so the flow cannot tell "
                "whose pick it is"
            )
            self.user_id = None
            return None

        da.log(f"Resolved Sleeper user '{username}' to id {resolved}")
        self.user_id = resolved
        return resolved

    # ------------------------------------------------------------------
    # Fetching
    # ------------------------------------------------------------------

    def fetch_draft_picks(self, draft_id: str) -> List[Dict]:
        return self._get(f"/draft/{draft_id}/picks") or []

    def fetch_all_players(self, cache_path: str) -> Dict[str, Dict]:
        """Return the fantasy-relevant player pool, keyed by player_id.

        The upstream endpoint is ~14MB of every player Sleeper knows about, so
        the response is pruned to the fields and positions this tool actually
        uses before being cached to disk.
        """
        cached = da.read_cache(cache_path, PLAYERS_CACHE_MAX_AGE_SECONDS)
        if cached is not None:
            da.log(f"Using cached player pool ({len(cached)} players)")
            return cached

        da.log("Fetching player pool from Sleeper (this endpoint is large)...")
        raw = self._get("/players/nfl")
        if not raw:
            raise da.DraftAnalysisError("Sleeper returned an empty player pool")

        players = self._prune_players(raw)
        self.cache_refreshed = True
        da.write_cache(cache_path, players)
        da.log(f"Fetched {len(players)} fantasy-relevant players")
        return players

    @staticmethod
    def _prune_players(raw: Dict[str, Dict]) -> Dict[str, Dict]:
        """Keep only fantasy-relevant players and the fields we score on."""
        pruned = {}
        for player_id, player in raw.items():
            if not isinstance(player, dict):
                continue

            positions = set(player.get("fantasy_positions") or ())
            if not positions & da.FANTASY_POSITIONS:
                continue

            full_name = player.get("full_name") or " ".join(
                part
                for part in (player.get("first_name"), player.get("last_name"))
                if part
            ).strip()

            pruned[player_id] = {
                "id": player_id,
                "name": full_name or player_id,
                "position": player.get("position") or "",
                "nfl_team": player.get("team") or "FA",
                "search_rank": player.get("search_rank"),
            }
        return pruned

    # ------------------------------------------------------------------
    # Ranking
    # ------------------------------------------------------------------

    @staticmethod
    def build_expected_picks(players: Dict[str, Dict]) -> Dict[str, int]:
        """Turn Sleeper's search_rank into a dense 1..N expected pick number.

        Sleeper publishes no ADP. ``search_rank`` is the closest thing, but it
        cannot be compared to a pick number directly: it is a search-popularity
        rank, it is heavily tied (a single rank can be shared by 100+ players),
        and it leaves gaps. Sorting the pool and taking each player's ordinal
        position yields a value on the same scale as "overall pick number",
        which is what the value score needs.

        Ties are broken by name so the ordering is stable between runs rather
        than dependent on dict iteration order.
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

        Picks on the board but not yet filled carry a null player_id.
        """
        return {pick.get("player_id") for pick in picks if pick.get("player_id")}

    @staticmethod
    def get_available_players(
        players: Dict[str, Dict],
        drafted: Set[str],
        expected_picks: Dict[str, int],
        exclude_positions: Set[str],
        require_nfl_team: bool,
    ) -> List[Dict]:
        """Undrafted, rankable players, best first.

        Unranked players are dropped rather than pushed to the back: without a
        rank there is no value to estimate, and Sleeper marks thousands of
        deep-roster players this way.

        Players with no NFL team are dropped too. Sleeper's `status` and
        `active` flags do not reliably mark players who have left the league -
        Todd Gurley, retired since 2021, is still listed as active with a
        search rank of 27 - but having no team does, and someone on no roster
        cannot score.
        """
        available = []
        for player_id, player in players.items():
            if player_id in drafted or player_id not in expected_picks:
                continue
            if player.get("position") in exclude_positions:
                continue
            if require_nfl_team and player.get("nfl_team") in (None, "", "FA"):
                continue
            available.append(
                {**player, "expected_pick": expected_picks[player_id]}
            )

        available.sort(key=lambda p: p["expected_pick"])
        return available

    # ------------------------------------------------------------------
    # Roster / draft state
    # ------------------------------------------------------------------

    @staticmethod
    def build_roster_spec(draft: Dict) -> Dict:
        """Normalize Sleeper's `slots_*` settings into a roster spec.

        `slots_bn` is bench and must not be counted as positional need; some
        leagues omit it entirely and leave the bench implied by the round count.
        """
        settings = draft.get("settings") or {}
        dedicated = {
            position: settings.get(key) or 0
            for key, position in DEDICATED_SLOTS.items()
        }
        flex_slots = sum(settings.get(key) or 0 for key in FLEX_SLOTS)
        flex_eligible = {
            position
            for key, positions in FLEX_SLOTS.items()
            if settings.get(key)
            for position in positions
        }

        starters = sum(dedicated.values()) + flex_slots
        bench = settings.get("slots_bn")
        if not bench:
            bench = max(0, (settings.get("rounds") or 0) - starters)

        return {
            "dedicated": dedicated,
            "flex_slots": flex_slots,
            "flex_eligible": flex_eligible,
            "bench_slots": bench,
        }

    def get_my_draft_slot(self, draft: Dict) -> Optional[int]:
        """Which draft slot is mine, per Sleeper's draft order."""
        if not self.user_id:
            return None
        slot = (draft.get("draft_order") or {}).get(self.user_id)
        return slot if isinstance(slot, int) else None

    @staticmethod
    def get_my_picks(
        picks: List[Dict], my_slot: Optional[int], user_id: Optional[str]
    ) -> List[Dict]:
        """The picks belonging to me.

        Attribution goes by ``draft_slot``, which every pick carries. The
        obvious-looking field, ``picked_by``, is blank on autopicked players
        (15 of 150 in a real draft), so using it alone would quietly leave
        players off my roster and overstate what I still need.
        """
        if my_slot is not None:
            return [pick for pick in picks if pick.get("draft_slot") == my_slot]
        if user_id:
            return [pick for pick in picks if pick.get("picked_by") == user_id]
        return []

    @staticmethod
    def summarize_roster(my_picks: List[Dict]) -> Dict[str, List[str]]:
        """My drafted players grouped by position.

        Read from each pick's own metadata rather than the player pool, so it
        still works for anyone the pool has been pruned of.
        """
        roster: Dict[str, List[str]] = {}
        for pick in my_picks:
            metadata = pick.get("metadata") or {}
            position = (metadata.get("position") or "").upper()
            if not position:
                continue
            name = " ".join(
                part
                for part in (metadata.get("first_name"), metadata.get("last_name"))
                if part
            ).strip()
            roster.setdefault(position, []).append(name or pick.get("player_id", "?"))
        return roster

    @staticmethod
    def describe_draft(draft: Dict) -> Dict:
        """Identify the draft being watched.

        A mock draft belongs to no league, so Sleeper leaves ``league_id`` null
        on it - that absence is what distinguishes a mock from a real league
        draft, since the two are otherwise the same shape.
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
            "provider": "sleeper",
        }

    def get_on_the_clock(self, draft: Dict, clock: Dict, my_slot: Optional[int]) -> Dict:
        """Which draft slot is picking, and whether it is mine."""
        slot = da.slot_on_the_clock(clock)
        result = {"slot": slot, "roster_id": None, "is_my_pick": None}
        if slot is None:
            return result

        slot_to_roster = draft.get("slot_to_roster_id") or {}
        result["roster_id"] = slot_to_roster.get(str(slot), slot_to_roster.get(slot))
        if self.user_id:
            result["is_my_pick"] = my_slot == slot if my_slot is not None else False
        return result


def run_analysis() -> Dict:
    """Run one polling cycle and return the analysis payload."""
    identifier = coerce_sleeper_id(os.getenv("SLEEPER_LEAGUE_OR_DRAFT_ID", ""))
    user_id = coerce_sleeper_id(os.getenv("SLEEPER_USER_ID", ""))
    base_url = os.getenv("SLEEPER_BASE_URL", "").strip() or DEFAULT_BASE_URL
    cache_path = os.getenv(
        "PLAYERS_CACHE_PATH", "sleeper_players_cache.json"
    ).strip()

    threshold = da.env_float("ALERT_THRESHOLD", 10.0)
    top_n = da.env_int("TOP_N", 5)
    flex_penalty = da.env_float("FLEX_PENALTY", 8.0)
    depth_penalty = da.env_float("DEPTH_PENALTY", 20.0)
    require_nfl_team = da.env_bool("REQUIRE_NFL_TEAM", True)
    exclude_positions = da.env_positions("EXCLUDE_POSITIONS")

    analyzer = SleeperAnalyzer(
        identifier=identifier, base_url=base_url, user_id=user_id or None
    )

    draft_id, draft = analyzer.resolve_draft()
    analyzer.resolve_user_id()
    picks = analyzer.fetch_draft_picks(draft_id)
    players = analyzer.fetch_all_players(cache_path)

    draft_info = analyzer.describe_draft(draft)
    expected_picks = analyzer.build_expected_picks(players)
    drafted = analyzer.get_drafted_player_ids(picks)

    settings = draft.get("settings") or {}
    clock = da.get_draft_clock(
        draft.get("status"),
        draft.get("type"),
        settings.get("rounds") or 0,
        settings.get("teams") or 0,
        len(picks),
    )

    my_slot = analyzer.get_my_draft_slot(draft)
    on_the_clock = analyzer.get_on_the_clock(draft, clock, my_slot)
    roster = analyzer.summarize_roster(
        analyzer.get_my_picks(picks, my_slot, analyzer.user_id)
    )
    roster_spec = analyzer.build_roster_spec(draft)
    needs = da.get_positional_needs(roster_spec, roster, exclude_positions)

    available = analyzer.get_available_players(
        players, drafted, expected_picks, exclude_positions, require_nfl_team
    )
    recommendations = da.get_recommendations(
        available, clock["current_pick"], top_n, needs, flex_penalty, depth_penalty
    )
    formatted = da.format_recommendations(recommendations, threshold)

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "provider": "sleeper",
        "league_id": draft_info["league_id"],
        "draft_id": draft_id,
        "draft_info": draft_info,
        "draft_status": clock,
        "on_the_clock": on_the_clock,
        "my_draft_slot": my_slot,
        "my_roster": roster,
        "my_roster_summary": da.roster_summary(roster),
        "roster_needs": needs,
        "players_available": len(available),
        "alert_threshold": threshold,
        "recommendations": formatted,
        "high_value_alerts": sum(1 for rec in formatted if rec["alert"]),
        "is_drafting": clock["status"] == "drafting",
        "cache_refreshed": analyzer.cache_refreshed,
        "error": None,
    }


if __name__ == "__main__":
    sys.exit(da.run_and_report(run_analysis))
