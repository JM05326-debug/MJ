# -*- coding: utf-8 -*-
"""Merge backfilled historical rows + forward-collected (prediction ⋈
result) rows into dataset/training_rows.jsonl, the single input
train_model.py reads. Re-run any time (weekly, before training) — it's a
pure derived view, never a source of truth itself (predictions/*.jsonl,
results/*.jsonl, and dataset/backfilled_rows.jsonl are the sources).
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

DATASET_DIR = ROOT / "dataset"
PREDICTIONS_DIR = ROOT / "predictions"
RESULTS_DIR = ROOT / "results"
LEAGUES = ("cpbl", "npb")


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def forward_rows() -> list[dict]:
    rows = []
    for league in LEAGUES:
        predictions = {p["game_id"]: p for p in _read_jsonl(PREDICTIONS_DIR / f"{league}_predictions_log.jsonl")}
        results = _read_jsonl(RESULTS_DIR / f"{league}_results_log.jsonl")
        for r in results:
            if r["status"] != "final":
                continue  # voided/unresolved games never enter training
            p = predictions.get(r["game_id"])
            if not p or "feature_vector" not in p:
                continue  # e.g. a manually-inserted or malformed prediction row; skip rather than guess
            rows.append({
                "game_id": r["game_id"], "league": league, "date": p["date"],
                "home": p["home"], "away": p["away"],
                "features": p["feature_vector"],
                "label_home_win": r["actual_home_win"],
                "actual_home_runs": r["actual_home_runs"], "actual_away_runs": r["actual_away_runs"],
                "is_backfilled": False,
                "model_version_at_lock": p.get("model_version"),
                # absent on predictions locked before this field existed —
                # never backfilled onto those, they're immutable once locked
                "feature_spec_version": p.get("feature_spec_version"),
            })
    return rows


def backfilled_rows() -> list[dict]:
    return _read_jsonl(DATASET_DIR / "backfilled_rows.jsonl")


def main():
    fwd = forward_rows()
    bf = backfilled_rows()

    # forward rows take precedence over a backfilled row with the same
    # game_id (shouldn't normally overlap in practice, since backfill only
    # covers games that existed before this pipeline went live, but be safe)
    fwd_ids = {r["game_id"] for r in fwd}
    bf = [r for r in bf if r["game_id"] not in fwd_ids]

    all_rows = bf + fwd
    all_rows.sort(key=lambda r: r["date"])

    out_path = DATASET_DIR / "training_rows.jsonl"
    with open(out_path, "w", encoding="utf-8") as f:
        for row in all_rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"training_rows.jsonl: {len(bf)} backfilled + {len(fwd)} forward = {len(all_rows)} total")


if __name__ == "__main__":
    main()
