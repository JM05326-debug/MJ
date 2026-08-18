# -*- coding: utf-8 -*-
"""Turn a model-registry entry into an actual prediction. Two model types
are supported today: 'deterministic' (the existing Elo+Poisson+context
blend, scripts/context.py — no artifact file, always available) and
'sklearn_logreg' (a saved scikit-learn classifier consuming the
feature_spec.FEATURE_KEYS vector, stacked on top of the deterministic
system's own outputs).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from context import contextual_predict_game  # noqa: E402
from feature_spec import FEATURE_KEYS, LeagueContext, build_feature_vector  # noqa: E402


def predict_with_model(model_entry: dict, home: str, away: str, home_starter, away_starter,
                        ctx: LeagueContext) -> dict:
    """Returns a dict with at minimum: home_win_pct, predicted_home_runs,
    predicted_away_runs, predicted_total_runs, confidence_score,
    confidence_label, context (raw signal notes) — regardless of which
    model type produced it, so callers (lock_predictions.py,
    validate_promote.py) don't need to branch on model type themselves."""
    fv = build_feature_vector(home, away, home_starter, away_starter, ctx)

    if model_entry["type"] == "deterministic":
        pred = contextual_predict_game(
            home, away, ctx.elo_ratings, ctx.poisson,
            home_starter=home_starter, away_starter=away_starter,
            pitchers=ctx.pitchers, relief_field=ctx.relief_field,
            hand_map=ctx.hand_map, vs_hand_splits=ctx.vs_hand_splits,
            fip_constant=ctx.fip_constant, league_era=ctx.league_era, league_avg_ip7=ctx.league_avg_ip7,
        )
        home_win_pct = pred["blended_home_win_pct"]
        confidence_score = pred["confidence_score"]
        confidence_label = pred["confidence_label"]
    elif model_entry["type"] == "sklearn_logreg":
        import joblib
        model = joblib.load(ROOT / model_entry["artifact_path"])
        x = [[fv["features"][k] for k in FEATURE_KEYS]]
        home_win_prob = float(model.predict_proba(x)[0][1])
        home_win_pct = home_win_prob * 100
        # confidence for an ML model: distance from 50/50, same spirit as
        # the deterministic model's edge term, kept simple and consistent.
        confidence_score = round(min(100.0, abs(home_win_prob - 0.5) * 200), 1)
        confidence_label = _confidence_label(confidence_score)
    else:
        raise ValueError(f"unknown model type: {model_entry['type']}")

    home_runs = fv["predicted_home_runs"]
    away_runs = fv["predicted_away_runs"]

    return {
        "model_version": model_entry["version"],
        "home_win_pct": round(home_win_pct, 1),
        "away_win_pct": round(100 - home_win_pct, 1),
        "predicted_home_runs": round(home_runs, 2),
        "predicted_away_runs": round(away_runs, 2),
        "predicted_total_runs": round(home_runs + away_runs, 2),
        "confidence_score": confidence_score,
        "confidence_label": confidence_label,
        "context_notes": fv["context_notes"],
        "feature_vector": fv["features"],
    }


def predict_home_win_prob_from_vector(model_entry: dict, feature_vector: dict) -> float:
    """Offline evaluation: given an already-computed, frozen feature vector
    (from a locked prediction record or a backfilled training row), return
    this model version's home-win probability — WITHOUT needing live team
    state (elo_ratings/poisson/pitchers as of some date), which historical
    rows don't retain. For the deterministic baseline this is exactly what
    contextual_predict_game does internally (average of the Elo and Poisson
    implied probabilities already stored in the vector); for a trained
    model it's model.predict_proba on the same fixed-order vector used at
    training time. Used by validate_promote.py to score champion AND
    challenger identically over a shared holdout set."""
    if model_entry["type"] == "deterministic":
        return (feature_vector["elo_p_home"] + feature_vector["poisson_p_home"]) / 2.0
    elif model_entry["type"] == "sklearn_logreg":
        import joblib
        model = joblib.load(ROOT / model_entry["artifact_path"])
        x = [[feature_vector[k] for k in FEATURE_KEYS]]
        return float(model.predict_proba(x)[0][1])
    raise ValueError(f"unknown model type: {model_entry['type']}")


CONFIDENCE_BANDS = ((70, "高信心"), (55, "中高"), (40, "中"), (25, "偏低"))


def _confidence_label(score: float) -> str:
    for threshold, name in CONFIDENCE_BANDS:
        if score >= threshold:
            return name
    return "不建議"
