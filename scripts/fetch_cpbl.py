# -*- coding: utf-8 -*-
"""Fetch CPBL (Chinese Professional Baseball League) game results from the
official site's schedule API and save history + upcoming games to
data/cpbl_data.json.
"""
import _console  # noqa: F401  (must import first to fix console encoding)
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

TAIPEI_OFFSET = timedelta(hours=8)  # Asia/Taipei has no DST


def _to_utc_iso(local_dt_str: str) -> str | None:
    """CPBL's GameDateTimeS is a naive 'YYYY-MM-DDTHH:MM:SS' in Asia/Taipei time."""
    if not local_dt_str:
        return None
    try:
        naive = datetime.fromisoformat(local_dt_str)
    except ValueError:
        return None
    aware_utc = naive.replace(tzinfo=timezone(TAIPEI_OFFSET)).astimezone(timezone.utc)
    return aware_utc.isoformat().replace("+00:00", "Z")

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0 Safari/537.36"}
BASE = "https://www.cpbl.com.tw"


def get_session():
    s = requests.Session()
    s.headers.update(HEADERS)
    r = s.get(f"{BASE}/schedule", timeout=20)
    r.raise_for_status()
    m = re.search(r'RequestVerificationToken:\s*[\'"]([^\'"]+)[\'"]', r.text)
    if not m:
        raise RuntimeError("could not find RequestVerificationToken on /schedule page")
    return s, m.group(1)


def fetch_year(session: requests.Session, token: str, year: int, kind_code: str = "A"):
    headers = {
        "RequestVerificationToken": token,
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Referer": f"{BASE}/schedule",
    }
    data = {"calendar": f"{year}/01/01", "location": "", "kindCode": kind_code}
    r = session.post(f"{BASE}/schedule/getgamedatas", data=data, headers=headers, timeout=20)
    r.raise_for_status()
    payload = r.json()
    if not payload.get("Success"):
        return []
    return json.loads(payload["GameDatas"])


def main():
    years = [2019, 2020, 2021, 2022, 2023, 2024, 2025, 2026]
    session, token = get_session()

    history = []
    upcoming = []
    today_str = date.today().isoformat()

    for year in years:
        games = fetch_year(session, token, year)
        for g in games:
            game_date = g["GameDate"][:10]
            native_id = f"{g['Year']}-{g['KindCode']}-{g['GameSno']}"
            row = {
                "date": game_date,
                "v": g["VisitingTeamName"],
                "h": g["HomeTeamName"],
                "vs": g["VisitingScore"],
                "hs": g["HomeScore"],
                "v_pitcher": g.get("VisitingPitcherName") or "",
                "h_pitcher": g.get("HomePitcherName") or "",
                "native_id": native_id,
            }
            played = g.get("GameDateTimeE") is not None and g["VisitingScore"] is not None
            if played:
                history.append(row)
            elif year == years[-1] and game_date >= today_str:
                row["field"] = g.get("FieldAbbe", "")
                row["scheduled_first_pitch_utc"] = _to_utc_iso(g.get("GameDateTimeS"))
                upcoming.append(row)
        print(f"year {year}: total so far history={len(history)}")

    DATA_DIR.mkdir(exist_ok=True)
    with open(DATA_DIR / "cpbl_data.json", "w", encoding="utf-8") as f:
        json.dump({"history": history, "upcoming": upcoming}, f, ensure_ascii=False)
    print("history:", len(history), "upcoming:", len(upcoming))


if __name__ == "__main__":
    main()
