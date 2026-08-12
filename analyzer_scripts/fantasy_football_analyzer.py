#!/usr/bin/env python3
"""
Fantasy Football Draft Decision Assistant
Analyzes a live Sleeper draft and recommends high-value picks
"""

import requests
import json
import os
import time
from datetime import datetime
from typing import Dict, List, Optional

PLAYERS_CACHE_PATH = "sleeper_players_cache.json"
PLAYERS_CACHE_MAX_AGE_SECONDS = 24 * 60 * 60  # Sleeper asks this endpoint be hit at most once/day


class SleeperFantasyAnalyzer:
    def __init__(self, league_id: str):
        """
        Initialize Sleeper Fantasy Football analyzer

        Args:
            league_id: Your Sleeper league ID (visible in the league URL)
        """
        self.league_id = league_id
        self.base_url = "https://api.sleeper.app/v1"
        self.session = requests.Session()

    def _get(self, path: str) -> Optional[Dict]:
        """GET helper against the Sleeper API"""
        try:
            response = self.session.get(f"{self.base_url}{path}")
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            print(f"Error fetching {path}: {e}")
            return None

    def fetch_league(self) -> Dict:
        """Fetch league metadata, including the current draft_id"""
        return self._get(f"/league/{self.league_id}") or {}

    def fetch_draft(self, draft_id: str) -> Dict:
        """Fetch draft settings/status"""
        return self._get(f"/draft/{draft_id}") or {}

    def fetch_draft_picks(self, draft_id: str) -> List[Dict]:
        """Fetch all picks made so far in the draft"""
        return self._get(f"/draft/{draft_id}/picks") or []

    def fetch_all_players(self) -> Dict[str, Dict]:
        """
        Fetch the full NFL player pool, keyed by player_id.

        Sleeper asks that this (large, ~5MB) endpoint not be hit more than
        once per day, so results are cached to disk between runs.
        """
        if os.path.exists(PLAYERS_CACHE_PATH):
            age = time.time() - os.path.getmtime(PLAYERS_CACHE_PATH)
            if age < PLAYERS_CACHE_MAX_AGE_SECONDS:
                with open(PLAYERS_CACHE_PATH, "r") as f:
                    return json.load(f)

        players = self._get("/players/nfl") or {}
        if players:
            with open(PLAYERS_CACHE_PATH, "w") as f:
                json.dump(players, f)

        return players

    def get_drafted_player_ids(self, picks: List[Dict]) -> set:
        """Extract the set of already-drafted Sleeper player_ids"""
        return {pick.get("player_id") for pick in picks if pick.get("player_id")}

    def get_available_players(self, all_players: Dict[str, Dict], drafted: set) -> List[Dict]:
        """Get list of available (undrafted) players"""
        available = []

        for player_id, player in all_players.items():
            if player_id in drafted:
                continue

            # Skip players Sleeper has no fantasy relevance for
            if not player.get("fantasy_positions"):
                continue

            full_name = player.get("full_name") or f"{player.get('first_name', '')} {player.get('last_name', '')}".strip()

            available.append({
                "id": player_id,
                "name": full_name,
                "position": player.get("position", ""),
                "nfl_team": player.get("team") or "FA",
                "search_rank": player.get("search_rank"),
            })

        return available

    def get_draft_clock(self, draft: Dict, picks: List[Dict]) -> Dict:
        """Get current draft status (pick number, picks remaining, etc)"""
        settings = draft.get("settings", {})
        total_picks = settings.get("rounds", 0) * settings.get("teams", 0)
        picks_made = len(picks)

        return {
            "status": draft.get("status", "unknown"),
            "current_pick": picks_made + 1,
            "picks_remaining": max(0, total_picks - picks_made),
        }

    def get_effective_adp(self, player: Dict) -> float:
        """
        Normalize a player's search_rank into a usable ADP number.

        Sleeper uses 9999999 as a sentinel "unranked" value for players
        with no meaningful ADP (free agents, practice squad, etc), and
        omits search_rank entirely for others. Both cases are treated the
        same: pushed to the back of the pool instead of trusted at face
        value.
        """
        UNRANKED_SENTINEL = 9999999
        FALLBACK_ADP = 9999.0

        player_adp = player.get("search_rank")
        try:
            player_adp = float(player_adp)
            if player_adp >= UNRANKED_SENTINEL:
                player_adp = FALLBACK_ADP
        except (TypeError, ValueError):
            player_adp = FALLBACK_ADP

        return player_adp

    def score_player_value(self, player: Dict, drafted_count: int) -> float:
        """
        Simple value scoring: how much better is this player than what's expected?

        Uses Sleeper's search_rank (an overall ADP-style ranking) as the
        player's expected draft position.

        Args:
            player: Player dict with position, name, search_rank, etc
            drafted_count: How many players have been drafted so far

        Returns:
            Value score (higher = better value)
        """
        expected_adp = drafted_count + 1
        player_adp = self.get_effective_adp(player)

        # search_rank is an ADP-style rank where lower = better player.
        # Value = how far past their expected draft slot they've fallen,
        # i.e. how much better-ranked they are than the current pick count.
        return max(0.0, expected_adp - player_adp)

    def get_recommendations(self, available: List[Dict], drafted_count: int, top_n: int = 5) -> List[Dict]:
        """
        Get top value recommendations from available players

        Args:
            available: List of available players
            drafted_count: Players drafted so far
            top_n: How many recommendations to return

        Returns:
            Ranked list of recommended players
        """
        scored = []

        for player in available:
            score = self.score_player_value(player, drafted_count)
            scored.append({**player, "value_score": score})

        # Sort by value score (descending), breaking ties by ADP (ascending,
        # best player first) instead of leaving equally-scored players in
        # whatever arbitrary order Sleeper's API returned them in.
        scored.sort(key=lambda x: (-x["value_score"], self.get_effective_adp(x)))

        return scored[:top_n]

    def should_alert(self, value_score: float, threshold: float = 10.0) -> bool:
        """Determine if we should alert for this pick (high value threshold)"""
        return value_score > threshold

    def format_recommendation(self, player: Dict) -> str:
        """Format a player recommendation for display"""
        return (
            f"🎯 RECOMMENDATION: {player['name']} ({player['position']}) - "
            f"Value Score: {player['value_score']:.1f}"
        )

    def format_alert(self, player: Dict) -> str:
        """Format a high-value alert"""
        return (
            f"🚨 ALERT! Exceptional value: {player['name']} ({player['position']}) "
            f"still available! Value: {player['value_score']:.1f}"
        )


