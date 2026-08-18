# -*- coding: utf-8 -*-
"""Daily job: for every locked prediction that doesn't have a result yet,
check whether the game now has a final score in the freshly-fetched
history data, and if so append it to results/<league>_results_log.jsonl.

Never fabricates a result and never touches predictions/*.jsonl (the
prediction record is immutable once locked — this only ever appends to a
SEPARATE results log, joined at training time by game_id). If nothing new
finished, it does nothing and exits 0 — that's the normal, expected state
most runs, not an error.
"""
from __future__ import annotations

import json
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
from generate_site import load_league_games  # noqa: E402
from game_id import game_id as make_game_id  # noqa: E402

PREDICTIONS_DIR = ROOT / "predictions"
RESULTS_DIR = ROOT / "results"
VOID_AFTER_DAYS = 10  # postponed/cancelled/dropped-from-source games stop being "pending" forever

LEAGUE_GAME_FILES = {"cpbl": "cpbl_data.json", "npb": "npb_data.json"}


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


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def collect_league(league: str, now_utc: datetime) -> int:
    pred_path = PREDICTIONS_DIR / f"{league}_predictions_log.jsonl"
    results_path = RESULTS_DIR / f"{league}_results_log.jsonl"

    predictions = _read_jsonl(pred_path)
    if not predictions:
        return 0
    existing_result_ids = {r["game_id"] for r in _read_jsonl(results_path)}
    pending = [p for p in predictions if p["game_id"] not in existing_result_ids]
    if not pending:
        return 0

    try:
        history, _ = load_league_games(LEAGUE_GAME_FILES[league])
    except FileNotFoundError as e:
        print(f"[{league}] SKIP: game data missing ({e}); results log untouched, will retry next run")
        return 0

    # index finished games by game_id for O(1) lookup
    finished_by_id = {}
    for g in history:
        if g.get("hs") is None or g.get("vs") is None:
            continue
        finished_by_id[make_game_id(league, g)] = g

    collected = 0
    for p in pending:
        gid = p["game_id"]
        g = finished_by_id.get(gid)
        if g is not None:
            home_runs, away_runs = g["hs"], g["vs"]
            record = {
                "game_id": gid,
                "league": league,
                "actual_home_runs": home_runs,
                "actual_away_runs": away_runs,
                "actual_total_runs": home_runs + away_runs,
                "actual_home_win": home_runs > away_runs,
                "status": "final",
                "collected_at_utc": now_utc.isoformat().replace("+00:00", "Z"),
            }
            _append_jsonl(results_path, record)
            collected += 1
            print(f"[{league}] result collected {gid}: {home_runs}-{away_runs}")
            continue

        # not finished yet — check whether it's aged out (postponed/dropped)
        locked_at = datetime.fromisoformat(p["locked_at_utc"].replace("Z", "+00:00"))
        if now_utc - locked_at > timedelta(days=VOID_AFTER_DAYS):
            record = {
                "game_id": gid, "league": league,
                "actual_home_runs": None, "actual_away_runs": None, "actual_total_runs": None,
                "actual_home_win": None, "status": "void",
                "collected_at_utc": now_utc.isoformat().replace("+00:00", "Z"),
            }
            _append_jsonl(results_path, record)
            collected += 1
            print(f"[{league}] voided (unresolved after {VOID_AFTER_DAYS}d): {gid}")

    return collected


def main():
    now_utc = datetime.now(timezone.utc)
    total = 0
    for league in LEAGUE_GAME_FILES:
        total += collect_league(league, now_utc)
    print(f"collected {total} new result(s) this run")


if __name__ == "__main__":
    main()
