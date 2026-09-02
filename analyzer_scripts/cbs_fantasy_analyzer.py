#!/usr/bin/env python3
"""
CBS Sports Fantasy Football Draft Decision Assistant

Polls a live CBS Sports Fantasy draft and reports the best-value players still
on the board, weighted by what my roster still needs.

Scoring, roster-need penalties and reporting all live in `draft_analysis.py`,
shared with the Sleeper and Yahoo analyzers. This module is only the CBS client
and the normalization of CBS's data into the shapes that module expects.

CBS is the only one of the three that publishes the *draft order* up front, so
whose turn it is can be read straight off the board rather than inferred:
Sleeper has to be asked for `draft_order`, and Yahoo does not reveal the order
until round one has been picked.

CBS's API is the v3.0 Fantasy Platform API. Its developer portal is retired,
but the API itself still serves, and a league access token is minted from the
`general/oauth` endpoints. Tokens carry no documented expiry, so unlike Yahoo
there is nothing to refresh on a timer - a token is minted once and reused
until CBS rejects it.

Configuration is entirely via environment variables:

  CBS_LEAGUE_ID       Required. Your league's CBS subdomain - the `myleague` in
                      https://myleague.football.cbssports.com/ . The league URL
                      works in place of the bare id.
  CBS_ACCESS_TOKEN    A league access token, used as-is. This is how the flow
                      avoids minting a token on every poll.
  CBS_CLIENT_ID       API client id. Any non-empty string is accepted by CBS's
                      token endpoints; it identifies your app, nothing more.
  CBS_CLIENT_SECRET   API client secret, paired with the client id.
  CBS_USER_ID         The CBS account the token is minted for - the email you
                      sign in to CBS Sports with. Required only when minting.
  CBS_SPORT           Which CBS fantasy game. Default "football".
  CBS_RANKINGS_SOURCE Which CBS ranking to score against: "cbs_avg_ppr"
                      (default) or "cbs_avg" for non-PPR. An unrecognized
                      value is silently ignored by CBS, which is logged.
  CBS_BASE_URL        API root. Overridable for testing against a fixture.
  CBS_TOKEN_URL       OAuth root. Overridable for testing against a fixture.

  ALERT_THRESHOLD, TOP_N, EXCLUDE_POSITIONS, REQUIRE_NFL_TEAM, FLEX_PENALTY,
  DEPTH_PENALTY, OUTPUT_FILE
                      As per the Sleeper analyzer.

When a token is minted, it is emitted as a Kestra output so the flow can cache
it and avoid minting another on the next poll.
"""

import os
import re
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set

import requests

import draft_analysis as da

DEFAULT_BASE_URL = "https://api.cbssports.com/fantasy"
DEFAULT_TOKEN_URL = "https://api.cbssports.com/general/oauth"
DEFAULT_SPORT = "football"

# Every request carries this; CBS defaults to v1 response shapes without it.
API_VERSION = "3.0"

# CBS's overall ranking, which is what a player's expected pick comes from.
DEFAULT_RANKINGS_SOURCE = "cbs_avg_ppr"

# CBS names roster slots with its own position abbreviations, listed in full at
# /fantasy/positions. Several are aliases for a position the shared scoring
# model already knows, so they are folded together rather than treated as
# positions of their own.
POSITION_ALIASES = {
    # Team defence, and the two halves CBS also offers separately.
    "DST": "DEF",
    "D": "DEF",
    "ST": "DEF",
    # "Team" slots, which take every player on one NFL team at that position.
    "TQB": "QB",
    "TK": "K",
}

# CBS writes flex slots as the eligible positions joined by hyphens...
FLEX_POSITIONS = {
    "RB-WR": ("RB", "WR"),
    "WR-TE": ("WR", "TE"),
    "RB-WR-TE": ("RB", "WR", "TE"),
    # ...except the superflex, which it just calls FLEX.
    "FLEX": ("QB", "RB", "WR", "TE"),
}

# Bench is a roster *status* on CBS rather than a slot, so it is read from the
# statuses list by description rather than from the positions list.
BENCH_STATUS_KEYWORDS = ("reserve", "bench")
TOTAL_STATUS_KEYWORDS = ("total",)

