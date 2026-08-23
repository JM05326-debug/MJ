# -*- coding: utf-8 -*-
"""One-time (not scheduled): reconstruct leak-free, point-in-time feature
vectors for every already-played historical game, so the very first
training run doesn't have to wait months for forward-collected data to
accumulate.

For each historical game on date D, Elo/Poisson ratings are recomputed
using ONLY games with date < D (never D itself or later) — exactly the
same no-leakage guarantee the live daily lock provides, just applied
retroactively. Starter/bullpen/handedness context is NOT reconstructable
historically (the pitcher fetchers only ever capture a rolling current
window, not point-in-time history for arbitrary past dates), so every
backfilled row calls build_context_features with pitchers=None — which,
via feature_spec.vector_from_feats, correctly imputes those specific
feature groups to a neutral value with their `_known` flag left at 0.
Forward-collected rows (predictions/*.jsonl once joined with results) have
those flags at 1 whenever the signal really was available at lock time.

Run this manually (`python pipeline/backfill_historical.py`), not on a
schedule — it's O(unique historical dates), a few minutes, and the output
doesn't change unless historical data itself is re-fetched.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
PIPELINE_DIR = ROOT / "pipeline"
for p in (SCRIPTS_DIR, PIPELINE_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import _console  # noqa: F401,E402
from model import compute_elo, compute_poisson_ratings, _parse_date  # noqa: E402
from context import build_context_features  # noqa: E402
from feature_spec import vector_from_feats, FEATURE_SPEC_VERSION  # noqa: E402
from generate_site import load_league_games  # noqa: E402
from game_id import game_id as make_game_id  # noqa: E402

DATASET_DIR = ROOT / "dataset"
LEAGUE_GAME_FILES = {"cpbl": "cpbl_data.json", "npb": "npb_data.json"}
MIN_PRIOR_GAMES = 30  # skip the very earliest games of the dataset — ratings would be nearly meaningless


def backfill_league(league: str) -> list[dict]:
    history, _ = load_league_games(LEAGUE_GAME_FILES[league])
    history = sorted(history, key=lambda g: g["date"])

    rows = []
    i = 0
    n = len(history)
    while i < n:
        d = history[i]["date"]
        # all games sharing this date, contiguous since history is sorted
        j = i
        todays_games = []
        while j < n and history[j]["date"] == d:
            todays_games.append(history[j])
            j += 1

        prior_games = history[:i]
        if len(prior_games) >= MIN_PRIOR_GAMES:
            elo_ratings, _ = compute_elo(prior_games)
            poisson = compute_poisson_ratings(prior_games, as_of=_parse_date(d))

            for g in todays_games:
                if g.get("hs") is None or g.get("vs") is None:
                    continue
                feats = build_context_features(
                    g["h"], g["v"], elo_ratings, poisson,
                    home_starter=None, away_starter=None,
                    pitchers=None, hand_map=None, vs_hand_splits=None,
                )
                vector = vector_from_feats(feats)
                rows.append({
                    "game_id": make_game_id(league, g),
                    "league": league,
                    "date": d,
                    "home": g["h"], "away": g["v"],
                    "features": vector,
                    "label_home_win": g["hs"] > g["vs"],
                    "actual_home_runs": g["hs"], "actual_away_runs": g["vs"],
                    "is_backfilled": True,
                    "feature_spec_version": FEATURE_SPEC_VERSION,
                })

        i = j

    return rows


def main():
    all_rows = []
    for league in LEAGUE_GAME_FILES:
        league_rows = backfill_league(league)
        print(f"[{league}] backfilled {len(league_rows)} rows")
        all_rows.extend(league_rows)

    DATASET_DIR.mkdir(exist_ok=True)
    out_path = DATASET_DIR / "backfilled_rows.jsonl"
    with open(out_path, "w", encoding="utf-8") as f:
        for row in all_rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"wrote {len(all_rows)} rows to {out_path}")


if __name__ == "__main__":
    main()
