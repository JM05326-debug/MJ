# -*- coding: utf-8 -*-
"""Fetch CPBL pitcher & batter data: season stat lines (from each player's
per-game logs), throwing hand, last-5-starts, and last-7-days bullpen
workload. Saves data/cpbl_pitchers.json and data/cpbl_batters.json.

Sourced from Rebas 野球革命 (rebas.tw)'s public JSON API — see
scripts/fetch_cpbl.py for why (GitHub Actions can reach rebas.tw, unlike
www.cpbl.com.tw which this replaces). Output shape is unchanged so
scripts/context.py and pipeline/feature_spec.py don't need to change.

Depends on data/cpbl_data.json already being fresh (fetch_cpbl.py runs
first in both the workflow and the old local batch script) — its h/v +
h_pitcher/v_pitcher columns are the only source of "who started this game"
info, since rebas's per-game box line for a pitcher doesn't carry a role
flag the way the old CPBL API's RoleType did.
"""
import _console  # noqa: F401  (must import first to fix console encoding)
import json
import time
from datetime import date, timedelta
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0 Safari/537.36"}
BASE = "https://www.rebas.tw"
REQUEST_PAUSE = 0.12


def find_current_season(session: requests.Session) -> tuple[str, str]:
    """Returns (season_uniqid, year) for the CPBL regular season currently
    in progress, by scanning the calendar around today for a regular-season
    game (title=='中職{year}年' — same filter fetch_cpbl.py uses to exclude
    exhibition/postseason)."""
    start = (date.today() - timedelta(days=14)).isoformat()
    r = session.get(
        f"{BASE}/api/formal/calendar",
        params={"start": start, "days": 29, "league_uniqid": "CPBL"},
        headers=HEADERS, timeout=30,
    )
    r.raise_for_status()
    for g in r.json().get("data") or []:
        season = g.get("season") or {}
        year = season.get("uniqid", "").split("-")[1] if season.get("uniqid") else ""
        if year and season.get("title") == f"中職{year}年":
            return season["uniqid"], year
    raise RuntimeError("could not find a current CPBL regular-season game in +/-14 days to identify season_uniqid")


def fetch_teams(session: requests.Session, season_uniqid: str) -> dict:
    """team_uniqid -> official team name."""
    r = session.get(f"{BASE}/api/seasons/{season_uniqid}/teams", headers=HEADERS, timeout=30)
    r.raise_for_status()
    return {t["origin"]["uniqid"]: t["name"] for t in r.json().get("data") or []}


def fetch_leaders(session: requests.Session, season_uniqid: str, player_type: str) -> list:
    """Full-season list of every player with at least one appearance
    (omitting the `pa` param, unlike the site's own UI which defaults to a
    qualifying minimum, returns the whole league — verified: 155 pitchers
    league-wide vs. a handful when a minimum is applied)."""
    r = session.get(
        f"{BASE}/api/seasons/{season_uniqid}/leaders",
        params={"type": player_type, "section": "new"},
        headers=HEADERS, timeout=30,
    )
    r.raise_for_status()
    return r.json().get("data") or []


def fetch_player_logs(session: requests.Session, uniqid: str, season_uniqid: str) -> list:
    r = session.get(
        f"{BASE}/api/formal/players/{uniqid}/seasons/{season_uniqid}/logs",
        headers=HEADERS, timeout=30,
    )
    if r.status_code != 200:
        return []
    return r.json().get("data") or []


def build_starter_lookup() -> dict:
    """(date, team_name) -> starting pitcher name, from the schedule data
    fetch_cpbl.py already wrote (its h_pitcher/v_pitcher come from rebas's
    scheduled_SP, i.e. the actual/probable starter)."""
    path = DATA_DIR / "cpbl_data.json"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    lookup = {}
    for row in d.get("history", []) + d.get("upcoming", []):
        if row.get("h_pitcher"):
            lookup[(row["date"], row["h"])] = row["h_pitcher"]
        if row.get("v_pitcher"):
            lookup[(row["date"], row["v"])] = row["v_pitcher"]
    return lookup


def _hand_from_logs(games: list, hand_key: str) -> str:
    """hand_key: 'p_hand' for pitchers, 'b_hand' for batters. Pulled from
    the first plate appearance of the first game with any PA_list data —
    rebas doesn't need a separate player-detail call for this since every
    PA event already carries both hands."""
    for g in games:
        pa_list = g.get("PA_list") or []
        if pa_list:
            code = pa_list[0].get(hand_key)
            if code == "L":
                return "左投" if hand_key == "p_hand" else "左打"
            if code == "R":
                return "右投" if hand_key == "p_hand" else "右打"
    return ""


def summarize_pitcher(games: list) -> dict | None:
    if not games:
        return None
    ip = sum(g["pitching"]["IPOut"] for g in games) / 3.0
    if ip <= 0:
        return None
    er = sum(g["pitching"]["ER"] for g in games)
    h = sum(g["pitching"]["H"] for g in games)
    bb = sum(g["pitching"]["BB"] for g in games)
    # rebas's per-game pitching summary doesn't carry HBP (unlike the batting
    # side, which does) — treated as 0 rather than fetching+parsing the full
    # pitch-by-pitch PA_list just for this one field. Slightly understates
    # fip_raw's (BB+HBP) term.
    hbp = 0
    hr = sum(g["pitching"]["HR"] for g in games)
    k = sum(g["pitching"]["SO"] for g in games)
    era = er * 9 / ip
    whip = (h + bb) / ip
    k9 = k * 9 / ip
    bb9 = bb * 9 / ip
    fip_raw = (13 * hr + 3 * (bb + hbp) - 2 * k) / ip
    return {
        "ip": round(ip, 1), "er": er, "h": h, "bb": bb, "hbp": hbp, "hr": hr, "k": k,
        "era": round(era, 2), "whip": round(whip, 2), "k9": round(k9, 2), "bb9": round(bb9, 2),
        "fip_raw": round(fip_raw, 2),
    }


