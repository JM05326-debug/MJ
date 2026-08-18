# -*- coding: utf-8 -*-
"""Renders docs/index.html (served by GitHub Pages) from whatever
predictions/results/registry files currently exist on disk. Always runs,
regardless of whether today's fetch/lock/collect steps succeeded — a
partial or stale-but-consistent dashboard is far more useful than one that
silently stops updating, which would be indistinguishable from "the whole
pipeline is broken." Data-source freshness is surfaced explicitly instead.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
PIPELINE_DIR = ROOT / "pipeline"
for p in (SCRIPTS_DIR, PIPELINE_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import _console  # noqa: F401,E402
import registry as registry_mod  # noqa: E402

DATA_DIR = ROOT / "data"
PREDICTIONS_DIR = ROOT / "predictions"
RESULTS_DIR = ROOT / "results"
DOCS_DIR = ROOT / "docs"
TRAINING_STATUS_PATH = ROOT / "models" / "training_status.json"

RECENT_WINDOW = 30
ROI_MIN_SAMPLE = 20


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


def _freshness(filename: str) -> str | None:
    path = DATA_DIR / filename
    if not path.exists():
        return None
    mtime = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)
    return mtime.isoformat().replace("+00:00", "Z")


def _load_league(league: str):
    predictions = _read_jsonl(PREDICTIONS_DIR / f"{league}_predictions_log.jsonl")
    results = _read_jsonl(RESULTS_DIR / f"{league}_results_log.jsonl")
    result_by_id = {r["game_id"]: r for r in results}

    joined = []
    for p in predictions:
        r = result_by_id.get(p["game_id"])
        joined.append({**p, "result": r})
    return joined


def _today_games(joined: list[dict], today_str: str) -> list[dict]:
    return [g for g in joined if g["date"] == today_str]


def _recent_resolved(joined: list[dict]) -> list[dict]:
    resolved = [g for g in joined if g.get("result") and g["result"]["status"] == "final"]
    resolved.sort(key=lambda g: g["date"], reverse=True)
    return resolved[:RECENT_WINDOW]


def _accuracy(resolved: list[dict]) -> dict | None:
    if not resolved:
        return None
    correct = 0
    for g in resolved:
        favored_home = g["home_win_pct"] >= 50
        actual_home_win = g["result"]["actual_home_win"]
        if favored_home == actual_home_win:
            correct += 1
    return {"correct": correct, "n": len(resolved), "pct": round(correct / len(resolved) * 100, 1)}


def _roi(resolved: list[dict]) -> dict | None:
    """EV-gated flat-stake ROI, only over games where market odds were
    captured at lock time. Never used to gate model promotion — reporting
    only (see validate_promote.py for the real promotion metric)."""
    staked = 0.0
    profit = 0.0
    n = 0
    for g in resolved:
        odds = g.get("market_odds")
        ev = g.get("ev")
        if not odds or not ev:
            continue
        result = g["result"]
        if result["actual_home_win"] is None:
            continue
        n += 1
        home_ev = ev.get("home")
        away_ev = ev.get("away")
        if home_ev and home_ev > 0 and odds.get("home_moneyline"):
            staked += 1
            profit += (odds["home_moneyline"] - 1) if result["actual_home_win"] else -1
        if away_ev and away_ev > 0 and odds.get("away_moneyline"):
            staked += 1
            profit += (odds["away_moneyline"] - 1) if not result["actual_home_win"] else -1
    if staked == 0:
        return {"n_games": n, "n_bets": 0, "roi_pct": None}
    return {"n_games": n, "n_bets": int(staked), "roi_pct": round(profit / staked * 100, 1)}


def _load_training_status() -> dict:
    if TRAINING_STATUS_PATH.exists():
        with open(TRAINING_STATUS_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"last_run_utc": None, "outcome": "尚未執行過訓練", "detail": None}


def build():
    today_str = date.today().isoformat()
    reg = registry_mod.load_registry()
    reg = registry_mod.ensure_baseline_registered(reg)

    leagues = {}
    for league, label in (("cpbl", "CPBL 中華職棒"), ("npb", "NPB 日本職棒")):
        joined = _load_league(league)
        resolved = _recent_resolved(joined)
        leagues[league] = {
            "name": label,
            "today_games": _today_games(joined, today_str),
            "recent_resolved": resolved,
            "accuracy": _accuracy(resolved),
            "roi": _roi(resolved),
            "pending_count": len([g for g in joined if not g.get("result")]),
        }

    data = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "leagues": leagues,
        "registry": reg,
        "training_status": _load_training_status(),
        "freshness": {
            "cpbl_data": _freshness("cpbl_data.json"),
            "npb_data": _freshness("npb_data.json"),
            "cpbl_pitchers": _freshness("cpbl_pitchers.json"),
            "npb_pitchers": _freshness("npb_pitchers.json"),
        },
    }

    template = (PIPELINE_DIR / "dashboard_template.html").read_text(encoding="utf-8")
    html = template.replace("__DASHBOARD_JSON__", json.dumps(data, ensure_ascii=False))

    DOCS_DIR.mkdir(exist_ok=True)
    (DOCS_DIR / "index.html").write_text(html, encoding="utf-8")
    print("wrote", DOCS_DIR / "index.html")


if __name__ == "__main__":
    build()
