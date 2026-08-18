# -*- coding: utf-8 -*-
"""Fetch CPBL probable starters + real betting odds (moneyline, total
runs) from playsport.cc (玩運彩), a Taiwan sports-betting community site.

This is a best-effort supplement, not a required data source: odds are
only posted close to game time, so most games return nothing yet. When
available, it fills in starters CPBL's own site hasn't announced yet and
lets the site pre-fill real market odds into the EV calculator instead of
requiring manual entry. Any failure here (site down, no odds posted,
markup changed) degrades gracefully — the rest of the pipeline doesn't
depend on this file existing.
"""
import _console  # noqa: F401  (must import first to fix console encoding)
import json
from datetime import date, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0 Safari/537.36"}
BASE = "https://www.playsport.cc"

# playsport.cc uses short team names; our data uses full official names.
TEAM_ALIAS = {
    "統一": "統一7-ELEVEn獅",
    "味全": "味全龍",
    "兄弟": "中信兄弟",
    "樂天": "樂天桃猿",
    "富邦": "富邦悍將",
    "台鋼": "台鋼雄鷹",
}


def _parse_odds(td):
    """<strong>line</strong><span>, odds</span> or <strong></strong><span>odds</span>."""
    label = td.find("strong", class_="team-side")
    if not label:
        return None, None
    wrap = td.find("span", class_="data-wrap")
    if not wrap:
        return None, None
    parts = wrap.find_all("strong")
    line_text = parts[0].get_text(strip=True) if parts else ""
    span = wrap.find("span")
    rest = span.get_text(strip=True) if span else ""
    odds_text = rest.lstrip(", ").strip()
    try:
        odds = float(odds_text) if odds_text else None
    except ValueError:
        odds = None
    line = None
    if line_text:
        try:
            line = float(line_text)
        except ValueError:
            line = None
    return line, odds


def fetch_gameday(session: requests.Session, gameday: str, game_date: str):
    url = f"{BASE}/predict/games?allianceid=6&gameday={gameday}"
    try:
        r = session.get(url, headers=HEADERS, timeout=20)
    except requests.RequestException:
        return []
    if r.status_code != 200:
        return []
    soup = BeautifulSoup(r.content, "html.parser")
    rows = soup.select("tr[gameid]")

    games = {}
    for tr in rows:
        gid = tr.get("gameid")
        team_cell = tr.select_one(".td-teaminfo")
        if not team_cell:
            continue
        name_tag = team_cell.select_one("h3 a")
        pitcher_tag = team_cell.select_one("p")
        team = name_tag.get_text(strip=True) if name_tag else None
        pitcher = pitcher_tag.get_text(strip=True) if pitcher_tag else ""
        if not team:
            continue

        moneyline_td = tr.select_one(".td-bank-bet03")
        ou_td = tr.select_one(".td-bank-bet02")
        _, moneyline = _parse_odds(moneyline_td) if moneyline_td else (None, None)
        ou_line, ou_odds = _parse_odds(ou_td) if ou_td else (None, None)
        side_label = None
        if ou_td:
            side_tag = ou_td.find("strong", class_="team-side")
            side_label = side_tag.get_text(strip=True) if side_tag else None

        slot = games.setdefault(gid, {"date": game_date})
        # first row per game is 客 (away), second is 主 (home)
        if "away" not in slot:
            slot["away"] = TEAM_ALIAS.get(team, team)
            slot["away_pitcher"] = pitcher
            slot["away_moneyline"] = moneyline
            if side_label == "大":
                slot["ou_line"], slot["over_odds"] = ou_line, ou_odds
            elif side_label == "小":
                slot["ou_line"], slot["under_odds"] = ou_line, ou_odds
        else:
            slot["home"] = TEAM_ALIAS.get(team, team)
            slot["home_pitcher"] = pitcher
            slot["home_moneyline"] = moneyline
            if side_label == "大":
                slot["ou_line"], slot["over_odds"] = ou_line, ou_odds
            elif side_label == "小":
                slot["ou_line"], slot["under_odds"] = ou_line, ou_odds

    return [g for g in games.values() if "home" in g and "away" in g]


def main():
    today = date.today()
    session = requests.Session()
    session.headers.update(HEADERS)

    all_games = []
    all_games += fetch_gameday(session, "today", today.isoformat())
    all_games += fetch_gameday(session, "tomorrow", (today + timedelta(days=1)).isoformat())

    DATA_DIR.mkdir(exist_ok=True)
    with open(DATA_DIR / "cpbl_odds.json", "w", encoding="utf-8") as f:
        json.dump(all_games, f, ensure_ascii=False)
    print("odds games found:", len(all_games))


if __name__ == "__main__":
    main()
