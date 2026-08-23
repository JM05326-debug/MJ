# -*- coding: utf-8 -*-
"""Read/write helpers for models/registry.json — the single source of
truth for which model version is currently in production, and the full,
permanent history of every version ever trained (promoted or not)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = ROOT / "models" / "registry.json"

BASELINE_VERSION = "v0000_elo_poisson_baseline"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_registry() -> dict:
    if not REGISTRY_PATH.exists():
        return {"current_production": None, "models": []}
    with open(REGISTRY_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_registry(registry: dict) -> None:
    REGISTRY_PATH.parent.mkdir(exist_ok=True)
    with open(REGISTRY_PATH, "w", encoding="utf-8") as f:
        json.dump(registry, f, ensure_ascii=False, indent=2)


def get_model(registry: dict, version: str) -> dict | None:
    for m in registry["models"]:
        if m["version"] == version:
            return m
    return None


def get_production(registry: dict) -> dict | None:
    v = registry.get("current_production")
    return get_model(registry, v) if v else None


def ensure_baseline_registered(registry: dict) -> dict:
    """The existing hand-tuned Elo+Poisson blend is model version zero —
    registering it as a first-class, metric-tracked production model (even
    with metrics filled in later, once enough games have been logged) means
    every future challenger is compared against a real, already-running
    system rather than a strawman, from day one."""
    if get_model(registry, BASELINE_VERSION) is None:
        registry["models"].append({
            "version": BASELINE_VERSION,
            "type": "deterministic",
            "model_name": "Elo + Poisson Baseline",
            "artifact_path": None,
            "trained_at": None,
            "training_row_count": None,
            "feature_list": None,
            "eval_window": None,
            "metrics": {"log_loss": None, "brier": None, "accuracy": None, "auc": None,
                        "calibration_bins": None, "roi": None},
            "status": "production",
            "promoted_at": _now_iso(),
            "notes": "Existing hand-tuned Elo(K=20,+50 home)+Poisson(180d half-life) blend "
                     "with starter/bullpen/handedness context adjustments (scripts/model.py + context.py). "
                     "Registered as the permanent baseline every challenger must beat.",
            "rejected_reason": None,
        })
    if registry.get("current_production") is None:
        registry["current_production"] = BASELINE_VERSION
    return registry


def add_model(registry: dict, entry: dict) -> None:
    registry["models"].append(entry)


def promote(registry: dict, version: str) -> None:
    registry["current_production"] = version
    m = get_model(registry, version)
    if m:
        m["status"] = "production"
        m["promoted_at"] = _now_iso()
    for other in registry["models"]:
        if other["version"] != version and other["status"] == "production":
            other["status"] = "archived"
