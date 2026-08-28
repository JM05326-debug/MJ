# -*- coding: utf-8 -*-
"""Fetch NPB pitcher & batter data: team-level season stat tables (one
page per team covers the whole roster), plus a recent-window box-score
scan for last-5-starts and last-7-days bullpen workload. Saves
data/npb_pitchers.json and data/npb_batters.json.
"""
import _console  # noqa: F401  (must import first to fix console encoding)
import json
import re
import time
from datetime import date, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

import fetch_npb as npbsched

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0 Safari/537.36"}
YEAR = date.today().year

TEAM_CODES = {
    "t": "阪神", "db": "DeNA", "g": "巨人", "d": "中日", "c": "広島", "s": "ヤクルト",
    "h": "ソフトバンク", "f": "日本ハム", "b": "オリックス", "e": "楽天", "l": "西武", "m": "ロッテ",
}

RECENT_WINDOW_DAYS = 25
BULLPEN_WINDOW_DAYS = 7


def _true_innings(text: str) -> float:
    """'17.2' style spans mean 17 and 2/3 innings (thirds notation)."""
    text = text.strip()
    if "." in text:
        whole, frac = text.split(".")
        whole = int(whole) if whole else 0
        outs = int(frac)
    else:
        whole = int(text) if text else 0
        outs = 0
    return whole + outs / 3.0


def _surname_key(full_name: str) -> str:
    """Just the family name. Box scores, schedules and win/loss pitcher
    credits print only this, so it's what the recent-appearance scan is
    keyed by — but it is NOT unique (several 髙橋 / 村上 / 伊藤 pitch in a
    given season), so it's only used to *join* appearances, never as the
    pitcher's own record key (see _fullname_key)."""
    name = full_name.lstrip("*").strip()
    return name.split("　")[0] if "　" in name else name


def _fullname_key(full_name: str) -> str:
    """The pitcher's own record key: the compact full name. Season/roster
    tables print '*姓　名' (roster marker + full-width space); strip both so
    the key is '髙橋宏斗' — the form npb.jp schedules and playsport.cc both
    (mostly) use, and one that actually distinguishes same-surname pitchers.
    scripts/context.resolve_pitcher_name maps the ragged real-world name
    forms back onto this."""
    return full_name.lstrip("*").replace("　", "").strip()


def _cell_text(td) -> str:
    span = td.find("span", class_="integer")
    if span:
        integer = span.get_text(strip=True)
        dec = td.find("span", class_="decimal")
        decimal = dec.get_text(strip=True) if dec else ""
        return integer + decimal
    return td.get_text(strip=True)


def _get(session: requests.Session, url: str, retries: int = 2):
    """GET with a couple of retries — this pipeline runs unattended, so a
    single transient network hiccup shouldn't lose an otherwise-successful
    run's worth of scraped data."""
    for attempt in range(retries + 1):
        try:
            return session.get(url, headers=HEADERS, timeout=20)
        except requests.RequestException:
            if attempt == retries:
                return None
            time.sleep(1.0)
    return None


def _box_innings(td) -> float:
    """Per-game box scores encode innings pitched as a nested
    <table class="table_inning"><tr><th>whole</th><td>marker</td></tr></table>
    where the marker is empty for a clean whole-inning outing or contains a
    '+' for a partial inning — unlike the season tables, the exact out
    count (1 or 2) isn't given, so a non-empty marker is approximated as
    +1 out (1/3 inning)."""
    inner = td.find("table", class_="table_inning")
    if not inner:
        return 0.0
    th = inner.find("th")
    whole = int(th.get_text(strip=True)) if th and th.get_text(strip=True).isdigit() else 0
    marker_td = inner.find("td")
    marker = marker_td.get_text(strip=True) if marker_td else ""
    return whole + (1 / 3.0 if marker else 0.0)


