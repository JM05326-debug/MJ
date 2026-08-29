# -*- coding: utf-8 -*-
"""Daily job: lock in a prediction for every upcoming game that (a) hasn't
been predicted yet and (b) hasn't started (with a safety buffer). Once a
game_id has an entry in predictions/<league>_predictions_log.jsonl, this
script will NEVER touch that line again — the only write path is append.
This is what makes requirement #3 (lock before first pitch, no post-start
data can revise it) and requirement #8 (no leakage) structural rather than
just a convention.

Run this multiple times per day (see .github/workflows/daily_predict.yml):
a game isn't locked until both probable starters are known (or until
FINAL_LOCK_HOURS before first pitch, whichever comes first), so a run that
sees a game before its starters are announced deliberately leaves it for a
later run.
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
from context import pick_starter, resolve_pitcher_name  # noqa: E402
from feature_spec import LEAGUES, load_live_context  # noqa: E402
from game_id import game_id as make_game_id  # noqa: E402
from predictor import predict_with_model  # noqa: E402
import registry as registry_mod  # noqa: E402

PREDICTIONS_DIR = ROOT / "predictions"
SAFETY_BUFFER_MINUTES = 45
# Outer bound: never even consider a game further out than this (a locked
# game_id is never re-predicted, so freezing one days early just guarantees
# a low-information row).
LOCK_WINDOW_HOURS = 36
# Inner gate: a game inside the window is still HELD BACK until both probable
# starters are known. Locking the moment a game entered a 36h window — hours
# before either starter is announced — is why NPB/CPBL predictions used to
# almost always run starter-less. The hold is released this many hours before
# first pitch regardless, so a genuinely late or never-announced starter
# can't block a game from being locked at all (it just locks on Elo+Poisson,
# same as before). Given daily_predict.yml's ~6 runs/day, the release window
# still contains 2-3 scheduled runs for even the earliest games.
FINAL_LOCK_HOURS = 10

# Conservative fallback first-pitch times (local league timezone) used only
# when a game's own scheduled_first_pitch_utc couldn't be scraped — always
# an UNDER-estimate of the real time, so the safety buffer errs toward
# skipping a game rather than ever locking one that may have started.
DEFAULT_FIRST_PITCH_LOCAL = {
    "cpbl": {"weekday": "17:00", "weekend": "13:00"},  # Asia/Taipei UTC+8
    "npb": {"weekday": "17:00", "weekend": "12:00"},   # Asia/Tokyo UTC+9
}
LEAGUE_UTC_OFFSET_HOURS = {"cpbl": 8, "npb": 9}


def _assumed_first_pitch_utc(league: str, date_str: str) -> datetime:
    d = date.fromisoformat(date_str)
    key = "weekend" if d.weekday() >= 5 else "weekday"
    hh, mm = DEFAULT_FIRST_PITCH_LOCAL[league][key].split(":")
    local_naive = datetime(d.year, d.month, d.day, int(hh), int(mm))
    offset = timedelta(hours=LEAGUE_UTC_OFFSET_HOURS[league])
    return local_naive.replace(tzinfo=timezone(offset)).astimezone(timezone.utc)


def _scheduled_first_pitch(league: str, game: dict) -> tuple[datetime, str]:
    raw = game.get("scheduled_first_pitch_utc")
    if raw:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")), "scraped"
    return _assumed_first_pitch_utc(league, game["date"]), "assumed_default"


def _load_locked_ids(path: Path) -> set:
    if not path.exists():
        return set()
    ids = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            ids.add(json.loads(line)["game_id"])
    return ids


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def lock_league(league: str, now_utc: datetime) -> int:
    log_path = PREDICTIONS_DIR / f"{league}_predictions_log.jsonl"
    locked_ids = _load_locked_ids(log_path)

    try:
        ctx, upcoming = load_live_context(league)
    except FileNotFoundError as e:
        print(f"[{league}] SKIP: required data file missing ({e}); "
              f"leaving predictions log untouched, will retry next scheduled run")
        return 0

    reg = registry_mod.load_registry()
    reg = registry_mod.ensure_baseline_registered(reg)
    registry_mod.save_registry(reg)
    production = registry_mod.get_production(reg)

    locked_count = 0
    for game in upcoming:
        gid = make_game_id(league, game)
        if gid in locked_ids:
            continue

        first_pitch_utc, source = _scheduled_first_pitch(league, game)
        cutoff = first_pitch_utc - timedelta(minutes=SAFETY_BUFFER_MINUTES)
        if now_utc >= cutoff:
            continue  # too close to / past first pitch this run — never lock retroactively
        if first_pitch_utc - now_utc > timedelta(hours=LOCK_WINDOW_HOURS):
            continue  # too far out — wait for a closer run so late-announced starters can still be caught

        home, away = game["h"], game["v"]
        odds_entry = ctx.odds_map.get((game["date"], home, away))

        # Prefer playsport (玩運彩) for the probable starter, falling back to
        # the league's own feed — see context.pick_starter for why it isn't
        # an unconditional override.
        odds_home_p = odds_entry.get("home_pitcher") if odds_entry else None
        odds_away_p = odds_entry.get("away_pitcher") if odds_entry else None
        home_starter = pick_starter(odds_home_p, game.get("h_pitcher"), ctx.pitchers)
        away_starter = pick_starter(odds_away_p, game.get("v_pitcher"), ctx.pitchers)
        # store the canonical roster name (what the pitcher factor actually
        # keyed off), not whichever ragged feed form we happened to read
        home_starter = resolve_pitcher_name(home_starter, ctx.pitchers) or home_starter
        away_starter = resolve_pitcher_name(away_starter, ctx.pitchers) or away_starter

        starters_known = bool(home_starter) and bool(away_starter)
        if not starters_known and first_pitch_utc - now_utc > timedelta(hours=FINAL_LOCK_HOURS):
            # hold this game back for a later run — a starter may still be
            # announced before we hit the FINAL_LOCK_HOURS deadline
            print(f"[{league}] hold {gid}: {away} @ {home} — starters not both known, "
                  f"{(first_pitch_utc - now_utc).total_seconds() / 3600:.1f}h to first pitch")
            continue

        result = predict_with_model(production, home, away, home_starter, away_starter, ctx)

        market_odds = None
        ev = None
        if odds_entry:
            market_odds = {
                "home_moneyline": odds_entry.get("home_moneyline"),
                "away_moneyline": odds_entry.get("away_moneyline"),
                "ou_line": odds_entry.get("ou_line"),
                "over_odds": odds_entry.get("over_odds"),
                "under_odds": odds_entry.get("under_odds"),
            }
            ev = {}
            if market_odds["home_moneyline"]:
                ev["home"] = round(result["home_win_pct"] / 100 * market_odds["home_moneyline"] - 1, 4)
            if market_odds["away_moneyline"]:
                ev["away"] = round(result["away_win_pct"] / 100 * market_odds["away_moneyline"] - 1, 4)

        record = {
            "game_id": gid,
            "league": league,
            "date": game["date"],
            "home": home, "away": away,
            "home_starter": home_starter or "", "away_starter": away_starter or "",
            "scheduled_first_pitch_utc": first_pitch_utc.isoformat().replace("+00:00", "Z"),
            "first_pitch_source": source,
            "locked_at_utc": now_utc.isoformat().replace("+00:00", "Z"),
            "model_version": production["version"],
            "model_name": result["model_name"],
            "home_win_pct": result["home_win_pct"],
            "away_win_pct": result["away_win_pct"],
            "predicted_home_runs": result["predicted_home_runs"],
            "predicted_away_runs": result["predicted_away_runs"],
            "predicted_total_runs": result["predicted_total_runs"],
            "confidence_score": result["confidence_score"],
            "confidence_label": result["confidence_label"],
            "market_odds": market_odds,
            "ev": ev,
            "context_notes": result["context_notes"],
            "feature_vector": result["feature_vector"],  # frozen verbatim for training later
            "feature_spec_version": result["feature_spec_version"],
        }
        _append_jsonl(log_path, record)
        locked_ids.add(gid)
        locked_count += 1
        print(f"[{league}] locked {gid}: {away} @ {home} — home {result['home_win_pct']}% "
              f"({result['confidence_label']}, {production['version']})")

    return locked_count


def main():
    now_utc = datetime.now(timezone.utc)
    total = 0
    for league in LEAGUES:
        total += lock_league(league, now_utc)
    print(f"locked {total} new prediction(s) this run")


if __name__ == "__main__":
    main()