def run_analysis(league_id: str) -> Dict:
    """
    Main analysis function - run this each polling cycle

    Args:
        league_id: Sleeper league ID
    """
    analyzer = SleeperFantasyAnalyzer(league_id)

    league = analyzer.fetch_league()
    if not league:
        return {"error": "Could not fetch league data"}

    draft_id = league.get("draft_id")
    if not draft_id:
        return {"error": "League has no associated draft_id"}

    draft = analyzer.fetch_draft(draft_id)
    picks = analyzer.fetch_draft_picks(draft_id)
    all_players = analyzer.fetch_all_players()

    # Get drafted and available players
    drafted = analyzer.get_drafted_player_ids(picks)
    available = analyzer.get_available_players(all_players, drafted)

    # Get draft status
    status = analyzer.get_draft_clock(draft, picks)

    # Get recommendations
    recommendations = analyzer.get_recommendations(available, len(drafted))

    # Check for high-value alerts
    alerts = [r for r in recommendations if analyzer.should_alert(r["value_score"])]

    # Format output
    result = {
        "timestamp": datetime.now().isoformat(),
        "draft_status": {
            "status": status["status"],
            "picks_made": len(drafted),
            "picks_remaining": status["picks_remaining"],
            "current_pick": status["current_pick"],
        },
        "recommendations": [
            {
                "rank": i + 1,
                "name": r["name"],
                "position": r["position"],
                "nfl_team": r["nfl_team"],
                "value_score": round(r["value_score"], 2),
                "alert": analyzer.should_alert(r["value_score"])
            }
            for i, r in enumerate(recommendations[:5])
        ],
        "high_value_alerts": len(alerts),
    }

    return result


if __name__ == "__main__":
    # For testing: replace with your league ID
    league_id = os.getenv("SLEEPER_LEAGUE_ID", "YOUR_LEAGUE_ID_HERE")

    if league_id == "YOUR_LEAGUE_ID_HERE":
        print("ERROR: Set SLEEPER_LEAGUE_ID environment variable")
        exit(1)

    results = run_analysis(league_id)
    print(json.dumps(results, indent=2))
