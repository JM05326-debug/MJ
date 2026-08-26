# -*- coding: utf-8 -*-
"""Chronological train/eval split for model promotion decisions.

Deliberately NOT random k-fold: the operational question is "does this
model perform well on games later than what it trained on," which only a
time-based holdout answers. Two regimes, chosen automatically:

- Once there are enough forward-collected (real, live-locked) completed
  games, evaluate ONLY on those — they're what the system actually saw and
  acted on, the most representative sample of live performance.
- Before that (early on), fall back to a holdout carved from the combined
  backfill+forward dataset so there's still a meaningful-sized evaluation
  window instead of refusing to ever train.
"""
from __future__ import annotations

MIN_EVAL_GAMES = 150
EVAL_FRACTION = 0.20


def determine_eval_window(rows: list[dict]) -> tuple[list[dict], list[dict], dict]:
    """rows must already be sorted by date. Returns (train_rows, eval_rows, meta)."""
    forward_rows = [r for r in rows if not r["is_backfilled"]]

    if len(forward_rows) >= MIN_EVAL_GAMES:
        n_eval = max(MIN_EVAL_GAMES, round(len(forward_rows) * EVAL_FRACTION))
        eval_rows = forward_rows[-n_eval:]
        eval_start = eval_rows[0]["date"]
        train_rows = [r for r in rows if r["date"] < eval_start]
        window_type = "forward_only"
    else:
        n_eval = max(1, round(len(rows) * EVAL_FRACTION))
        eval_rows = rows[-n_eval:]
        eval_start = eval_rows[0]["date"] if eval_rows else None
        train_rows = rows[: len(rows) - n_eval]
        window_type = "backfill_holdout"

    meta = {
        "type": window_type,
        "start": eval_start,
        "end": rows[-1]["date"] if rows else None,
        "n_games": len(eval_rows),
        "n_train": len(train_rows),
        "n_forward_available": len(forward_rows),
    }
    return train_rows, eval_rows, meta


TEST_FRACTION = 0.30  # of eval_rows, carved off as the most-recent slice
MIN_TEST_GAMES = 20   # below this the test holdout isn't worth reporting on its own


def split_validation_test(eval_rows: list[dict]) -> tuple[list[dict], list[dict], dict]:
    """Further splits determine_eval_window()'s eval_rows (already sorted by
    date) into (validation_rows, test_rows) — validation is what
    validate_promote.py actually gates the promotion decision on, same as
    before this existed; test is the most-recent slice, held out from every
    promotion decision (not just this week's) so it stays a clean,
    never-touched-by-any-decision holdout for periodic honest reporting.
    Without this, re-evaluating champion-vs-challenger on the same rolling
    "recent window" every week is a mild multiple-comparisons risk — a model
    that happened to look good on this week's eval slice gets promoted,
    and next week's slice overlaps heavily with the one that approved it.
    A permanently-quarantined test tail doesn't fix that by itself, but it
    at least gives one number nothing was ever selected to look good on."""
    n_test = round(len(eval_rows) * TEST_FRACTION)
    if n_test < MIN_TEST_GAMES or len(eval_rows) - n_test < MIN_TEST_GAMES:
        return eval_rows, [], {"test_held_out": False, "reason": "eval_rows too small to carve out a test slice"}
    validation_rows = eval_rows[:-n_test]
    test_rows = eval_rows[-n_test:]
    meta = {
        "test_held_out": True,
        "validation": {"n": len(validation_rows), "start": validation_rows[0]["date"], "end": validation_rows[-1]["date"]},
        "test": {"n": len(test_rows), "start": test_rows[0]["date"], "end": test_rows[-1]["date"]},
    }
    return validation_rows, test_rows, meta
