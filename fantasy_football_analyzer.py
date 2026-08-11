#!/usr/bin/env python3
"""
Fantasy Football Draft Decision Assistant
Analyzes live ESPN draft and recommends high-value picks
"""

import requests
import json
import os
from datetime import datetime
from typing import Dict, List, Tuple

class ESPNFantasyAnalyzer:
    def __init__(self, league_id: str, year: int = 2024):
        """
        Initialize ESPN Fantasy Football analyzer
        
        Args:
            league_id: Your ESPN league ID (visible in URL)
            year: NFL season year
        """
        self.league_id = league_id
        self.year = year
        self.base_url = f"https://lm-api-reads.platform.espn.com/apis/site/epin/v2/static/football/leagueV3/{league_id}"
        self.session = requests.Session()
        
    def fetch_draft_data(self) -> Dict:
        """Fetch current draft state from ESPN"""
        try:
            response = self.session.get(self.base_url)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            print(f"Error fetching draft data: {e}")
            return {}
    
    def get_drafted_players(self, draft_data: Dict) -> set:
        """Extract list of already-drafted players"""
        drafted = set()
        
        # Navigate the ESPN API structure for draft picks
        if "members" in draft_data:
            for member in draft_data["members"]:
                if "playerList" in member:
                    for pick in member["playerList"]:
                        drafted.add(pick.get("playerId"))
        
        return drafted
    
    def get_available_players(self, draft_data: Dict, drafted: set) -> List[Dict]:
        """Get list of available (undrafted) players"""
        available = []
        
        if "players" not in draft_data:
            return available
        
        for player in draft_data["players"]:
            player_id = player.get("playerId")
            if player_id not in drafted:
                available.append({
                    "id": player_id,
                    "name": player.get("displayName", "Unknown"),
                    "position": player.get("defaultPosition", ""),
                    "nfl_team": player.get("proTeam", ""),
                    "adp": player.get("ranking", {}),
                })
        
        return available
    
    def get_draft_clock(self, draft_data: Dict) -> Dict:
        """Get current draft status (whose turn, pick number, etc)"""
        status = {
            "current_pick": 0,
            "current_team": "Unknown",
            "picks_remaining": 0
        }
        
        if "draftDetail" in draft_data:
            draft = draft_data["draftDetail"]
            status["current_pick"] = draft.get("currentPick", 0)
            status["picks_remaining"] = draft.get("playerCount", 0) - draft.get("pickedCount", 0)
        
        return status
    
    def score_player_value(self, player: Dict, drafted_count: int, league_size: int = 12) -> float:
        """
        Simple value scoring: how much better is this player than what's expected?
        
        Args:
            player: Player dict with position, name, etc
            drafted_count: How many players have been drafted so far
            league_size: League size (default 12)
        
        Returns:
            Value score (higher = better value)
        """
        # Expected ADP at this point in draft
        expected_adp = drafted_count + 1
        
        # Get player's actual ADP (default to high number if not available)
        player_adp = player.get("adp", {}).get("auctionValue", 100)
        
        if isinstance(player_adp, dict):
            player_adp = player_adp.get("value", 100)
        
        try:
            player_adp = float(player_adp)
        except (TypeError, ValueError):
            player_adp = 100
        
        # Value = how much earlier we're getting them than expected
        value_score = max(0, player_adp - expected_adp)
        
        return value_score
    
    def get_recommendations(self, available: List[Dict], drafted_count: int, 
                           top_n: int = 5, league_size: int = 12) -> List[Dict]:
        """
        Get top value recommendations from available players
        
        Args:
            available: List of available players
            drafted_count: Players drafted so far
            top_n: How many recommendations to return
            league_size: League size for calculations
        
        Returns:
            Ranked list of recommended players
        """
        scored = []
        
        for player in available:
            score = self.score_player_value(player, drafted_count, league_size)
            scored.append({
                **player,
                "value_score": score
            })
        
        # Sort by value score (descending)
        scored.sort(key=lambda x: x["value_score"], reverse=True)
        
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


def run_analysis(league_id: str, league_size: int = 12):
    """
    Main analysis function - run this each polling cycle
    
    Args:
        league_id: ESPN league ID
        league_size: Your league size
    """
    analyzer = ESPNFantasyAnalyzer(league_id)
    
    # Fetch current draft state
    draft_data = analyzer.fetch_draft_data()
    if not draft_data:
        return {"error": "Could not fetch draft data"}
    
    # Get drafted and available players
    drafted = analyzer.get_drafted_players(draft_data)
    available = analyzer.get_available_players(draft_data, drafted)
    
    # Get draft status
    status = analyzer.get_draft_clock(draft_data)
    
    # Get recommendations
    recommendations = analyzer.get_recommendations(available, len(drafted), league_size=league_size)
    
    # Check for high-value alerts
    alerts = [r for r in recommendations if analyzer.should_alert(r["value_score"])]
    
    # Format output
    result = {
        "timestamp": datetime.now().isoformat(),
        "draft_status": {
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
    league_id = os.getenv("ESPN_LEAGUE_ID", "YOUR_LEAGUE_ID_HERE")
    
    if league_id == "YOUR_LEAGUE_ID_HERE":
        print("ERROR: Set ESPN_LEAGUE_ID environment variable")
        exit(1)
    
    results = run_analysis(league_id)
    print(json.dumps(results, indent=2))