# -*- coding: utf-8 -*-
"""Weekly job (step 2 of 2, after train_model.py): evaluate the staged
challenger against the current production model on a shared chronological
holdout, and promote it ONLY if it clearly beats production on Log Loss
(the primary metric — a proper scoring rule, sensitive to calibration,
which matters most for a system whose job is producing probabilities an
EV calculator consumes). Every outcome — promoted or rejected — is
permanently recorded in models/registry.json; only a genuine promotion
changes current_production. A mid-run crash here must never leave
registry.json partially written, so it's only saved once, at the very end,
after every metric has successfully computed.
"""
from __future__ import annotations

import json
import shutil
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
from eval_split import determine_eval_window, split_validation_test  # noqa: E402
from eval_metrics import compute_metrics, compute_roi, is_degenerate  # noqa: E402
from predictor import predict_home_win_prob_from_vector, MODEL_TYPE_NAMES  # noqa: E402
import registry as registry_mod  # noqa: E402

DATASET_DIR = ROOT / "dataset"
STAGING_DIR = ROOT / "models" / "_staging"
MODELS_DIR = ROOT / "models"

LOG_LOSS_MARGIN = 0.005  # challenger must beat champion by at least this much (proper-scoring-rule units)
MIN_EVAL_FOR_PROMOTION = 30  # below this, even a "win" isn't trustworthy enough to act on


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
    with open(MODELS_DIR / "training_status.json", "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)


def _next_version(reg: dict) -> str:
    n = sum(1 for m in reg["models"] if m["type"] == "sklearn_logreg") + 1
    now = datetime.now(timezone.utc)
    return f"v{n:04d}_logreg_{now.strftime('%Ywk%V')}"


def main():
    meta_path = STAGING_DIR / "meta.json"
    model_path = STAGING_DIR / "model.joblib"
    if not meta_path.exists() or not model_path.exists():
        print("no staged challenger found — run train_model.py first")
        _write_status("跳過：無待驗證模型", "train_model.py 尚未產生 staging 模型")
        return

    with open(meta_path, encoding="utf-8") as f:
        stage_meta = json.load(f)

    rows = _read_jsonl(DATASET_DIR / "training_rows.jsonl")
    rows.sort(key=lambda r: r["date"])
    _, eval_rows, eval_meta = determine_eval_window(rows)

    # eval_rows splits further into validation (what actually gates
    # promotion, same role eval_rows played before this split existed) and
    # test (the most-recent slice, held out from every promotion decision —
    # never just this week's — so it stays a clean holdout nothing was ever
    # selected to look good on; see eval_split.split_validation_test).
    validation_rows, test_rows, split_meta = split_validation_test(eval_rows)

    if len(validation_rows) < MIN_EVAL_FOR_PROMOTION:
        print(f"validation window only has {len(validation_rows)} games (< {MIN_EVAL_FOR_PROMOTION}) — staying on champion")
        _write_status("跳過：驗證樣本不足", f"驗證集僅 {len(validation_rows)} 場（需要至少 {MIN_EVAL_FOR_PROMOTION} 場）")
        return

    reg = registry_mod.load_registry()
    reg = registry_mod.ensure_baseline_registered(reg)
    champion_entry = registry_mod.get_production(reg)
    challenger_entry = {"type": "sklearn_logreg", "artifact_path": model_path.relative_to(ROOT).as_posix()}

    y_true = [bool(r["label_home_win"]) for r in validation_rows]
    champion_probs = [predict_home_win_prob_from_vector(champion_entry, r["features"]) for r in validation_rows]
    challenger_probs = [predict_home_win_prob_from_vector(challenger_entry, r["features"]) for r in validation_rows]

    market_odds = [r.get("market_odds") for r in validation_rows]
    champion_metrics = compute_metrics(y_true, champion_probs)
    challenger_metrics = compute_metrics(y_true, challenger_probs)
    champion_metrics["roi"] = compute_roi(y_true, champion_probs, market_odds)
    challenger_metrics["roi"] = compute_roi(y_true, challenger_probs, market_odds)
    print(f"champion  ({champion_entry['version']}): {champion_metrics}")
    print(f"challenger (staged): {challenger_metrics}")

    # Test holdout: reporting only, never gates anything below. Skipped
    # gracefully (empty dict) when there isn't enough data yet to carve one
    # out — see split_validation_test's own MIN_TEST_GAMES threshold.
    test_metrics = None
    if test_rows:
        y_test = [bool(r["label_home_win"]) for r in test_rows]
        challenger_test_probs = [predict_home_win_prob_from_vector(challenger_entry, r["features"]) for r in test_rows]
        champion_test_probs = [predict_home_win_prob_from_vector(champion_entry, r["features"]) for r in test_rows]
        test_odds = [r.get("market_odds") for r in test_rows]
        challenger_test_metrics = compute_metrics(y_test, challenger_test_probs)
        champion_test_metrics = compute_metrics(y_test, champion_test_probs)
        challenger_test_metrics["roi"] = compute_roi(y_test, challenger_test_probs, test_odds)
        champion_test_metrics["roi"] = compute_roi(y_test, champion_test_probs, test_odds)
        test_metrics = {"champion": champion_test_metrics, "challenger": challenger_test_metrics}
        print(f"test holdout (never used for any promotion decision) — champion: {champion_test_metrics['log_loss']}, challenger: {challenger_test_metrics['log_loss']}")

    version = _next_version(reg)
    version_dir = MODELS_DIR / version
    version_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(model_path, version_dir / "model.joblib")

    degenerate = is_degenerate(challenger_probs)
    beats_champion = (challenger_metrics["log_loss"] <= champion_metrics["log_loss"] - LOG_LOSS_MARGIN)

    entry = {
        "version": version,
        "type": "sklearn_logreg",
        "model_name": MODEL_TYPE_NAMES["sklearn_logreg"],
        "artifact_path": (version_dir / "model.joblib").relative_to(ROOT).as_posix(),
        "trained_at": stage_meta["trained_at"],
        "training_row_count": stage_meta["training_row_count"],
        "feature_list": stage_meta["feature_list"],
        "eval_window": eval_meta,
        "validation_test_split": split_meta,
        "metrics": challenger_metrics,
        "metrics_test_holdout": test_metrics,
        "status": None, "promoted_at": None, "rejected_reason": None,
    }

    if degenerate:
        entry["status"] = "rejected"
        entry["rejected_reason"] = "degenerate output (near-constant probability across eval set) — likely a broken training run"
        outcome, detail = "拒絕升級", entry["rejected_reason"]
    elif not beats_champion:
        entry["status"] = "rejected"
        entry["rejected_reason"] = (
            f"log_loss {challenger_metrics['log_loss']} did not beat champion "
            f"{champion_metrics['log_loss']} by required margin {LOG_LOSS_MARGIN}"
        )
        outcome, detail = "維持現有模型", entry["rejected_reason"]
    else:
        entry["status"] = "challenger"  # registry_mod.promote() flips this to "production"
        registry_mod.add_model(reg, entry)
        registry_mod.promote(reg, version)
        outcome, detail = "升級成功", f"{version} log_loss {challenger_metrics['log_loss']} < {champion_metrics['log_loss']}"
        registry_mod.save_registry(reg)
        _write_status(outcome, detail)
        print(f"PROMOTED: {version}")
        return

    registry_mod.add_model(reg, entry)
    registry_mod.save_registry(reg)
    _write_status(outcome, detail)
    print(f"NOT promoted: {detail}")


if __name__ == "__main__":
    main()
