# -*- coding: utf-8 -*-
"""Probabilistic-forecast evaluation metrics, computed identically for the
champion and any challenger so comparisons are apples-to-apples."""
from __future__ import annotations

import numpy as np
from sklearn.calibration import calibration_curve
from sklearn.metrics import accuracy_score, brier_score_loss, log_loss, roc_auc_score


def compute_metrics(y_true: list[bool], y_prob: list[float]) -> dict:
    y_true_arr = np.array(y_true, dtype=int)
    y_prob_arr = np.clip(np.array(y_prob, dtype=float), 1e-6, 1 - 1e-6)  # log_loss blows up at exactly 0/1

    metrics = {
        "n": len(y_true),
        "log_loss": round(float(log_loss(y_true_arr, y_prob_arr, labels=[0, 1])), 4),
        "brier": round(float(brier_score_loss(y_true_arr, y_prob_arr)), 4),
        "accuracy": round(float(accuracy_score(y_true_arr, y_prob_arr >= 0.5)), 4),
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


def is_degenerate(y_prob: list[float], epsilon: float = 0.01) -> bool:
    """A model that outputs (near-)constant probabilities regardless of
    matchup is almost certainly a broken training run, not a real model —
    reject it before it's even eligible for promotion comparison."""
    arr = np.array(y_prob, dtype=float)
    return float(np.std(arr)) < epsilon
