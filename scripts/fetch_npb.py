# -*- coding: utf-8 -*-
"""Fetch NPB (Nippon Professional Baseball) game results from npb.jp
and save history + upcoming games to data/npb_data.json.
"""
import _console  # noqa: F401  (must import first to fix console encoding)
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0 Safari/537.36"}
JST_OFFSET = timedelta(hours=9)  # Asia/Tokyo has no DST


def _to_utc_iso(date_str: str, time_str: str) -> str | None:
    """date_str='2026-08-06', time_str='18:00' (both Asia/Tokyo local)."""
    if not time_str or ":" not in time_str:
        return None
    try:
        naive = datetime.fromisoformat(f"{date_str}T{time_str}:00")
    except ValueError:
        return None
    aware_utc = naive.replace(tzinfo=timezone(JST_OFFSET)).astimezone(timezone.utc)
    return aware_utc.isoformat().replace("+00:00", "Z")

# npb.jp monthly schedule/result pages: /games/{year}/schedule_{MM}_detail.html
MONTH_CODES = ["02", "03", "04", "05", "06", "07", "08", "09", "10", "11"]


def fetch_month(year: int, month_code: str):
    url = f"https://npb.jp/games/{year}/schedule_{month_code}_detail.html"
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            break
        except requests.RequestException:
            if attempt == 2:
                return []
            time.sleep(1.0)
    if r.status_code != 200:
        return []
    soup = BeautifulSoup(r.content, "html.parser")
    rows = soup.select("table tbody tr")
    games = []
    current_month_day = None
    for tr in rows:
        row_id = tr.get("id", "")  # e.g. date0801
        m = re.match(r"date(\d{2})(\d{2})", row_id)
        if m:
            current_month_day = (int(m.group(1)), int(m.group(2)))
        if current_month_day is None:
            continue
        tds = tr.find_all("td")
        td = tds[0] if tds else None
        if td is None:
            continue
        time_td = tds[1] if len(tds) > 1 else None
        time_div = time_td.select_one(".time") if time_td else None
        time_text = time_div.get_text(strip=True) if time_div else ""
        team1 = td.select_one(".team1")
        team2 = td.select_one(".team2")
        score1 = td.select_one(".score1")
        score2 = td.select_one(".score2")
        if not team1 or not team2:
            continue
        t1 = team1.get_text(strip=True)
        t2 = team2.get_text(strip=True)
        s1 = score1.get_text(strip=True) if score1 else ""
        s2 = score2.get_text(strip=True) if score2 else ""
        link = td.find("a")
        slug = link["href"].strip("/") if link and link.get("href") else None
        mm, dd = current_month_day
        date_str = f"{year}-{mm:02d}-{dd:02d}"
        if s1.isdigit() and s2.isdigit():
            games.append({
                "date": date_str,
                "v": t1,
                "h": t2,
                "vs": int(s1),
                "hs": int(s2),
                "slug": slug,
            })
        else:
            # unplayed / postponed / suspended-no-score game
            pit_divs = tr.select(".pit")
            v_pitcher = h_pitcher = ""
            if len(pit_divs) >= 2:
                m1 = re.search(r"先発：(\S+)", pit_divs[0].get_text(strip=True))
                m2 = re.search(r"先発：(\S+)", pit_divs[1].get_text(strip=True))
                v_pitcher = m1.group(1) if m1 else ""
                h_pitcher = m2.group(1) if m2 else ""
            games.append({
                "date": date_str,
                "v": t1,
                "h": t2,
                "vs": None,
                "hs": None,
                "slug": slug,
                "v_pitcher": v_pitcher,
                "h_pitcher": h_pitcher,
                "scheduled_first_pitch_utc": _to_utc_iso(date_str, time_text),
            })
    return games


EXCLUDE_TEAMS = {"セ・リーグ", "パ・リーグ"}  # All-Star Game squads, not real teams


def main():
    years = [2019, 2020, 2021, 2022, 2023, 2024, 2025, 2026]
    history = []
    upcoming = []
    for year in years:
        for mc in MONTH_CODES:
            games = fetch_month(year, mc)
            for g in games:
                if g["v"] in EXCLUDE_TEAMS or g["h"] in EXCLUDE_TEAMS:
                    continue
                if g["vs"] is None:
                    if year == years[-1]:
                        upcoming.append(g)
                else:
                    history.append(g)
            time.sleep(0.3)
        print(f"year {year}: total so far history={len(history)}")

    DATA_DIR.mkdir(exist_ok=True)
    out = {"history": history, "upcoming": upcoming}
    with open(DATA_DIR / "npb_data.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print("history:", len(history), "upcoming:", len(upcoming))


if __name__ == "__main__":
    main()
