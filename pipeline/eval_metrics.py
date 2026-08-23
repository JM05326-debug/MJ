# -*- coding: utf-8 -*-
"""Probabilistic-forecast evaluation metrics, computed identically for the
champion and any challenger so comparisons are apples-to-apples."""
from __future__ import annotations

import numpy as np
from sklearn.calibration import calibration_curve
from sklearn.metrics import accuracy_score, brier_score_loss, log_loss, roc_auc_score


CONFIDENCE_BUCKETS = [(0.50, 0.55), (0.55, 0.60), (0.60, 0.65), (0.65, 0.70), (0.70, 1.0001)]


def confidence_bucket_calibration(y_true: list[bool], y_prob: list[float]) -> list[dict]:
    """Bucket games by how confident the model was in its actual pick (the
    probability it gave the side it favored, i.e. max(p, 1-p)) into
    50-55/55-60/60-65/65-70/70%+, and check whether the pick's actual win
    rate in each bucket matches the stated probability — the sports-betting
    framing of calibration, as opposed to compute_metrics()'s home-win-prob
    calibration_curve (which is finer-grained but not anchored at 50%)."""
    y_true_arr = np.array(y_true, dtype=int)
    y_prob_arr = np.array(y_prob, dtype=float)
    pick_prob = np.where(y_prob_arr >= 0.5, y_prob_arr, 1 - y_prob_arr)
    pick_won = np.where(y_prob_arr >= 0.5, y_true_arr == 1, y_true_arr == 0)

    buckets = []
    for lo, hi in CONFIDENCE_BUCKETS:
        mask = (pick_prob >= lo) & (pick_prob < hi)
        n = int(mask.sum())
        label = f"{int(round(lo * 100))}%+" if hi > 1 else f"{int(round(lo * 100))}-{int(round(hi * 100))}%"
        if n == 0:
            buckets.append({"range": label, "n": 0, "predicted_prob_mean": None, "actual_win_rate": None})
            continue
        buckets.append({
            "range": label,
            "n": n,
            "predicted_prob_mean": round(float(pick_prob[mask].mean()), 3),
            "actual_win_rate": round(float(pick_won[mask].mean()), 3),
        })
    return buckets


def compute_metrics(y_true: list[bool], y_prob: list[float]) -> dict:
    y_true_arr = np.array(y_true, dtype=int)
    y_prob_arr = np.clip(np.array(y_prob, dtype=float), 1e-6, 1 - 1e-6)  # log_loss blows up at exactly 0/1

    metrics = {
        "n": len(y_true),
        "log_loss": round(float(log_loss(y_true_arr, y_prob_arr, labels=[0, 1])), 4),
        "brier": round(float(brier_score_loss(y_true_arr, y_prob_arr)), 4),
        "accuracy": round(float(accuracy_score(y_true_arr, y_prob_arr >= 0.5)), 4),
        "confidence_buckets": confidence_bucket_calibration(y_true, y_prob),
    }

    if len(set(y_true_arr.tolist())) > 1:
        metrics["auc"] = round(float(roc_auc_score(y_true_arr, y_prob_arr)), 4)
        frac_pos, mean_pred = calibration_curve(y_true_arr, y_prob_arr, n_bins=10, strategy="uniform")
        metrics["calibration_bins"] = [
            {"mean_predicted": round(float(p), 3), "fraction_positive": round(float(f), 3)}
            for p, f in zip(mean_pred, frac_pos)
        ]
    else:
        # a degenerate eval slice (all-wins or all-losses) — AUC/calibration
        # are undefined, not zero; say so rather than silently omitting them
        metrics["auc"] = None
        metrics["calibration_bins"] = None

    return metrics


def compute_roi(y_true: list[bool], y_prob: list[float], market_odds: list[dict | None]) -> dict:
    """EV-gated flat-stake ROI, recomputed against THIS model's own
    probabilities (unlike pipeline/build_dashboard.py's `_roi()`, which
    reports ROI from whatever model's `ev` was actually locked in at the
    time — this one asks "if this candidate model's probabilities had been
    used to decide which bets clear EV>0, how would it have done" so
    champion and challenger are compared fairly on the same odds. Reporting
    only, same as the dashboard's version — never used to gate promotion
    (see validate_promote.py; log_loss is the only promotion criterion)."""
    staked = 0.0
    profit = 0.0
    n_games = 0
    for actual_home_win, prob_home, odds in zip(y_true, y_prob, market_odds):
        if not odds:
            continue
        n_games += 1
        home_ml, away_ml = odds.get("home_moneyline"), odds.get("away_moneyline")
        if home_ml:
            home_ev = prob_home * home_ml - 1
            if home_ev > 0:
                staked += 1
                profit += (home_ml - 1) if actual_home_win else -1
        if away_ml:
            away_ev = (1 - prob_home) * away_ml - 1
            if away_ev > 0:
                staked += 1
                profit += (away_ml - 1) if not actual_home_win else -1
    if staked == 0:
        return {"n_games": n_games, "n_bets": 0, "roi_pct": None}
    return {"n_games": n_games, "n_bets": int(staked), "roi_pct": round(profit / staked * 100, 1)}


def is_degenerate(y_prob: list[float], epsilon: float = 0.01) -> bool:
    """A model that outputs (near-)constant probabilities regardless of
    matchup is almost certainly a broken training run, not a real model —
    reject it before it's even eligible for promotion comparison."""
    arr = np.array(y_prob, dtype=float)
    return float(np.std(arr)) < epsilon
