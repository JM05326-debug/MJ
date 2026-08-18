# -*- coding: utf-8 -*-
"""Canonical feature-vector definition, shared by the live daily-lock path,
the historical backfill, and model training — this is what prevents
training/serving skew: there is exactly one function
(`context.build_context_features`) that turns a matchup into raw signals,
and exactly one function here (`build_feature_vector`) that turns those
raw signals into the fixed-order numeric vector a classifier consumes.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from model import compute_elo, compute_poisson_ratings  # noqa: E402
from context import build_context_features, build_vs_hand_splits, league_fip_constant, league_avg_bullpen_ip_7d  # noqa: E402
from generate_site import load_league_games, load_players, load_odds, TEAM_ALIAS  # noqa: E402

# Fixed order — train_model.py, backfill_historical.py, and lock_predictions.py
# must all use this exact list so a saved model's coefficients always line
# up with the right column.
FEATURE_KEYS = [
    "elo_p_home", "poisson_p_home", "elo_rating_diff",
    "lam_home", "lam_away",
    "home_pitcher_factor", "home_pitcher_factor_known",
    "away_pitcher_factor", "away_pitcher_factor_known",
    "home_bullpen_factor", "home_bullpen_factor_known",
    "away_bullpen_factor", "away_bullpen_factor_known",
    "home_hand_factor", "home_hand_factor_known",
    "away_hand_factor", "away_hand_factor_known",
]

LEAGUES = {
    "cpbl": dict(game_file="cpbl_data.json", pitchers_file="cpbl_pitchers.json",
                 batters_file="cpbl_batters.json", relief_field="relief_season",
                 odds_file="cpbl_odds.json"),
    "npb": dict(game_file="npb_data.json", pitchers_file="npb_pitchers.json",
                batters_file="npb_batters.json", relief_field="relief_recent_window",
                odds_file=None),
}


class LeagueContext:
    """Everything needed to compute a prediction for any matchup in this
    league, as of a given date. Building this once per run and reusing it
    across every game that day/date is what keeps backfill fast (Elo/Poisson
    replay is O(games), not O(games^2))."""

    def __init__(self, league: str, history: list, elo_ratings: dict, poisson: dict,
                 pitchers: dict, batters: dict, hand_map: dict, vs_hand_splits: dict,
                 fip_constant: float, league_era: float, league_avg_ip7: float,
                 odds_map: dict):
        self.league = league
        self.history = history
        self.elo_ratings = elo_ratings
        self.poisson = poisson
        self.pitchers = pitchers
        self.batters = batters
        self.hand_map = hand_map
        self.vs_hand_splits = vs_hand_splits
        self.fip_constant = fip_constant
        self.league_era = league_era
        self.league_avg_ip7 = league_avg_ip7
        self.odds_map = odds_map
        self.relief_field = LEAGUES[league]["relief_field"]


def load_live_context(league: str, today: date | None = None) -> LeagueContext:
    """Load today's *current* data files (as freshly fetched) and build the
    live context used to lock today's predictions. `pitchers`/`batters`
    reflect only-current-window data (this is the live system's normal,
    accepted limitation — see README) so this is NOT used for historical
    backfill, which must reconstruct point-in-time ratings instead (see
    backfill_historical.py)."""
    cfg = LEAGUES[league]
    today = today or date.today()
    history, upcoming = load_league_games(cfg["game_file"])
    pitchers, batters = load_players(cfg["pitchers_file"], cfg["batters_file"])
    odds_map = load_odds(cfg["odds_file"]) if cfg["odds_file"] else {}

    elo_ratings, _ = compute_elo(history)
    poisson = compute_poisson_ratings(history, as_of=today)
    hand_map = {n: p.get("hand", "") for n, p in pitchers.items()}
    vs_hand_splits = build_vs_hand_splits(history, hand_map)
    fip_constant, league_era = league_fip_constant(pitchers)
    league_avg_ip7 = league_avg_bullpen_ip_7d(pitchers)

    return LeagueContext(
        league, history, elo_ratings, poisson, pitchers, batters,
        hand_map, vs_hand_splits, fip_constant, league_era, league_avg_ip7, odds_map,
    ), upcoming


def vector_from_feats(feats: dict) -> dict:
    """Turn build_context_features()'s raw output into the fixed-order,
    fully-numeric FEATURE_KEYS vector (missing signals imputed to a neutral
    1.0 with a paired `_known` flag). Shared by the live path
    (build_feature_vector, below, via a LeagueContext) and
    backfill_historical.py (which calls build_context_features directly
    per-date with pitchers=None, since point-in-time pitcher data isn't
    reconstructable historically) — this is the one place imputation rules
    live, so both paths can never drift apart."""
    notes = feats["context"]

    def _known(v):
        return 1.0 if v is not None else 0.0

    def _imputed(v, default=1.0):
        return float(v) if v is not None else default

    vector = {
        "elo_p_home": feats["elo_p_home"],
        "poisson_p_home": feats["poisson_p_home"],
        "elo_rating_diff": feats["elo_home_rating"] - feats["elo_away_rating"],
        "lam_home": feats["lam_home"],
        "lam_away": feats["lam_away"],
        "home_pitcher_factor": _imputed(notes["home_pitcher_factor"]),
        "home_pitcher_factor_known": _known(notes["home_pitcher_factor"]),
        "away_pitcher_factor": _imputed(notes["away_pitcher_factor"]),
        "away_pitcher_factor_known": _known(notes["away_pitcher_factor"]),
        "home_bullpen_factor": _imputed(notes["home_bullpen_factor"]),
        "home_bullpen_factor_known": _known(notes["home_bullpen_factor"]),
        "away_bullpen_factor": _imputed(notes["away_bullpen_factor"]),
        "away_bullpen_factor_known": _known(notes["away_bullpen_factor"]),
        "home_hand_factor": _imputed(notes["home_hand_factor"]),
        "home_hand_factor_known": _known(notes["home_hand_factor"]),
        "away_hand_factor": _imputed(notes["away_hand_factor"]),
        "away_hand_factor_known": _known(notes["away_hand_factor"]),
    }
    assert list(vector.keys()) == FEATURE_KEYS, "feature order drifted from FEATURE_KEYS"
    return vector


def build_feature_vector(home: str, away: str, home_starter: str | None, away_starter: str | None,
                          ctx: LeagueContext) -> dict:
    """Returns {feature_name: float} for every key in FEATURE_KEYS, plus the
    raw `context_notes` and `predicted_*` values for display/EV purposes."""
    feats = build_context_features(
        home, away, ctx.elo_ratings, ctx.poisson,
        home_starter=home_starter, away_starter=away_starter,
        pitchers=ctx.pitchers, relief_field=ctx.relief_field,
        hand_map=ctx.hand_map, vs_hand_splits=ctx.vs_hand_splits,
        fip_constant=ctx.fip_constant, league_era=ctx.league_era, league_avg_ip7=ctx.league_avg_ip7,
    )
    vector = vector_from_feats(feats)

    return {
        "features": vector,
        "context_notes": feats["context"],
        "elo_home_win_pct": feats["elo_home_win_pct"],
        "poisson_home_win_pct": feats["poisson_home_win_pct"],
        "predicted_home_runs": feats["lam_home"],
        "predicted_away_runs": feats["lam_away"],
    }
