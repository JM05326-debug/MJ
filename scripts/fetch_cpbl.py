# -*- coding: utf-8 -*-
"""Fetch CPBL (Chinese Professional Baseball League) schedule + results from
Rebas 野球革命 (rebas.tw)'s public JSON API and save history + upcoming games
to data/cpbl_data.json.

Runs in the cloud (GitHub Actions), unlike the old www.cpbl.com.tw scraper
this replaces: the official site blocks GitHub Actions' published IP ranges,
but rebas.tw is a different domain and isn't on that blocklist.

Historical games already collected via the old scraper stay untouched in
`history` (that's still correct data, no need to re-derive it) — this only
merges in newly-finished games going forward. `native_id` is reconstructed
as f"{year}-A-{seq}", verified against the old cpbl.com.tw-derived
`{Year}-{KindCode}-{GameSno}` scheme for every game currently locked in
predictions/cpbl_predictions_log.jsonl (all matched exactly) so already-
locked predictions keep resolving to the same game_id
(see pipeline/game_id.py).
"""
import _console  # noqa: F401  (must import first to fix console encoding)
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

TAIPEI_OFFSET = timedelta(hours=8)  # Asia/Taipei has no DST
LOOKAHEAD_DAYS = 45  # how far into the future to pull upcoming schedule
LOOKBACK_BUFFER_DAYS = 3  # re-check a few days before our latest known date, in case a game was postponed/rescored
CHUNK_DAYS = 1  # rebas's /calendar response sometimes omits scheduled_SP for a game when queried as
                # part of a wide date range, even though the same game returns it fine alone — the
                # exact trigger isn't documented and doesn't look like a clean days-count cutoff
                # (empirically non-monotonic), just more likely to happen the wider the request. Querying
                # one day at a time is the most reliable way to get it; the odd still-missing case is
                # handled by main()'s "backfill a blank v_pitcher/h_pitcher on a later run" logic below.