def fetch_team_pitching(session: requests.Session, code: str):
    url = f"https://npb.jp/bis/{YEAR}/stats/idp1_{code}.html"
    r = _get(session, url)
    if r is None or r.status_code != 200:
        return []
    soup = BeautifulSoup(r.content, "html.parser")
    rows = soup.select("table.tablefix2 tbody tr")
    out = []
    for tr in rows:
        tds = tr.find_all("td")
        if len(tds) < 23:
            continue
        name = tds[0].get_text(strip=True)
        try:
            ip = _true_innings(_cell_text(tds[12]))
            h = int(tds[13].get_text(strip=True))
            hr = int(tds[14].get_text(strip=True))
            bb = int(tds[15].get_text(strip=True))
            hbp = int(tds[17].get_text(strip=True))
            k = int(tds[18].get_text(strip=True))
            er = int(tds[22].get_text(strip=True))
            era = float(tds[23].get_text(strip=True) or 0)
        except ValueError:
            continue
        if ip <= 0:
            continue
        whip = (h + bb) / ip
        k9 = k * 9 / ip
        bb9 = bb * 9 / ip
        fip_raw = (13 * hr + 3 * (bb + hbp) - 2 * k) / ip
        out.append({
            "name": name, "ip": round(ip, 1), "h": h, "hr": hr, "bb": bb, "hbp": hbp, "k": k, "er": er,
            "era": era, "whip": round(whip, 2), "k9": round(k9, 2), "bb9": round(bb9, 2),
            "fip_raw": round(fip_raw, 2),
        })
    return out


def fetch_team_batting(session: requests.Session, code: str):
    url = f"https://npb.jp/bis/{YEAR}/stats/idb1_{code}.html"
    r = _get(session, url)
    if r is None or r.status_code != 200:
        return []
    soup = BeautifulSoup(r.content, "html.parser")
    rows = soup.select("table.tablefix2 tbody tr")
    out = []
    for tr in rows:
        tds = tr.find_all("td")
        if len(tds) < 22:
            continue
        name = tds[0].get_text(strip=True)
        try:
            pa = int(tds[2].get_text(strip=True))
            ab = int(tds[3].get_text(strip=True))
            h = int(tds[5].get_text(strip=True))
            tb = int(tds[9].get_text(strip=True))
            sf = int(tds[14].get_text(strip=True))
            bb = int(tds[15].get_text(strip=True))
            hbp = int(tds[17].get_text(strip=True))
        except ValueError:
            continue
        if ab <= 0:
            continue
        obp_den = ab + bb + hbp + sf
        obp = (h + bb + hbp) / obp_den if obp_den else 0
        slg = tb / ab
        out.append({
            "name": name, "pa": pa, "ab": ab, "h": h, "bb": bb, "hbp": hbp, "sf": sf, "tb": tb,
            "avg": round(h / ab, 3), "obp": round(obp, 3), "slg": round(slg, 3), "ops": round(obp + slg, 3),
        })
    return out


def scan_recent_boxscores(session: requests.Session, slugs: list):
    """slugs: list of (date_str, slug) for games in the recent window.
    Returns per-pitcher appearance list keyed by (team_abbr_in_box, name)."""
    appearances = {}  # name -> list of {date, team, role, ip, pitches, er}
    name_to_id = {}

    for game_date, slug in slugs:
        slug = slug.removeprefix("scores/")
        url = f"https://npb.jp/scores/{slug}/box.html"
        r = _get(session, url)
        if r is None or r.status_code != 200:
            continue
        soup = BeautifulSoup(r.content, "html.parser")
        for table_id, team_label in [("tablefix_t_p", "top"), ("tablefix_b_p", "bottom")]:
            table = soup.find("table", id=table_id)
            if not table:
                continue
            tbody = table.find("tbody")
            rows = tbody.find_all("tr", recursive=False) if tbody else []
            for i, tr in enumerate(rows):
                tds = tr.find_all("td", recursive=False)
                if len(tds) < 14:
                    continue
                link = tds[1].find("a")
                name = link.get_text(strip=True) if link else tds[1].get_text(strip=True)
                if link and link.get("href"):
                    m = re.search(r"/players/(\d+)\.html", link["href"])
                    if m:
                        name_to_id[name] = m.group(1)
                try:
                    pitches = int(tds[2].get_text(strip=True) or 0)
                    ip = _box_innings(tds[4])
                    er = int(tds[13].get_text(strip=True) or 0)  # 自責点 (earned runs); tds[12] is 失点 (total runs)
                except ValueError:
                    continue
                role = "先發" if i == 0 else "後援"
                appearances.setdefault(name, []).append({
                    "date": game_date, "role": role, "ip": round(ip, 2), "pitches": pitches, "er": er,
                })
        time.sleep(0.15)

    return appearances, name_to_id