def summarize_batter(games: list) -> dict | None:
    if not games:
        return None
    ab = sum(g["batting"]["AB"] for g in games)
    h = sum(g["batting"]["H"] for g in games)
    bb = sum(g["batting"]["BB"] for g in games)
    hbp = sum(g["batting"]["HBP"] for g in games)
    sf = sum(g["batting"]["SF"] for g in games)
    pa = sum(g["batting"]["PA"] for g in games)
    doubles = sum(g["batting"]["Double"] for g in games)
    triples = sum(g["batting"]["Triple"] for g in games)
    hr = sum(g["batting"]["HR"] for g in games)
    singles = h - doubles - triples - hr
    tb = singles + 2 * doubles + 3 * triples + 4 * hr
    obp_den = ab + bb + hbp + sf
    if ab <= 0 or obp_den <= 0:
        return None
    obp = (h + bb + hbp) / obp_den
    slg = tb / ab
    return {
        "pa": pa, "ab": ab, "h": h, "bb": bb, "hbp": hbp, "sf": sf, "tb": tb,
        "avg": round(h / ab, 3), "obp": round(obp, 3), "slg": round(slg, 3), "ops": round(obp + slg, 3),
    }


def main():
    today = date.today()
    cutoff7 = (today - timedelta(days=7)).isoformat()
    cutoff30 = (today - timedelta(days=30)).isoformat()

    session = requests.Session()
    session.headers.update(HEADERS)

    season_uniqid, year = find_current_season(session)
    print(f"season: {season_uniqid} ({year})")
    team_names = fetch_teams(session, season_uniqid)
    starter_lookup = build_starter_lookup()

    all_pitchers = {}
    pitcher_roster = fetch_leaders(session, season_uniqid, "pitcher")
    print(f"pitcher roster: {len(pitcher_roster)}")
    for entry in pitcher_roster:
        p = entry["player"]
        uniqid, name, team_uniqid = p["uniqid"], p["name"], p["team_uniqid"]
        team = team_names.get(team_uniqid, "")
        games = fetch_player_logs(session, uniqid, season_uniqid)
        time.sleep(REQUEST_PAUSE)
        # A two-way/emergency appearance can log a game for this player with
        # only a "batting" entry (e.g. pinch-hit) and no "pitching" line —
        # irrelevant to pitcher stats, so drop it here.
        games = [g for g in games if "pitching" in g]
        if not games:
            continue
        games.sort(key=lambda g: g["date"])
        for g in games:
            g["_is_start"] = starter_lookup.get((g["date"], team)) == name

        hand = _hand_from_logs(games, "p_hand")
        season = summarize_pitcher(games)
        start_games = [g for g in games if g["_is_start"]]
        relief_games = [g for g in games if not g["_is_start"]]
        relief_season = summarize_pitcher(relief_games) if relief_games else None
        last5 = summarize_pitcher(sorted(start_games, key=lambda g: g["date"], reverse=True)[:5]) if start_games else None
        last7d_relief = [
            {
                "date": g["date"], "role": "後援",
                "ip": round(g["pitching"]["IPOut"] / 3.0, 2), "pitches": g["pitching"].get("NP", 0),
            }
            for g in relief_games if g["date"] >= cutoff7
        ]
        all_pitchers[name] = {
            "acnt": uniqid, "team": team, "hand": hand,
            "season": season, "relief_season": relief_season, "last5": last5, "last7d": last7d_relief,
            "recent30_starts": len([g for g in start_games if g["date"] >= cutoff30]),
        }

    all_batters = {}
    batter_roster = fetch_leaders(session, season_uniqid, "batter")
    print(f"batter roster: {len(batter_roster)}")
    for entry in batter_roster:
        p = entry["player"]
        uniqid, name, team_uniqid = p["uniqid"], p["name"], p["team_uniqid"]
        team = team_names.get(team_uniqid, "")
        games = fetch_player_logs(session, uniqid, season_uniqid)
        time.sleep(REQUEST_PAUSE)
        games = [g for g in games if "batting" in g]
        if not games:
            continue
        season = summarize_batter(games)
        if season:
            all_batters[name] = {"acnt": uniqid, "team": team, "pos": "", "season": season}

    DATA_DIR.mkdir(exist_ok=True)
    with open(DATA_DIR / "cpbl_pitchers.json", "w", encoding="utf-8") as f:
        json.dump(all_pitchers, f, ensure_ascii=False)
    with open(DATA_DIR / "cpbl_batters.json", "w", encoding="utf-8") as f:
        json.dump(all_batters, f, ensure_ascii=False)
    print("pitchers:", len(all_pitchers), "batters:", len(all_batters))


if __name__ == "__main__":
    main()