def _to_utc_iso(local_dt_str: str | None) -> str | None:
    """rebas's scheduled_start_at is a naive 'YYYY-MM-DD HH:MM' in Asia/Taipei time."""
    if not local_dt_str:
        return None
    try:
        naive = datetime.strptime(local_dt_str, "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    aware_utc = naive.replace(tzinfo=timezone(TAIPEI_OFFSET)).astimezone(timezone.utc)
    return aware_utc.isoformat().replace("+00:00", "Z")


ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0 Safari/537.36"}
BASE = "https://www.rebas.tw"


def fetch_calendar(session: requests.Session, start: str, days: int) -> list:
    r = session.get(
        f"{BASE}/api/formal/calendar",
        params={"start": start, "days": days, "league_uniqid": "CPBL"},
        headers=HEADERS, timeout=30,
    )
    r.raise_for_status()
    payload = r.json()
    if payload.get("error"):
        return []
    return payload.get("data") or []


def fetch_calendar_range(session: requests.Session, start_date, total_days: int) -> list:
    """fetch_calendar(), but split into CHUNK_DAYS-sized requests (see
    CHUNK_DAYS) so scheduled_SP is never silently dropped."""
    games = []
    offset = 0
    while offset < total_days:
        chunk = min(CHUNK_DAYS, total_days - offset)
        chunk_start = (start_date + timedelta(days=offset)).isoformat()
        games.extend(fetch_calendar(session, chunk_start, chunk))
        offset += chunk
    return games


def _is_regular_season(g: dict) -> bool:
    """rebas's calendar mixes regular season, exhibition ("官辦熱身賽"), and
    postseason games under the same league_uniqid — each is its own `season`
    record. Regular season's title is exactly '中職{year}年'; anything else
    (exhibition/postseason) has a suffixed title and is excluded, matching
    the old scraper's kind_code='A'-only behavior."""
    season = g.get("season") or {}
    year = season.get("uniqid", "").split("-")[1] if season.get("uniqid") else ""
    return bool(year) and season.get("title") == f"中職{year}年"


def to_row(g: dict) -> tuple[dict, str]:
    season_uniqid = g["season"]["uniqid"]
    year = season_uniqid.split("-")[1]
    native_id = f"{year}-A-{g['seq']}"
    info = g["info"]
    status = info.get("status", "")
    away, home = g["away"], g["home"]
    finished = status == "FINISHED" and away.get("runs") is not None and home.get("runs") is not None
    row = {
        "date": info.get("scheduled_start_at", "")[:10],
        "v": away["team"],
        "h": home["team"],
        "vs": away.get("runs") if finished else None,
        "hs": home.get("runs") if finished else None,
        "v_pitcher": ((away.get("scheduled_SP") or {}).get("name")) or "",
        "h_pitcher": ((home.get("scheduled_SP") or {}).get("name")) or "",
        "native_id": native_id,
    }
    return row, ("FINISHED" if finished else status)


def main():
    session = requests.Session()
    session.headers.update(HEADERS)

    existing_path = DATA_DIR / "cpbl_data.json"
    if existing_path.exists():
        with open(existing_path, encoding="utf-8") as f:
            existing = json.load(f)
    else:
        existing = {"history": [], "upcoming": []}

    history = existing.get("history", [])
    history_by_id = {r["native_id"]: r for r in history if r.get("native_id")}
    known_ids = set(history_by_id)
    latest_known_date = max((r["date"] for r in history if r.get("date")), default="2019-01-01")

    today = date.today()
    start = min(
        datetime.strptime(latest_known_date, "%Y-%m-%d").date() - timedelta(days=LOOKBACK_BUFFER_DAYS),
        today,
    )
    days_span = (today - start).days + 1 + LOOKAHEAD_DAYS
    games = fetch_calendar_range(session, start, days_span)

    # native_id -> row. A postponed (rained out) game keeps showing up under
    # its *original* slot/date with status POSTPONED even after the league
    # assigns a makeup date elsewhere in the calendar — both share the same
    # seq/native_id, so without deduping here a rainout produces two
    # upcoming rows for one game_id. SCHEDULED (a confirmed makeup date) beats
    # anything else; among equal status, the later date wins (makeup dates
    # move forward, never back).
    upcoming_by_id: dict[str, dict] = {}
    upcoming_status: dict[str, str] = {}
    added = 0
    for g in games:
        if not _is_regular_season(g):
            continue
        row, status = to_row(g)
        if status == "FINISHED":
            existing_row = history_by_id.get(row["native_id"])
            if existing_row is None:
                history.append(row)
                history_by_id[row["native_id"]] = row
                known_ids.add(row["native_id"])
                added += 1
            elif not existing_row["v_pitcher"] and not existing_row["h_pitcher"]:
                # rebas can momentarily lack scheduled_SP right as a game
                # finishes; backfill names once available without touching
                # date/score/native_id (the frozen part of the row).
                existing_row["v_pitcher"] = row["v_pitcher"]
                existing_row["h_pitcher"] = row["h_pitcher"]
            continue
        if row["native_id"] in known_ids:
            continue  # already resolved as finished in history; ignore a stray non-finished duplicate

        row["field"] = g["info"].get("location_abbr", "")
        row["scheduled_first_pitch_utc"] = _to_utc_iso(g["info"].get("scheduled_start_at"))

        nid = row["native_id"]
        prev_status = upcoming_status.get(nid)
        if prev_status is None:
            keep = True
        elif status == "SCHEDULED" and prev_status != "SCHEDULED":
            keep = True
        elif status != "SCHEDULED" and prev_status == "SCHEDULED":
            keep = False
        else:
            keep = row["date"] >= upcoming_by_id[nid]["date"]
        if keep:
            upcoming_by_id[nid] = row
            upcoming_status[nid] = status

    upcoming = list(upcoming_by_id.values())

    DATA_DIR.mkdir(exist_ok=True)
    with open(DATA_DIR / "cpbl_data.json", "w", encoding="utf-8") as f:
        json.dump({"history": history, "upcoming": upcoming}, f, ensure_ascii=False)
    print(f"history: {len(history)} (+{added} new), upcoming: {len(upcoming)}")


if __name__ == "__main__":
    main()