def fetch_hand(session: requests.Session, player_id: str) -> str:
    r = _get(session, f"https://npb.jp/bis/players/{player_id}.html")
    if r is None or r.status_code != 200:
        return ""
    m = re.search(r"<th>投打</th>\s*<td>([^<]+)</td>", r.text)
    return m.group(1).strip() if m else ""


def main():
    today = date.today()
    cutoff_recent = today - timedelta(days=RECENT_WINDOW_DAYS)
    cutoff7 = (today - timedelta(days=BULLPEN_WINDOW_DAYS)).isoformat()

    session = requests.Session()
    session.headers.update(HEADERS)

    print("collecting recent game slugs...")
    slugs = []
    months_to_check = sorted({(cutoff_recent.year, cutoff_recent.month), (today.year, today.month)})
    for yr, mo in months_to_check:
        games = npbsched.fetch_month(yr, f"{mo:02d}")
        for g in games:
            if g.get("slug") and g["vs"] is not None and g["date"] >= cutoff_recent.isoformat():
                slugs.append((g["date"], g["slug"]))
    print(f"found {len(slugs)} recent games to scan")

    appearances, name_to_id = scan_recent_boxscores(session, slugs)
    print(f"box scan complete: {len(appearances)} pitchers seen")

    all_pitchers = {}
    all_batters = {}

    for code, team_name in TEAM_CODES.items():
        pitching = fetch_team_pitching(session, code)
        batting = fetch_team_batting(session, code)
        print(f"{team_name}: {len(pitching)} pitchers, {len(batting)} batters (season tables)")

        for p in pitching:
            full_name = p["name"]
            surname = _surname_key(full_name)
            name = _fullname_key(full_name)
            # appearances come from box scores, which print only the surname;
            # this join is therefore approximate when two same-surname
            # pitchers are active, mostly affecting the (team-aggregated,
            # so resilient) bullpen-workload numbers and the display-only
            # last-5-starts line — never pitcher_factor, which reads season.
            log = appearances.get(surname, [])
            starts = sorted([g for g in log if g["role"] == "先發"], key=lambda g: g["date"], reverse=True)[:5]
            last5 = None
            if starts:
                ip5 = sum(g["ip"] for g in starts)
                er5 = sum(g["er"] for g in starts)
                if ip5 > 0:
                    last5 = {"starts": len(starts), "ip": round(ip5, 1), "era": round(er5 * 9 / ip5, 2)}
            last7d = [g for g in log if g["date"] >= cutoff7]
            relief_recent = [g for g in log if g["role"] != "先發"]
            relief_season = None
            if relief_recent:
                ip_r = sum(g["ip"] for g in relief_recent)
                er_r = sum(g["er"] for g in relief_recent)
                if ip_r > 0:
                    relief_season = {"ip": round(ip_r, 1), "er": er_r, "era": round(er_r * 9 / ip_r, 2)}
            all_pitchers[name] = {
                "full_name": full_name, "surname": surname, "team": team_name, "hand": "",
                "player_id": name_to_id.get(surname, ""),
                "season": {k: v for k, v in p.items() if k != "name"},
                "relief_recent_window": relief_season,
                "last5": last5, "last7d": last7d,
            }

        for b in batting:
            all_batters[b["name"]] = {"team": team_name, "season": {k: v for k, v in b.items() if k != "name"}}

        time.sleep(0.2)

    # hand lookup only for pitchers we could resolve to a player id (keeps
    # this bounded & cheap). The id comes from a surname-keyed box-score
    # scan, so same-surname pitchers can pick up the wrong hand here — a
    # known minor imprecision (NPB's vs-handedness split is empty anyway:
    # finished games in npb_data carry no starter name to build it from).
    print("resolving throwing hand for pitchers with known IDs...")
    for name, info in all_pitchers.items():
        pid = info["player_id"]
        if pid:
            info["hand"] = fetch_hand(session, pid)
            time.sleep(0.15)

    DATA_DIR.mkdir(exist_ok=True)
    with open(DATA_DIR / "npb_pitchers.json", "w", encoding="utf-8") as f:
        json.dump(all_pitchers, f, ensure_ascii=False)
    with open(DATA_DIR / "npb_batters.json", "w", encoding="utf-8") as f:
        json.dump(all_batters, f, ensure_ascii=False)
    print("pitchers:", len(all_pitchers), "batters:", len(all_batters))


if __name__ == "__main__":
    main()
