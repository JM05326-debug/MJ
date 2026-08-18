# -*- coding: utf-8 -*-
"""Elo + Poisson prediction model shared by CPBL and NPB.

Approach
--------
1. Elo rating: chronological update over all historical games, with a
   home-field-advantage offset applied only to the win-probability
   expectation (not stored in the rating itself). Gives a single overall
   "team strength" power ranking and a logistic win probability.
2. Poisson scoring model: each team gets an offensive factor (runs scored
   relative to league average) and a defensive factor (runs allowed
   relative to league average), both exponentially recency-weighted so
   recent form matters more than results from years ago. These factors
   produce expected runs (lambda) for each side of a matchup, from which
   we derive a win/loss probability distribution and a most-likely score.
3. Final predicted win probability blends the two methods (equal weight)
   since they capture different signal (Elo = overall strength trend,
   Poisson = current scoring form).
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, datetime

ELO_INITIAL = 1500.0
ELO_K = 20.0
ELO_HOME_ADV = 50.0  # rating points added to home team for expectation only

POISSON_HALF_LIFE_DAYS = 180.0
POISSON_HOME_ADV = 1.08  # multiplicative bump to home expected runs
MAX_RUNS = 15  # truncate Poisson sum for win-prob computation


def _parse_date(d: str) -> date:
    return datetime.strptime(d, "%Y-%m-%d").date()


def compute_elo(games: list[dict]) -> tuple[dict[str, float], dict[str, list]]:
    """Chronologically replay games and return final ratings + per-team history."""
    games_sorted = sorted(games, key=lambda g: g["date"])
    ratings: dict[str, float] = defaultdict(lambda: ELO_INITIAL)
    history: dict[str, list] = defaultdict(list)

    for g in games_sorted:
        h, v = g["h"], g["v"]
        hs, vs = g["hs"], g["vs"]
        if hs == vs:
            score_h = 0.5
        elif hs > vs:
            score_h = 1.0
        else:
            score_h = 0.0

        rh, rv = ratings[h], ratings[v]
        exp_h = 1.0 / (1.0 + 10 ** (-((rh + ELO_HOME_ADV) - rv) / 400.0))
        delta = ELO_K * (score_h - exp_h)
        ratings[h] = rh + delta
        ratings[v] = rv - delta

        history[h].append((g["date"], ratings[h]))
        history[v].append((g["date"], ratings[v]))

    return dict(ratings), dict(history)


def compute_poisson_ratings(games: list[dict], as_of: date | None = None):
    """Recency-weighted offensive/defensive scoring factors per team."""
    if as_of is None:
        as_of = max(_parse_date(g["date"]) for g in games)

    weighted_runs_for = defaultdict(float)
    weighted_runs_against = defaultdict(float)
    weighted_games = defaultdict(float)
    total_weighted_runs = 0.0
    total_weighted_team_games = 0.0

    for g in games:
        gdate = _parse_date(g["date"])
        days_ago = max((as_of - gdate).days, 0)
        w = 0.5 ** (days_ago / POISSON_HALF_LIFE_DAYS)

        h, v, hs, vs = g["h"], g["v"], g["hs"], g["vs"]

        weighted_runs_for[h] += hs * w
        weighted_runs_against[h] += vs * w
        weighted_games[h] += w

        weighted_runs_for[v] += vs * w
        weighted_runs_against[v] += hs * w
        weighted_games[v] += w

        total_weighted_runs += (hs + vs) * w
        total_weighted_team_games += 2 * w

    league_avg_runs = total_weighted_runs / total_weighted_team_games

    offense: dict[str, float] = {}
    defense: dict[str, float] = {}
    for team, wg in weighted_games.items():
        if wg <= 0:
            continue
        avg_scored = weighted_runs_for[team] / wg
        avg_allowed = weighted_runs_against[team] / wg
        offense[team] = avg_scored / league_avg_runs
        defense[team] = avg_allowed / league_avg_runs

    return {
        "offense": offense,
        "defense": defense,
        "league_avg_runs": league_avg_runs,
    }


def _poisson_pmf(k: int, lam: float) -> float:
    return math.exp(-lam) * lam**k / math.factorial(k)


def poisson_win_prob(lam_home: float, lam_away: float) -> tuple[float, float, float]:
    """Return (home_win_prob, tie_prob, away_win_prob) from independent Poisson dists."""
    home_win = tie = away_win = 0.0
    home_pmf = [_poisson_pmf(i, lam_home) for i in range(MAX_RUNS + 1)]
    away_pmf = [_poisson_pmf(j, lam_away) for j in range(MAX_RUNS + 1)]
    for i in range(MAX_RUNS + 1):
        for j in range(MAX_RUNS + 1):
            p = home_pmf[i] * away_pmf[j]
            if i > j:
                home_win += p
            elif i == j:
                tie += p
            else:
                away_win += p
    # Renormalize the small truncated tail away, then split ties
    # (baseball has extra innings, so ties resolve ~50/50 in practice).
    total = home_win + tie + away_win
    home_win, tie, away_win = home_win / total, tie / total, away_win / total
    home_final = home_win + tie * 0.5
    away_final = away_win + tie * 0.5
    return home_final, tie, away_final


def elo_win_prob(rating_home: float, rating_away: float) -> float:
    return 1.0 / (1.0 + 10 ** (-((rating_home + ELO_HOME_ADV) - rating_away) / 400.0))


def predict_game(home: str, away: str, elo_ratings: dict, poisson: dict) -> dict:
    rh = elo_ratings.get(home, ELO_INITIAL)
    rv = elo_ratings.get(away, ELO_INITIAL)
    elo_p_home = elo_win_prob(rh, rv)

    league_avg = poisson["league_avg_runs"]
    off_h = poisson["offense"].get(home, 1.0)
    def_h = poisson["defense"].get(home, 1.0)
    off_v = poisson["offense"].get(away, 1.0)
    def_v = poisson["defense"].get(away, 1.0)

    lam_home = league_avg * off_h * def_v * POISSON_HOME_ADV
    lam_away = league_avg * off_v * def_h

    poi_p_home, poi_tie, poi_p_away = poisson_win_prob(lam_home, lam_away)

    blended_home = (elo_p_home + poi_p_home) / 2.0

    return {
        "home": home,
        "away": away,
        "elo_home_rating": round(rh, 1),
        "elo_away_rating": round(rv, 1),
        "elo_home_win_pct": round(elo_p_home * 100, 1),
        "poisson_home_win_pct": round(poi_p_home * 100, 1),
        "blended_home_win_pct": round(blended_home * 100, 1),
        "blended_away_win_pct": round((1 - blended_home) * 100, 1),
        "predicted_home_runs": round(lam_home, 2),
        "predicted_away_runs": round(lam_away, 2),
    }