# CBS's draft_state vocabulary, mapped onto the states the flow switches on.
# The flow's cases are named for Sleeper's vocabulary, so all three providers
# are translated to the same set rather than the flow needing to know which
# platform it is reading. An auction sits in bidding/nominating rather than
# picking, but it is still a draft in progress.
DRAFT_STATES = {
    "awaitingstart": "pre_draft",
    "picking": "drafting",
    "bidding": "drafting",
    "nominating": "drafting",
    "completed": "complete",
    "suspended": "suspended",
}

# CBS's order_type, mapped onto the two orderings the shared snake arithmetic
# understands.
ORDER_TYPES = {"snake": "snake", "nonsnaking": "linear"}


def coerce_league_id(value: str) -> str:
    """Accept either a bare CBS league id or the league URL it came from.

    A CBS league *is* a subdomain - https://myleague.football.cbssports.com/ -
    so the id is the first label of the host. People copy the address bar
    rather than picking that label out of it, so a pasted link is the common
    case.

    Anything unrecognizable is handed back untouched so the caller can put the
    original value in its error message.
    """
    value = (value or "").strip()
    if not value:
        return ""

    match = re.search(r"(?:https?://)?([^./\s]+)\.[^./\s]*\.?cbssports\.com", value)
    if match:
        return match.group(1)
    # Not a URL: strip any stray scheme or path someone pasted around the id.
    return value.strip("/").split("/")[-1]


