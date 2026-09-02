#!/usr/bin/env python3
"""
Yahoo Fantasy Football Draft Decision Assistant

Polls a live Yahoo Fantasy draft and reports the best-value players still on
the board, weighted by what my roster still needs.

Scoring, roster-need penalties and reporting all live in `draft_analysis.py`,
shared with the Sleeper and CBS analyzers. This module is only the Yahoo
client and the normalization of Yahoo's data into the shapes that module
expects.

Yahoo is the only one of the three providers that publishes a real average
draft position, so a player's expected pick is their actual ADP rather than a
rank standing in for one.

Yahoo's API is OAuth 2.0 only. Access tokens last one hour, which is shorter
than a draft, so a refresh token is required and the access token is refreshed
on demand.

Configuration is entirely via environment variables:

  YAHOO_LEAGUE_KEY    Required. e.g. "449.l.123456", or the league URL to pull
                      it from. The numeric prefix is Yahoo's game key for the
                      season; YAHOO_GAME_KEY fills it in if you only have the
                      league id.
  YAHOO_GAME_KEY      Game key to assume when YAHOO_LEAGUE_KEY is a bare league
                      id. Default "nfl", which Yahoo resolves to the current
                      season.
  YAHOO_ACCESS_TOKEN  A cached access token, used as-is while it is still
                      valid. This is how the flow avoids refreshing on every
                      poll.
  YAHOO_ACCESS_TOKEN_EXPIRES_AT
                      Unix timestamp the cached token expires at. A token
                      within two minutes of expiry is refreshed rather than
                      risking it going stale mid-request.
  YAHOO_REFRESH_TOKEN Long-lived refresh token. Required unless a valid access
                      token is supplied.
  YAHOO_CLIENT_ID     OAuth client id (Yahoo calls it the Consumer Key).
  YAHOO_CLIENT_SECRET OAuth client secret (Consumer Secret).
  YAHOO_REDIRECT_URI  Redirect URI registered with the app. Default "oob".
  YAHOO_BASE_URL      API root. Overridable for testing against a fixture.
  YAHOO_TOKEN_URL     Token endpoint. Overridable for testing against a fixture.
  YAHOO_PLAYER_PAGES  How many 25-player pages of available players to pull.
                      Default 4 (the top 100 by Yahoo's own ranking).

  ALERT_THRESHOLD, TOP_N, EXCLUDE_POSITIONS, REQUIRE_NFL_TEAM, FLEX_PENALTY,
  DEPTH_PENALTY, OUTPUT_FILE
                      As per the Sleeper analyzer.

When the access token is refreshed, the new token and its expiry are emitted as
Kestra outputs so the flow can cache them and avoid re-refreshing every poll.
"""

import base64
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Set

import requests

import draft_analysis as da

DEFAULT_BASE_URL = "https://fantasysports.yahooapis.com/fantasy/v2"
DEFAULT_TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"

PAGE_SIZE = 25

# Yahoo names roster slots in league settings' `roster_positions`. These must
# be filled by one specific position...
DEDICATED_POSITIONS = frozenset({"QB", "RB", "WR", "TE", "K", "DEF"})

# ...and these take any of several. Yahoo writes them with slashes.
FLEX_POSITIONS = {
    "W/R": ("WR", "RB"),
    "W/T": ("WR", "TE"),
    "W/R/T": ("WR", "RB", "TE"),
    "Q/W/R/T": ("QB", "WR", "RB", "TE"),
    "R/W/T": ("WR", "RB", "TE"),
}

# Bench slots are not positional need, but they are drafted, so they count
# toward the number of rounds.
BENCH_POSITIONS = frozenset({"BN"})

# Injury slots are neither a need nor drafted into - counting them would
# inflate the round count and so the total pick count.
RESERVE_POSITIONS = frozenset({"IR", "IR+", "IL", "NA"})

# Refresh a little early rather than discovering expiry mid-draft.
TOKEN_EXPIRY_MARGIN_SECONDS = 120


