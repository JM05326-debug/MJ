# -*- coding: utf-8 -*-
"""Weekly job (step 1 of 2, see validate_promote.py): train a challenger
model on everything before the evaluation window, and stage it — this
script never touches models/registry.json or current_production itself;
it only ever writes to a staging path that validate_promote.py reads next.
If training itself throws, that's treated as a real bug (non-zero exit,
should show red in Actions), unlike "not enough data yet" which is a
normal, logged, zero-exit outcome.

Model: a stacked LogisticRegression — elo_p_home/poisson_p_home/lam_home/
lam_away (the existing deterministic system's own outputs) are themselves
input features here, alongside the starter/bullpen/handedness factors and
their _known flags. This lets the classifier learn a correction on top of
an already-reasonable prior instead of relearning team strength from a
still-small forward-labeled sample, and keeps every model version
comparable on the same underlying signals.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
PIPELINE_DIR = ROOT / "pipeline"
for p in (SCRIPTS_DIR, PIPELINE_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import _console  # noqa: F401,E402
from feature_spec import FEATURE_KEYS  # noqa: E402
from eval_split import determine_eval_window  # noqa: E402

DATASET_DIR = ROOT / "dataset"
STAGING_DIR = ROOT / "models" / "_staging"
MIN_TRAIN_ROWS = 50


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


def _write_status(outcome: str, detail: str | None):
    status = {
        "last_run_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "outcome": outcome, "detail": detail,
    }
    with open(ROOT / "models" / "training_status.json", "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)


def main():
    rows = _read_jsonl(DATASET_DIR / "training_rows.jsonl")
    rows.sort(key=lambda r: r["date"])

    if not rows:
        print("no training data at all — run backfill_historical.py + build_dataset.py first")
        _write_status("跳過：無訓練資料", "尚未執行過 backfill_historical.py / build_dataset.py")
        return

    train_rows, eval_rows, meta = determine_eval_window(rows)
    print(f"eval window: {meta}")

    if len(train_rows) < MIN_TRAIN_ROWS:
        print(f"only {len(train_rows)} training rows (< {MIN_TRAIN_ROWS}) — skipping this week, not an error")
        _write_status("跳過：資料量不足", f"僅有 {len(train_rows)} 筆可訓練資料（需要至少 {MIN_TRAIN_ROWS} 筆）")
        return

    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    x_train = [[r["features"][k] for k in FEATURE_KEYS] for r in train_rows]
    y_train = [1 if r["label_home_win"] else 0 for r in train_rows]

    model = Pipeline([
        ("scale", StandardScaler()),
        ("clf", LogisticRegression(C=1.0, max_iter=1000)),
    ])
    model.fit(x_train, y_train)

    import joblib
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    model_path = STAGING_DIR / "model.joblib"
    joblib.dump(model, model_path)

    meta_out = {
        "trained_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "training_row_count": len(train_rows),
        "feature_list": FEATURE_KEYS,
        "eval_window": meta,
    }
    with open(STAGING_DIR / "meta.json", "w", encoding="utf-8") as f:
        json.dump(meta_out, f, ensure_ascii=False, indent=2)

    print(f"trained challenger on {len(train_rows)} rows, staged at {model_path}")
    print("run validate_promote.py next to evaluate and (maybe) promote")


if __name__ == "__main__":
    main()