def _to_float(value, default: Optional[float] = None) -> Optional[float]:
    """CBS returns numbers as strings, and empty strings for 'no limit'."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value, default: int = 0) -> int:
    return int(_to_float(value, float(default)) or default)


def canonical_position(abbr: str) -> str:
    """One of the positions the shared scoring model knows, or "".

    CBS lists individual defensive slots (DL, LB, DB) and their flex, which
    this tool does not recommend for; they normalize to "" and are ignored
    rather than miscounted as a need.
    """
    abbr = (abbr or "").strip().upper()
    abbr = POSITION_ALIASES.get(abbr, abbr)
    return abbr if abbr in da.FANTASY_POSITIONS else ""


class CBSAnalyzer:
    def __init__(
        self,
        league_id: str,
        access_token: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        user_id: Optional[str] = None,
        sport: str = DEFAULT_SPORT,
        base_url: str = DEFAULT_BASE_URL,
        token_url: str = DEFAULT_TOKEN_URL,
    ):
        if not league_id:
            raise da.DraftAnalysisError("A CBS league id is required")

        self.league_id = league_id
        self.access_token = access_token
        self.client_id = client_id
        self.client_secret = client_secret
        self.user_id = user_id
        self.sport = sport
        self.base_url = base_url.rstrip("/")
        self.token_url = token_url.rstrip("/")
        self.session = da.build_session()
        # Set when a token was minted, so the flow knows to cache it rather
        # than minting another on the next poll.
        self.token_minted = False
        # The ranking actually scored against, which is not always the one
        # asked for - see fetch_ranked_players.
        self.rankings_source = ""

    # ------------------------------------------------------------------
    # OAuth
    # ------------------------------------------------------------------

    def ensure_access_token(self) -> str:
        """Return a usable access token, minting one only when necessary.

        CBS tokens carry no documented expiry, so the flow passes in a cached
        one and this is a no-op on all but the first poll.
        """
        if self.access_token:
            return self.access_token

        if not (self.client_id and self.client_secret and self.user_id):
            raise da.DraftAnalysisError(
                "No CBS access token supplied and no credentials to mint one "
                "with. Set CBS_ACCESS_TOKEN, or CBS_CLIENT_ID, "
                "CBS_CLIENT_SECRET and CBS_USER_ID."
            )

        da.log("Minting a CBS access token...")
        # CBS's flow is two hops: a request token bound to the account, then
        # the access token it is exchanged for.
        request_token = self._token_request(
            "request_token",
            {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "user_id": self.user_id,
            },
        ).get("token")
        if not request_token:
            raise da.DraftAnalysisError(
                "CBS returned no request token; check CBS_USER_ID"
            )

        token = self._token_request(
            "access_token",
            {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "request_token": request_token,
            },
        ).get("access_token")
        if not token:
            raise da.DraftAnalysisError("CBS returned no access token")

        self.access_token = token
        self.token_minted = True
        da.log("Got a new CBS access token")
        return token

    def _token_request(self, path: str, params: Dict[str, str]) -> Dict:
        """Run one hop of CBS's token flow and return its body.

        GET, with the credentials in the query string. That is not where
        credentials belong, but it is the only thing CBS accepts: the same
        request as a POST with a form body answers HTTP 500. The resource
        endpoints take `access_token` as a query parameter too, so this API
        puts secrets in URLs by design and there is nothing to work around.
        """
        url = f"{self.token_url}/{path}"
        try:
            response = self.session.get(
                url,
                params={**params, "response_format": "json"},
                timeout=da.REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise da.DraftAnalysisError(f"Request to {url} failed: {exc}") from exc

        if not response.ok:
            raise da.DraftAnalysisError(
                f"{url} returned HTTP {response.status_code}: {response.text[:300]}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise da.DraftAnalysisError(f"{url} returned a non-JSON body") from exc

        # CBS answers a failed token request with 200 and an errors block, so
        # the body has to be inspected rather than the status code trusted.
        body = payload.get("body") or {}
        errors = self._collect_errors(body)
        if errors:
            raise da.DraftAnalysisError(f"CBS rejected {path}: {errors}")
        return body

    # ------------------------------------------------------------------
    # Requests
    # ------------------------------------------------------------------

    @staticmethod
    def _collect_errors(body: Dict) -> str:
        """CBS's in-body error text, if any.

        The `error` value is sometimes a string and sometimes a list of them,
        so both are flattened into one message.
        """
        errors = (body.get("errors") or {}).get("error") if body else None
        if not errors:
            return ""
        if isinstance(errors, str):
            return errors
        return "; ".join(str(item) for item in errors)

    def _get(self, path: str, params: Optional[Dict[str, str]] = None) -> Dict:
        """GET a CBS resource and return its `body`.

        Every resource takes the same four parameters, so they are applied here
        rather than at each call site.
        """
        token = self.ensure_access_token()
        query = {
            "version": API_VERSION,
            "SPORT": self.sport,
            "response_format": "json",
            "league_id": self.league_id,
            "access_token": token,
            **(params or {}),
        }
        url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            response = self.session.get(
                url, params=query, timeout=da.REQUEST_TIMEOUT_SECONDS
            )
        except requests.RequestException as exc:
            raise da.DraftAnalysisError(f"Request to {url} failed: {exc}") from exc

        # CBS reports auth and parameter problems as HTTP 400 with a bare
        # text/plain reason, so those never reach the JSON decode below.
        if response.status_code == 400:
            reason = response.text.strip()[:200]
            if "access token" in reason.lower():
                raise da.DraftAnalysisError(
                    f"CBS rejected the access token ({reason}). Mint a new one "
                    "with scripts/cbs_access_token.sh."
                )
            raise da.DraftAnalysisError(
                f"CBS rejected the request for {path}: {reason}. Check "
                f"CBS_LEAGUE_ID ('{self.league_id}') - it is your league's "
                "subdomain, the 'myleague' in "
                "https://myleague.football.cbssports.com/."
            )
        if not response.ok:
            raise da.DraftAnalysisError(
                f"{url} returned HTTP {response.status_code}: {response.text[:300]}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise da.DraftAnalysisError(f"{url} returned a non-JSON body") from exc

        body = payload.get("body")
        if not isinstance(body, dict):
            raise da.DraftAnalysisError(f"{url} returned no body")

        errors = self._collect_errors(body)
        if errors:
            raise da.DraftAnalysisError(f"CBS returned an error for {path}: {errors}")
        return body

    # ------------------------------------------------------------------
    # Fetching
    # ------------------------------------------------------------------

    def fetch_league_details(self) -> Dict:
        """League name, size and draft state."""
        details = self._get("league/details").get("league_details")
        if not isinstance(details, dict):
            raise da.DraftAnalysisError(
                f"CBS has no league '{self.league_id}'. The league id is the "
                "subdomain of your league's URL."
            )
        return details

    def fetch_draft_config(self) -> Dict:
        """Round count and whether the order snakes."""
        return self._get("league/draft/config").get("draft") or {}

    def fetch_roster_rules(self) -> Dict:
        """The roster half of the league rules."""
        rules = self._get("league/rules").get("rules") or {}
        return rules.get("roster") or {}

    def fetch_draft_order(self) -> List[Dict]:
        """Pick number -> team, as published before the draft starts.

        CBS delivers round one for a snake or manual order and every round for
        a custom one, so the list is not assumed to cover the whole board.
        """
        order = self._get("league/draft/order").get("draft_order") or {}
        picks = []
        for entry in order.get("picks") or []:
            if not isinstance(entry, dict):
                continue
            team = entry.get("team") or {}
            number = _to_int(entry.get("number"))
            if not number:
                continue
            picks.append(
                {
                    "pick": number,
                    "round": _to_int(entry.get("round")),
                    # Team ids come back as a string here and an int on the
                    # draft board, so both are compared as strings.
                    "team_id": str(team.get("id") or ""),
                    "team_name": team.get("name") or "",
                }
            )
        picks.sort(key=lambda p: p["pick"])
        return picks

    def fetch_draft_results(self) -> List[Dict]:
        """Every pick made so far, best-known first.

        CBS lists only completed picks here, so unlike Yahoo there are no
        empty placeholder rows to filter out - but a pick with no player is
        still skipped rather than trusted.
        """
        results = self._get("league/draft/results").get("draft_results") or {}
        picks = []
        for entry in results.get("picks") or []:
            if not isinstance(entry, dict):
                continue
            player = entry.get("player") or {}
            player_id = str(player.get("id") or "")
            if not player_id:
                continue
            team = entry.get("team") or {}
            picks.append(
                {
                    "pick": _to_int(entry.get("overall_pick")),
                    "round": _to_int(entry.get("round")),
                    "round_pick": _to_int(entry.get("round_pick")),
                    "team_id": str(team.get("id") or ""),
                    "player_id": player_id,
                    "name": player.get("fullname") or player_id,
                    "position": canonical_position(player.get("position")),
                    "nfl_team": (player.get("pro_team") or "FA").upper(),
                }
            )
        picks.sort(key=lambda p: p["pick"])
        return picks

    def fetch_my_team_id(self) -> Optional[str]:
        """The team id belonging to the account the token was minted for.

        CBS marks it with `logged_in_team` rather than making the caller supply
        it, so there is nothing for the user to configure.
        """
        for team in self._get("league/teams").get("teams") or []:
            if not isinstance(team, dict):
                continue
            if _to_int(team.get("logged_in_team")):
                return str(team.get("id") or "")
        return None

    def fetch_ranked_players(self, source: str) -> List[Dict]:
        """CBS's overall player ranking, best-ranked first.

        This is where a player's expected pick comes from. CBS also publishes
        an average-draft-position resource, which would be the better source -
        it is real draft data rather than an editorial ranking - but it has
        answered every request with HTTP 500 since well before this was
        written, so the overall ranking is what there is.

        Unlike Sleeper's search rank, this needs no normalizing: it is already
        a dense 1..N ordering of draftable players, which is the same scale as
        an overall pick number.
        """
        ranking = self._get(
            "players/rankings", {"type": "overall", "source": source}
        ).get("rankings")
        if not isinstance(ranking, dict):
            raise da.DraftAnalysisError("CBS returned no player ranking")

        # CBS ignores a source it does not recognize and serves its default
        # instead, silently scoring against the wrong ranking. Report what was
        # actually served rather than what was asked for, and say so.
        served = ranking.get("source") or source
        self.rankings_source = served
        if served != source:
            da.log(
                f"CBS does not publish a '{source}' ranking; scoring against "
                f"'{served}' instead"
            )

        players = []
        for entry in ranking.get("players") or []:
            if not isinstance(entry, dict):
                continue
            player = self._parse_player(entry)
            if player:
                players.append(player)

        players.sort(key=lambda p: p["expected_pick"])
        return players

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_player(entry: Dict) -> Optional[Dict]:
        """Normalize one ranked CBS player into the shared player shape.

        Unranked players are dropped, for the same reason the Sleeper analyzer
        drops them: without an expected pick there is no value to estimate.
        """
        player_id = str(entry.get("id") or "")
        rank = _to_float(entry.get("rank"))
        if not player_id or not rank or rank <= 0:
            return None

        name = entry.get("fullname") or " ".join(
            part
            for part in (entry.get("firstname"), entry.get("lastname"))
            if part
        ).strip()

        return {
            "id": player_id,
            "name": name or player_id,
            "position": canonical_position(entry.get("position")),
            "nfl_team": (entry.get("pro_team") or "FA").upper(),
            "expected_pick": rank,
            "bye_week": entry.get("bye_week") or "",
        }

    @staticmethod
    def get_available_players(
        ranked: List[Dict],
        drafted: Set[str],
        exclude_positions: Set[str],
        require_nfl_team: bool,
    ) -> List[Dict]:
        """Undrafted, rankable players, best first."""
        available = []
        for player in ranked:
            if player["id"] in drafted:
                continue
            # A player whose position normalized to "" is an individual
            # defensive player, which this tool does not recommend for.
            if not player["position"]:
                continue
            if player["position"] in exclude_positions:
                continue
            if require_nfl_team and player["nfl_team"] in ("", "FA"):
                continue
            available.append(player)
        return available

    # ------------------------------------------------------------------
    # Roster / draft state
    # ------------------------------------------------------------------

    @staticmethod
    def build_roster_spec(roster_rules: Dict, rounds: int) -> Dict:
        """Normalize CBS's roster rules into a roster spec.

        CBS states a per-position `max_active`, which is that position's
        starting slots, and puts flex slots in the same list under their own
        abbreviations. Bench is not a slot at all but a roster *status*, so it
        comes from the statuses list.
        """
        dedicated: Dict[str, int] = {}
        flex_slots = 0
        flex_eligible: Set[str] = set()

        for entry in roster_rules.get("positions") or []:
            if not isinstance(entry, dict):
                continue
            abbr = (entry.get("abbr") or "").strip().upper()
            # A blank max_active means "no limit", which for a draft is not a
            # starting requirement - so it counts as no slots rather than many.
            slots = _to_int(entry.get("max_active"))
            if not abbr or not slots:
                continue

            if abbr in FLEX_POSITIONS:
                flex_slots += slots
                flex_eligible.update(FLEX_POSITIONS[abbr])
                continue

            position = canonical_position(abbr)
            if position:
                dedicated[position] = dedicated.get(position, 0) + slots
            # Anything else (individual defensive slots and their flex) is not
            # something this tool recommends for, so it is ignored rather than
            # miscounted as a need.

        # Ensure every fantasy position has an entry, so a league with no
        # kicker slot reports zero need rather than no opinion.
        for position in sorted(da.FANTASY_POSITIONS):
            dedicated.setdefault(position, 0)

        starters = sum(dedicated.values()) + flex_slots
        bench = CBSAnalyzer._bench_slots(roster_rules, starters)
        if not bench:
            bench = max(0, (rounds or 0) - starters)

        return {
            "dedicated": dedicated,
            "flex_slots": flex_slots,
            "flex_eligible": flex_eligible,
            "bench_slots": bench,
        }

    @staticmethod
    def _bench_slots(roster_rules: Dict, starters: int) -> int:
        """How many bench spots the league carries.

        CBS's statuses are described in prose ("Reserve Players", "Total
        Players") rather than keyed, so they are matched on the description.
        A league that names its bench something else still gets a sane answer
        from the total minus the starters.
        """
        reserve = total = 0
        for entry in roster_rules.get("statuses") or []:
            if not isinstance(entry, dict):
                continue
            description = (entry.get("description") or "").lower()
            count = _to_int(entry.get("max"))
            if any(word in description for word in BENCH_STATUS_KEYWORDS):
                reserve += count
            elif any(word in description for word in TOTAL_STATUS_KEYWORDS):
                total = max(total, count)

        if reserve:
            return reserve
        return max(0, total - starters) if total else 0

    @staticmethod
    def normalize_draft_status(details: Dict) -> str:
        """Map CBS's draft_state onto the states the flow switches on.

        CBS writes the pre-draft state both as `awaitingstart` and, in its own
        documentation, as `awaiting start`, so the value is squashed to letters
        before being looked up.
        """
        raw = re.sub(r"[^a-z]", "", (details.get("draft_state") or "").lower())
        return DRAFT_STATES.get(raw, "unknown")

    @staticmethod
    def normalize_order_type(config: Dict) -> str:
        """Whether the board snakes or runs linear.

        CBS says this outright, unlike Yahoo, whose `draft_type` describes how
        the draft is conducted and never whether the order reverses.
        """
        raw = (config.get("order_type") or "").strip().lower()
        return ORDER_TYPES.get(raw, "snake")

    @staticmethod
    def get_my_picks(
        draft_results: List[Dict], my_team_id: Optional[str]
    ) -> List[Dict]:
        """The picks belonging to my team."""
        if not my_team_id:
            return []
        return [pick for pick in draft_results if pick["team_id"] == my_team_id]

    @staticmethod
    def summarize_roster(my_picks: List[Dict]) -> Dict[str, List[str]]:
        """My drafted players grouped by position.

        Read from the draft board itself, which carries each player's name and
        position, so no per-player lookup is needed.
        """
        roster: Dict[str, List[str]] = {}
        for pick in my_picks:
            if not pick["position"]:
                continue
            roster.setdefault(pick["position"], []).append(pick["name"])
        return roster

    @staticmethod
    def build_slot_map(draft_order: List[Dict]) -> Dict[int, str]:
        """Draft slot -> team id, from round one of the published order.

        In round one the overall pick number *is* the draft slot, which is what
        the shared snake arithmetic reports.
        """
        return {
            pick["pick"]: pick["team_id"]
            for pick in draft_order
            if pick["round"] == 1 and pick["team_id"]
        }

    @staticmethod
    def get_my_draft_slot(
        draft_order: List[Dict], my_team_id: Optional[str]
    ) -> Optional[int]:
        """My draft slot, known before the draft starts."""
        if not my_team_id:
            return None
        for slot, team_id in CBSAnalyzer.build_slot_map(draft_order).items():
            if team_id == my_team_id:
                return slot
        return None

    @staticmethod
    def get_on_the_clock(
        clock: Dict,
        draft_order: List[Dict],
        my_team_id: Optional[str],
        my_slot: Optional[int],
    ) -> Dict:
        """Which draft slot is picking, and whether it is mine.

        CBS publishes every round's picks for a custom order and only round
        one's for a snake, so the current pick is looked up on the board first
        and derived from the snake arithmetic only when it is not there. The
        lookup is what makes a keeper or custom order come out right, where
        the arithmetic would assume a regular snake.
        """
        slot_map = CBSAnalyzer.build_slot_map(draft_order)
        entry = {pick["pick"]: pick for pick in draft_order}.get(
            clock["current_pick"]
        )

        if entry and clock["status"] == "drafting":
            team_id = entry["team_id"]
            if entry["round"] == 1:
                slot = entry["pick"]
            else:
                # Beyond round one the pick number is not the slot, so recover
                # the slot from whichever round-one pick that team holds.
                slot = next(
                    (
                        candidate
                        for candidate, holder in slot_map.items()
                        if holder == team_id
                    ),
                    None,
                )
        else:
            slot = da.slot_on_the_clock(clock)
            team_id = slot_map.get(slot) if slot else None

        result = {"slot": slot, "team_id": team_id or None, "is_my_pick": None}
        if my_team_id:
            if team_id:
                # Prefer the team id: it is what the board actually states, and
                # it stays right for a custom order where the slot has to be
                # inferred.
                result["is_my_pick"] = team_id == my_team_id
            elif slot is not None and my_slot is not None:
                result["is_my_pick"] = slot == my_slot
            # Otherwise my own slot is unknown too, so there is nothing to
            # compare. Answering "not your pick" there would be confidently
            # wrong at exactly the moment it matters, so stay unknown.
        return result

    def describe_draft(self, details: Dict, config: Dict) -> Dict:
        """Identify the league being watched.

        CBS runs mock drafts in a separate lobby that is not exposed as a
        league resource, so `is_mock` is always false here. The field is kept
        so all three providers emit the same payload shape.
        """
        return {
            "league_id": self.league_id,
            "is_mock": False,
            "name": details.get("name") or "",
            # CBS publishes no scoring type on a league; the ranking the
            # analysis scored against is reported as `rankings_source`.
            "scoring_type": "",
            "season_status": details.get("season_status") or "",
            "league_type": details.get("type") or "",
            "sport": self.sport,
            "draft_type": details.get("draft_type") or config.get("type") or "",
            "order_type": config.get("order_type") or "",
            "provider": "cbs",
        }


def run_analysis() -> Dict:
    """Run one polling cycle and return the analysis payload."""
    league_id = coerce_league_id(os.getenv("CBS_LEAGUE_ID", ""))

    threshold = da.env_float("ALERT_THRESHOLD", 10.0)
    top_n = da.env_int("TOP_N", 5)
    flex_penalty = da.env_float("FLEX_PENALTY", 8.0)
    depth_penalty = da.env_float("DEPTH_PENALTY", 20.0)
    require_nfl_team = da.env_bool("REQUIRE_NFL_TEAM", True)
    exclude_positions = da.env_positions("EXCLUDE_POSITIONS")
    rankings_source = (
        os.getenv("CBS_RANKINGS_SOURCE", "").strip() or DEFAULT_RANKINGS_SOURCE
    )

    analyzer = CBSAnalyzer(
        league_id=league_id,
        access_token=os.getenv("CBS_ACCESS_TOKEN", "").strip() or None,
        client_id=os.getenv("CBS_CLIENT_ID", "").strip() or None,
        client_secret=os.getenv("CBS_CLIENT_SECRET", "").strip() or None,
        user_id=os.getenv("CBS_USER_ID", "").strip() or None,
        sport=os.getenv("CBS_SPORT", "").strip() or DEFAULT_SPORT,
        base_url=os.getenv("CBS_BASE_URL", "").strip() or DEFAULT_BASE_URL,
        token_url=os.getenv("CBS_TOKEN_URL", "").strip() or DEFAULT_TOKEN_URL,
    )

    details = analyzer.fetch_league_details()
    config = analyzer.fetch_draft_config()
    roster_rules = analyzer.fetch_roster_rules()
    draft_order = analyzer.fetch_draft_order()
    draft_results = analyzer.fetch_draft_results()

    draft_info = analyzer.describe_draft(details, config)
    teams = _to_int(details.get("num_teams"))
    rounds = _to_int(config.get("rounds"))
    roster_spec = analyzer.build_roster_spec(roster_rules, rounds)

    clock = da.get_draft_clock(
        analyzer.normalize_draft_status(details),
        analyzer.normalize_order_type(config),
        rounds,
        teams,
        len(draft_results),
    )

    my_team_id = analyzer.fetch_my_team_id()
    if not my_team_id:
        da.log(
            "No team in this league is owned by the account the token was "
            "minted for, so whose turn it is cannot be reported"
        )
    my_slot = analyzer.get_my_draft_slot(draft_order, my_team_id)
    on_the_clock = analyzer.get_on_the_clock(
        clock, draft_order, my_team_id, my_slot
    )
    roster = analyzer.summarize_roster(
        analyzer.get_my_picks(draft_results, my_team_id)
    )
    needs = da.get_positional_needs(roster_spec, roster, exclude_positions)

    ranked = analyzer.fetch_ranked_players(rankings_source)
    drafted = {pick["player_id"] for pick in draft_results}
    available = analyzer.get_available_players(
        ranked, drafted, exclude_positions, require_nfl_team
    )
    recommendations = da.get_recommendations(
        available, clock["current_pick"], top_n, needs, flex_penalty, depth_penalty
    )
    formatted = da.format_recommendations(recommendations, threshold)

    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "provider": "cbs",
        "league_id": league_id,
        "draft_id": league_id,
        "draft_info": draft_info,
        "draft_status": clock,
        "on_the_clock": on_the_clock,
        "my_team_id": my_team_id,
        "my_draft_slot": my_slot,
        "my_roster": roster,
        "my_roster_summary": da.roster_summary(roster),
        "roster_needs": needs,
        "players_available": len(available),
        "alert_threshold": threshold,
        "rankings_source": analyzer.rankings_source,
        "recommendations": formatted,
        "high_value_alerts": sum(1 for rec in formatted if rec["alert"]),
        "is_drafting": clock["status"] == "drafting",
        "cache_refreshed": False,
        "error": None,
    }

    # Hand a freshly minted token back to the flow so it can be cached rather
    # than re-minted on the next poll.
    if analyzer.token_minted:
        emit_token(analyzer)
    return result


def emit_token(analyzer: "CBSAnalyzer") -> None:
    """Publish a minted token as a Kestra output for the flow to cache."""
    da.emit_kestra(
        {
            "outputs": {
                "token": {
                    "access_token": analyzer.access_token,
                    "minted": True,
                }
            }
        }
    )


if __name__ == "__main__":
    sys.exit(da.run_and_report(run_analysis))