def coerce_league_key(value: str, game_key: str) -> str:
    """Accept a Yahoo league key, a bare league id, or the league URL.

    A full key looks like ``449.l.123456``. Yahoo's own league URLs
    (https://football.fantasysports.yahoo.com/f1/123456) expose only the
    numeric league id, which is what people have to hand, so a bare id is
    completed with the configured game key.
    """
    value = (value or "").strip().rstrip("/")
    if not value:
        return ""

    # Already a full key, possibly pasted with surrounding URL fragments.
    match = re.search(r"\d+\.l\.\d+", value)
    if match:
        return match.group(0)

    # A URL or a bare id: take the last long number in it as the league id.
    numbers = re.findall(r"\d{3,}", value)
    if numbers:
        return f"{game_key}.l.{numbers[-1]}"
    return value


# ----------------------------------------------------------------------
# Yahoo JSON helpers
#
# Yahoo returns objects as arrays of single-key fragments, and collections as
# dicts keyed "0", "1", ... alongside a "count". Both shapes vary between
# resources and between sub-resources of the same resource, so these helpers
# read by key name and never by position - a positional parser against this
# API breaks the first time Yahoo adds a field.
# ----------------------------------------------------------------------


def merge_fragments(node) -> Dict:
    """Flatten Yahoo's arrays of single-key dicts into one dict.

    Nested dict *values* are left alone, so structures like
    ``{"name": {"full": ...}}`` survive intact and only the fragmenting is
    undone. The first occurrence of a key wins.
    """
    merged: Dict = {}
    stack = [node]
    while stack:
        item = stack.pop(0)
        if isinstance(item, list):
            stack = list(item) + stack
        elif isinstance(item, dict):
            for key, value in item.items():
                if key not in merged:
                    merged[key] = value
    return merged


def iter_collection(node, item_key: str) -> Iterable:
    """Yield the items of a Yahoo collection.

    Collections are dicts keyed by stringified indices plus a ``count``, where
    each entry usually wraps the item under its singular name.
    """
    if not isinstance(node, dict):
        return
    for key in sorted((k for k in node if str(k).isdigit()), key=int):
        entry = node[key]
        if isinstance(entry, dict) and item_key in entry:
            yield entry[item_key]
        else:
            yield entry


def league_node(payload: Dict) -> Dict:
    """The league object, with its metadata and sub-resources merged together."""
    content = (payload or {}).get("fantasy_content") or {}
    return merge_fragments(content.get("league"))


