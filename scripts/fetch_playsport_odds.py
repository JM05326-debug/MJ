# -*- coding: utf-8 -*-
"""Fetch probable starters + real betting odds (moneyline, total runs) from
playsport.cc (玩運彩), a Taiwan sports-betting community site, for BOTH
CPBL (allianceid=6) and NPB (allianceid=2).

Writes data/cpbl_odds.json and data/npb_odds.json, one list of games each,
schema: {date, home, away, home_pitcher, away_pitcher, home_moneyline,
away_moneyline, ou_line, over_odds, under_odds}.

This is a best-effort supplement, not a required data source: odds are only
posted close to game time, so games further out return no odds yet (but
often already carry the probable starters). When available it fills in
starters the league's own feed hasn't announced, and lets the site/EV
calculator use real market odds. Any failure here (site down, nothing
posted, markup changed) degrades gracefully — nothing else in the pipeline
depends on these files existing, and each league is written independently
so one failing never blocks the other.

(Was scripts/fetch_cpbl_odds.py — renamed when NPB was added.)
"""
import _console  # noqa: F401  (must import first to fix console encoding)
import json
from datetime import date, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0 Safari/537.36",
           "Accept-Language": "zh-TW,zh;q=0.9"}
BASE = "https://www.playsport.cc"

# playsport.cc uses short team names; our data uses each league's own
# canonical names (CPBL: official registered name; NPB: npb.jp's Japanese
# short name, see scripts/fetch_npb.py).
CPBL_TEAM_ALIAS = {
    "統一": "統一7-ELEVEn獅",
    "味全": "味全龍",
    "兄弟": "中信兄弟",
    "樂天": "樂天桃猿",
    "富邦": "富邦悍將",
    "台鋼": "台鋼雄鷹",
}
NPB_TEAM_ALIAS = {
    "中日": "中日",
    "橫濱": "DeNA",
    "巨人": "巨人",
    "阪神": "阪神",
    "養樂多": "ヤクルト",
    "廣島": "広島",
    "羅德": "ロッテ",
    "火腿": "日本ハム",
    "樂天": "楽天",
    "西武": "西武",
    "軟銀": "ソフトバンク",
    "歐力士": "オリックス",
}

LEAGUES = {
    "cpbl": {"allianceid": 6, "out": "cpbl_odds.json", "alias": CPBL_TEAM_ALIAS},
    "npb": {"allianceid": 2, "out": "npb_odds.json", "alias": NPB_TEAM_ALIAS},
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


def fetch_gameday(session: requests.Session, allianceid: int, alias: dict,
                  gameday: str, game_date: str):
    url = f"{BASE}/predict/games?allianceid={allianceid}&gameday={gameday}"
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
            slot["away"] = alias.get(team, team)
            slot["away_pitcher"] = pitcher
            slot["away_moneyline"] = moneyline
            if side_label == "大":
                slot["ou_line"], slot["over_odds"] = ou_line, ou_odds
            elif side_label == "小":
                slot["ou_line"], slot["under_odds"] = ou_line, ou_odds
        else:
            slot["home"] = alias.get(team, team)
            slot["home_pitcher"] = pitcher
            slot["home_moneyline"] = moneyline
            if side_label == "大":
                slot["ou_line"], slot["over_odds"] = ou_line, ou_odds
            elif side_label == "小":
                slot["ou_line"], slot["under_odds"] = ou_line, ou_odds

    return [g for g in games.values() if "home" in g and "away" in g]


def fetch_league(session: requests.Session, cfg: dict):
    today = date.today()
    games = []
    games += fetch_gameday(session, cfg["allianceid"], cfg["alias"], "today", today.isoformat())
    games += fetch_gameday(session, cfg["allianceid"], cfg["alias"], "tomorrow",
                           (today + timedelta(days=1)).isoformat())
    return games


def main():
    session = requests.Session()
    session.headers.update(HEADERS)
    # Prime the session against the homepage first — playsport.cc returns 403
    # on a cold hit straight to /predict/games from some IPs.
    try:
        session.get(BASE, timeout=20)
    except requests.RequestException:
        pass

    DATA_DIR.mkdir(exist_ok=True)
    for league, cfg in LEAGUES.items():
        try:
            games = fetch_league(session, cfg)
        except Exception as e:  # noqa: BLE001 — one league failing must not block the other
            print(f"[{league}] odds fetch failed: {e}")
            continue
        with open(DATA_DIR / cfg["out"], "w", encoding="utf-8") as f:
            json.dump(games, f, ensure_ascii=False)
        print(f"[{league}] odds games found: {len(games)}")


if __name__ == "__main__":
    main()