def _to_float(value, default: Optional[float] = None) -> Optional[float]:
    """Yahoo returns numbers as strings, and empty strings for 'no value'."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class YahooAnalyzer:
    def __init__(
        self,
        league_key: str,
        access_token: Optional[str] = None,
        refresh_token: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        redirect_uri: str = "oob",
        base_url: str = DEFAULT_BASE_URL,
        token_url: str = DEFAULT_TOKEN_URL,
    ):
        if not league_key:
            raise da.DraftAnalysisError("A Yahoo league key is required")

        self.league_key = league_key
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.base_url = base_url.rstrip("/")
        self.token_url = token_url
        self.session = da.build_session()
        # Set when a new access token was obtained, so the flow knows to cache
        # it rather than refreshing again on the next poll.
        self.token_refreshed = False
        self.refresh_token_rotated = False
        self.token_expires_at: Optional[float] = None

    # ------------------------------------------------------------------
    # OAuth
    # ------------------------------------------------------------------

    def ensure_access_token(self) -> str:
        """Return a usable access token, refreshing only when necessary.

        The flow passes in a cached token; refreshing on every poll would mean
        hitting Yahoo's token endpoint twice a minute for a whole draft, which
        their terms specifically discourage.
        """
        if self.access_token:
            return self.access_token

        if not self.refresh_token:
            raise da.DraftAnalysisError(
                "No Yahoo access token supplied and no refresh token to get one "
                "with. Set YAHOO_REFRESH_TOKEN (plus YAHOO_CLIENT_ID and "
                "YAHOO_CLIENT_SECRET)."
            )
        if not (self.client_id and self.client_secret):
            raise da.DraftAnalysisError(
                "Refreshing a Yahoo access token needs YAHOO_CLIENT_ID and "
                "YAHOO_CLIENT_SECRET"
            )

        da.log("Refreshing Yahoo access token...")
        basic = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()
        ).decode()
        try:
            response = self.session.post(
                self.token_url,
                headers={
                    "Authorization": f"Basic {basic}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={
                    "grant_type": "refresh_token",
                    "redirect_uri": self.redirect_uri,
                    "refresh_token": self.refresh_token,
                },
                timeout=da.REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise da.DraftAnalysisError(
                f"Yahoo token refresh request failed: {exc}"
            ) from exc

        if not response.ok:
            # The body carries Yahoo's reason (invalid_grant on a revoked
            # refresh token, for instance), which is the actionable part.
            raise da.DraftAnalysisError(
                f"Yahoo token refresh returned HTTP {response.status_code}: "
                f"{response.text[:300]}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise da.DraftAnalysisError(
                "Yahoo token refresh returned a non-JSON body"
            ) from exc

        token = payload.get("access_token")
        if not token:
            raise da.DraftAnalysisError(
                f"Yahoo token refresh returned no access_token: {payload}"
            )

        self.access_token = token
        self.token_refreshed = True
        # Yahoo usually returns the same refresh token, but if it ever rotates
        # one the stored secret is now stale and every later poll will fail
        # until it is updated - so say so loudly rather than silently using a
        # value that lives only for this run.
        rotated = payload.get("refresh_token")
        if rotated and rotated != self.refresh_token:
            self.refresh_token_rotated = True
            da.log(
                "WARNING: Yahoo issued a NEW refresh token. Update the "
                "YAHOO_REFRESH_TOKEN secret, or future polls will fail once "
                "the old one is rejected."
            )
            self.refresh_token = rotated
        expires_in = _to_float(payload.get("expires_in"), 3600.0) or 3600.0
        self.token_expires_at = time.time() + expires_in
        da.log(f"Got a new Yahoo access token, valid for {int(expires_in)}s")
        return token

    # ------------------------------------------------------------------
    # Requests
    # ------------------------------------------------------------------

    def _get(self, path: str) -> Dict:
        """GET a Yahoo resource as JSON."""
        token = self.ensure_access_token()
        separator = "&" if "?" in path else "?"
        url = f"{self.base_url}{path}{separator}format=json"
        try:
            response = self.session.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=da.REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise da.DraftAnalysisError(f"Request to {url} failed: {exc}") from exc

        if response.status_code == 401:
            raise da.DraftAnalysisError(
                "Yahoo rejected the access token (401). If this persists, the "
                "refresh token may have been revoked - reauthorize the app."
            )
        if response.status_code == 404:
            raise da.DraftAnalysisError(
                f"Yahoo has no such resource: {path}. Check YAHOO_LEAGUE_KEY "
                f"('{self.league_key}') - it must look like 449.l.123456, and "
                "the game key prefix changes every season."
            )
        if not response.ok:
            raise da.DraftAnalysisError(
                f"{url} returned HTTP {response.status_code}: {response.text[:300]}"
            )

        try:
            return response.json()
        except ValueError as exc:
            raise da.DraftAnalysisError(f"{url} returned a non-JSON body") from exc

    # ------------------------------------------------------------------
    # Fetching
    # ------------------------------------------------------------------

    def fetch_league(self) -> Dict:
        """League metadata plus settings, in one request."""
        return league_node(self._get(f"/league/{self.league_key};out=settings"))

    def fetch_draft_results(self) -> List[Dict]:
        """Every pick made so far.

        Yahoo lists the full board once the draft exists, including picks not
        yet made, which carry no player key.
        """
        league = league_node(self._get(f"/league/{self.league_key}/draftresults"))
        results = []
        for entry in iter_collection(league.get("draft_results"), "draft_result"):
            pick = merge_fragments(entry)
            if not pick.get("player_key"):
                continue
            results.append(
                {
                    "pick": int(_to_float(pick.get("pick"), 0) or 0),
                    "round": int(_to_float(pick.get("round"), 0) or 0),
                    "team_key": pick.get("team_key") or "",
                    "player_key": pick.get("player_key"),
                }
            )
        results.sort(key=lambda p: p["pick"])
        return results

    def fetch_available_players(self, pages: int) -> List[Dict]:
        """Available players with their ADP, best-ranked first.

        Yahoo's ``status=A`` filter means "not on any roster", which during a
        draft is exactly "undrafted" - so unlike Sleeper there is no need to
        pull the whole player universe and diff it against the board. Only the
        first few pages matter: nobody is drafting the 300th-ranked player
        while a hundred better ones are free.
        """
        players: List[Dict] = []
        for page in range(max(pages, 1)):
            start = page * PAGE_SIZE
            payload = self._get(
                f"/league/{self.league_key}/players;status=A;sort=OR;"
                f"start={start};count={PAGE_SIZE};out=draft_analysis"
            )
            batch = list(iter_collection(league_node(payload).get("players"), "player"))
            if not batch:
                break
            for entry in batch:
                player = self._parse_player(entry)
                if player:
                    players.append(player)
            if len(batch) < PAGE_SIZE:
                break

        players.sort(key=lambda p: p["expected_pick"])
        return players

    def fetch_my_team_key(self) -> Optional[str]:
        """The team key belonging to the authenticated user in this league.

        Yahoo marks it with ``is_owned_by_current_login`` rather than making
        the caller supply it, so there is nothing for the user to configure.
        """
        league = league_node(self._get(f"/league/{self.league_key}/teams"))
        for entry in iter_collection(league.get("teams"), "team"):
            team = merge_fragments(entry)
            if str(team.get("is_owned_by_current_login") or "") == "1":
                return team.get("team_key")
        return None

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_player(entry) -> Optional[Dict]:
        """Normalize one Yahoo player into the shared player shape.

        Players with no ADP are dropped: without an expected pick there is no
        value to estimate, which is the same reason the Sleeper analyzer drops
        unranked players.
        """
        player = merge_fragments(entry)
        player_key = player.get("player_key")
        if not player_key:
            return None

        name = player.get("name")
        full_name = (
            name.get("full")
            if isinstance(name, dict)
            else (name if isinstance(name, str) else "")
        )

        position = (player.get("display_position") or "").upper()
        # display_position can list multiple ("WR,RB"); the primary is first.
        position = position.split(",")[0].strip()
        if not position:
            position = (player.get("primary_position") or "").upper()

        analysis = merge_fragments(player.get("draft_analysis"))
        average_pick = _to_float(analysis.get("average_pick"))
        if not average_pick or average_pick <= 0:
            return None

        editorial_team = (
            player.get("editorial_team_abbr")
            or player.get("editorial_team_full_name")
            or ""
        )

        return {
            "id": player_key,
            "name": full_name or player_key,
            "position": position,
            "nfl_team": (editorial_team or "FA").upper(),
            "expected_pick": average_pick,
            "percent_drafted": _to_float(analysis.get("percent_drafted")),
        }

    # ------------------------------------------------------------------
    # Roster / draft state
    # ------------------------------------------------------------------

    @staticmethod
    def build_roster_spec(league: Dict) -> Dict:
        """Normalize Yahoo's `roster_positions` into a roster spec.

        Yahoo lists every slot including bench and IR, which are not positional
        need and must not be counted as such.
        """
        settings = merge_fragments(league.get("settings"))
        dedicated: Dict[str, int] = {}
        flex_slots = 0
        flex_eligible: Set[str] = set()
        bench_slots = 0

        reserve_slots = 0
        for entry in settings.get("roster_positions") or []:
            slot = merge_fragments(entry)
            # Yahoo wraps each entry under a singular "roster_position" key.
            slot = merge_fragments(slot.get("roster_position", slot))
            position = (slot.get("position") or "").upper()
            count = int(_to_float(slot.get("count"), 0) or 0)
            if not position or not count:
                continue

            if position in BENCH_POSITIONS:
                bench_slots += count
            elif position in RESERVE_POSITIONS:
                reserve_slots += count
            elif position in DEDICATED_POSITIONS:
                dedicated[position] = dedicated.get(position, 0) + count
            elif position in FLEX_POSITIONS:
                flex_slots += count
                flex_eligible.update(FLEX_POSITIONS[position])
            # Anything else (individual defensive slots, for instance) is not
            # something this tool recommends for, so it is ignored rather than
            # miscounted as a need.

        # Ensure every fantasy position has an entry, so a league with no
        # kicker slot reports zero need rather than no opinion.
        for position in sorted(DEDICATED_POSITIONS):
            dedicated.setdefault(position, 0)

        return {
            "dedicated": dedicated,
            "flex_slots": flex_slots,
            "flex_eligible": flex_eligible,
            "bench_slots": bench_slots,
            "reserve_slots": reserve_slots,
        }

    @staticmethod
    def get_my_picks(
        draft_results: List[Dict], my_team_key: Optional[str]
    ) -> List[Dict]:
        """The picks belonging to my team."""
        if not my_team_key:
            return []
        return [
            pick for pick in draft_results if pick.get("team_key") == my_team_key
        ]

    def summarize_roster(self, my_picks: List[Dict]) -> Dict[str, List[str]]:
        """My drafted players grouped by position.

        Yahoo's draft results give only player keys, so the names and positions
        have to be looked up. They are fetched in one batched request rather
        than one per pick.
        """
        roster: Dict[str, List[str]] = {}
        keys = [pick["player_key"] for pick in my_picks if pick.get("player_key")]
        if not keys:
            return roster

        for chunk_start in range(0, len(keys), PAGE_SIZE):
            chunk = keys[chunk_start : chunk_start + PAGE_SIZE]
            payload = self._get(f"/players;player_keys={','.join(chunk)}")
            content = (payload or {}).get("fantasy_content") or {}
            for entry in iter_collection(content.get("players"), "player"):
                player = merge_fragments(entry)
                name = player.get("name")
                full_name = (
                    name.get("full")
                    if isinstance(name, dict)
                    else (name if isinstance(name, str) else "")
                )
                position = (player.get("display_position") or "").upper()
                position = position.split(",")[0].strip()
                if not position:
                    continue
                roster.setdefault(position, []).append(
                    full_name or player.get("player_key") or "?"
                )
        return roster

    @staticmethod
    def describe_draft(league: Dict) -> Dict:
        """Identify the league being watched.

        Yahoo has no public mock-draft API - mocks are a separate pre-draft
        product and are not exposed as league resources - so `is_mock` is
        always false here. The field is kept so every provider emits the same
        payload shape.
        """
        settings = merge_fragments(league.get("settings"))
        return {
            "league_id": league.get("league_id"),
            "is_mock": False,
            "name": league.get("name") or "",
            "scoring_type": league.get("scoring_type") or "",
            "season": league.get("season") or "",
            "sport": "nfl",
            "draft_type": settings.get("draft_type") or "",
            "provider": "yahoo",
        }

    @staticmethod
    def infer_draft_order_type(draft_results: List[Dict]) -> str:
        """Whether the board snakes or runs linear.

        Yahoo's ``draft_type`` describes how the draft is conducted (live,
        auction, offline) and never whether the order reverses, so it cannot be
        used here - passing it straight through leaves the snake reversal off
        and puts every even round's slots in the wrong order. The board itself
        answers the question: compare round two's team order to round one's.

        Falls back to snake, which is Yahoo's default, when there is not yet a
        second round to compare.
        """
        first = [p["team_key"] for p in draft_results if p["round"] == 1]
        second = [p["team_key"] for p in draft_results if p["round"] == 2]
        if first and len(first) == len(second):
            if second == list(reversed(first)):
                return "snake"
            if second == first:
                return "linear"
        return "snake"

    @staticmethod
    def normalize_draft_status(league: Dict) -> str:
        """Map Yahoo's draft_status onto the states the flow switches on.

        Yahoo says predraft/drafting/postdraft; the flow's cases are named for
        Sleeper's vocabulary, so every provider is translated to the same set
        rather than the flow needing to know which platform it is reading.
        """
        return {
            "predraft": "pre_draft",
            "drafting": "drafting",
            "postdraft": "complete",
        }.get((league.get("draft_status") or "").lower(), "unknown")

    @staticmethod
    def build_slot_map(draft_results: List[Dict]) -> Dict[int, str]:
        """Draft slot -> team key, learned from round one of the board.

        Yahoo does not publish the draft order anywhere; it is only revealed as
        round one is picked. In round one the overall pick number *is* the draft
        slot, so the board is the one available source - and it stays correct
        for linear drafts too, unlike assuming an order.
        """
        return {
            pick["pick"]: pick["team_key"]
            for pick in draft_results
            if pick["round"] == 1 and pick["team_key"]
        }

    def get_my_draft_slot(
        self, draft_results: List[Dict], my_team_key: Optional[str]
    ) -> Optional[int]:
        """My draft slot, once round one has reached it."""
        if not my_team_key:
            return None
        for slot, team_key in self.build_slot_map(draft_results).items():
            if team_key == my_team_key:
                return slot
        return None

    def get_on_the_clock(
        self,
        clock: Dict,
        draft_results: List[Dict],
        my_team_key: Optional[str],
        my_slot: Optional[int],
    ) -> Dict:
        """Which draft slot is picking, and whether it is mine."""
        slot = da.slot_on_the_clock(clock)
        result = {"slot": slot, "team_key": None, "is_my_pick": None}
        if slot is None:
            return result

        result["team_key"] = self.build_slot_map(draft_results).get(slot)
        if my_team_key:
            # My slot is unknown until my first round-one pick lands on the
            # board. Reporting "not your pick" then would be a confidently
            # wrong answer at exactly the moment it matters, so stay unknown.
            result["is_my_pick"] = (slot == my_slot) if my_slot is not None else None
        return result


def run_analysis() -> Dict:
    """Run one polling cycle and return the analysis payload."""
    game_key = os.getenv("YAHOO_GAME_KEY", "").strip() or "nfl"
    league_key = coerce_league_key(os.getenv("YAHOO_LEAGUE_KEY", ""), game_key)

    threshold = da.env_float("ALERT_THRESHOLD", 10.0)
    top_n = da.env_int("TOP_N", 5)
    flex_penalty = da.env_float("FLEX_PENALTY", 8.0)
    depth_penalty = da.env_float("DEPTH_PENALTY", 20.0)
    require_nfl_team = da.env_bool("REQUIRE_NFL_TEAM", True)
    exclude_positions = da.env_positions("EXCLUDE_POSITIONS")
    pages = da.env_int("YAHOO_PLAYER_PAGES", 4)

    access_token = os.getenv("YAHOO_ACCESS_TOKEN", "").strip() or None
    expires_at = da.env_float("YAHOO_ACCESS_TOKEN_EXPIRES_AT", 0.0)
    if access_token and expires_at:
        remaining = expires_at - time.time()
        if remaining < TOKEN_EXPIRY_MARGIN_SECONDS:
            # Yahoo tokens last an hour and a draft lasts longer, so this is
            # the normal path partway through, not an error.
            da.log(
                f"Cached Yahoo token expires in {int(remaining)}s; refreshing"
            )
            access_token = None

    analyzer = YahooAnalyzer(
        league_key=league_key,
        access_token=access_token,
        refresh_token=os.getenv("YAHOO_REFRESH_TOKEN", "").strip() or None,
        client_id=os.getenv("YAHOO_CLIENT_ID", "").strip() or None,
        client_secret=os.getenv("YAHOO_CLIENT_SECRET", "").strip() or None,
        redirect_uri=os.getenv("YAHOO_REDIRECT_URI", "").strip() or "oob",
        base_url=os.getenv("YAHOO_BASE_URL", "").strip() or DEFAULT_BASE_URL,
        token_url=os.getenv("YAHOO_TOKEN_URL", "").strip() or DEFAULT_TOKEN_URL,
    )

    league = analyzer.fetch_league()
    if not league.get("league_key"):
        raise da.DraftAnalysisError(
            f"Yahoo returned no league for key '{league_key}'"
        )

    draft_info = analyzer.describe_draft(league)
    status = analyzer.normalize_draft_status(league)
    draft_results = analyzer.fetch_draft_results()

    teams = int(_to_float(league.get("num_teams"), 0) or 0)
    roster_spec = analyzer.build_roster_spec(league)
    # Yahoo states the roster size, not the round count; for a snake draft they
    # are the same thing - every team fills every slot exactly once.
    rounds = (
        sum(roster_spec["dedicated"].values())
        + roster_spec["flex_slots"]
        + roster_spec["bench_slots"]
    )

    clock = da.get_draft_clock(
        status,
        analyzer.infer_draft_order_type(draft_results),
        rounds,
        teams,
        len(draft_results),
    )

    my_team_key = analyzer.fetch_my_team_key()
    my_slot = analyzer.get_my_draft_slot(draft_results, my_team_key)
    if my_team_key and my_slot is None and clock["status"] == "drafting":
        da.log(
            "Draft order not yet known - Yahoo only reveals it as round one is "
            "picked, so whose turn it is cannot be reported yet"
        )
    on_the_clock = analyzer.get_on_the_clock(
        clock, draft_results, my_team_key, my_slot
    )
    roster = analyzer.summarize_roster(
        analyzer.get_my_picks(draft_results, my_team_key)
    )
    needs = da.get_positional_needs(roster_spec, roster, exclude_positions)

    available = [
        player
        for player in analyzer.fetch_available_players(pages)
        if player["position"] not in exclude_positions
        and not (require_nfl_team and player["nfl_team"] in ("", "FA"))
    ]
    recommendations = da.get_recommendations(
        available, clock["current_pick"], top_n, needs, flex_penalty, depth_penalty
    )
    formatted = da.format_recommendations(recommendations, threshold)

    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "provider": "yahoo",
        "league_id": draft_info["league_id"],
        "league_key": league_key,
        "draft_id": league_key,
        "draft_info": draft_info,
        "draft_status": clock,
        "on_the_clock": on_the_clock,
        "my_team_key": my_team_key,
        "my_draft_slot": my_slot,
        "my_roster": roster,
        "my_roster_summary": da.roster_summary(roster),
        "roster_needs": needs,
        "players_available": len(available),
        "alert_threshold": threshold,
        "recommendations": formatted,
        "high_value_alerts": sum(1 for rec in formatted if rec["alert"]),
        "is_drafting": clock["status"] == "drafting",
        "cache_refreshed": False,
        "error": None,
    }

    # Hand a freshly minted token back to the flow so it can be cached rather
    # than re-minted on the next poll.
    if analyzer.token_refreshed:
        emit_token(analyzer)
    return result


def emit_token(analyzer: "YahooAnalyzer") -> None:
    """Publish a refreshed token as a Kestra output for the flow to cache."""
    da.emit_kestra(
        {
            "outputs": {
                "token": {
                    "access_token": analyzer.access_token,
                    "expires_at": analyzer.token_expires_at,
                    "refreshed": True,
                    "refresh_token_rotated": analyzer.refresh_token_rotated,
                }
            }
        }
    )


if __name__ == "__main__":
    sys.exit(da.run_and_report(run_analysis))
