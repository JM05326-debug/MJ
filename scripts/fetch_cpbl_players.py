# -*- coding: utf-8 -*-
"""Fetch CPBL pitcher & batter data: roster, season stat lines (via each
player's game log), throwing hand, last-5-starts, and last-7-days bullpen
workload. Saves data/cpbl_pitchers.json and data/cpbl_batters.json.
"""
import _console  # noqa: F401  (must import first to fix console encoding)
import json
import re
import time
from datetime import date, timedelta
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0 Safari/537.36"}
BASE = "https://www.cpbl.com.tw"
YEAR = str(date.today().year)

TEAMS = {
    "ACN": "中信兄弟", "ADD": "統一7-ELEVEn獅", "AJL": "樂天桃猿",
    "AEO": "富邦悍將", "AAA": "味全龍", "AKP": "台鋼雄鷹",
}


def get_token(session: requests.Session, url: str) -> str:
    r = session.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    m = re.search(r'RequestVerificationToken:\s*[\'"]([^\'"]+)[\'"]', r.text)
    if m:
        return m.group(1)
    m = re.search(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', r.text)
    return m.group(1) if m else ""


def fetch_roster(session: requests.Session, club_no: str):
    r = session.get(f"{BASE}/team?ClubNo={club_no}", headers=HEADERS, timeout=20)
    r.raise_for_status()
    html = r.text
    items = re.findall(
        r'<div class="pos">([^<]+)</div>\s*<div class="name"><a href="/team/person\?Acnt=(\d+)">([^<]+)</a>',
        html,
    )
    pitchers, batters = [], []
    for pos, acnt, name in items:
        if pos == "投手":
            pitchers.append({"acnt": acnt, "name": name})
        elif pos != "教練":
            batters.append({"acnt": acnt, "name": name, "pos": pos})
    return pitchers, batters


def fetch_hand(session: requests.Session, acnt: str) -> str:
    r = session.get(f"{BASE}/team/person?acnt={acnt}", headers=HEADERS, timeout=20)
    if r.status_code != 200:
        return ""
    m = re.search(r"投打習慣</div>\s*<div[^>]*>([^<]+)</div>", r.text)
    if m:
        return m.group(1).strip()
    m2 = re.search(r"(右投|左投|左右投)(右打|左打|左右打)", r.text)
    return m2.group(0) if m2 else ""


def fetch_follow_score(session: requests.Session, token: str, referer: str, acnt: str, defend_station: str):
    headers = {
        "RequestVerificationToken": token,
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Referer": referer,
    }
    data = {"acnt": acnt, "defendStation": defend_station, "year": YEAR, "kindCode": "A"}
    r = session.post(f"{BASE}/team/getfollowscore", data=data, headers=headers, timeout=20)
    if r.status_code != 200:
        return []
    j = r.json()
    if not j.get("Success"):
        return []
    return json.loads(j.get("FollowScore") or "[]")


def _true_innings(raw: float) -> float:
    """CPBL/NPB innings-pitched fields use baseball notation where the
    fractional digit is OUTS (thirds), e.g. 6.1 = 6 and 1/3 innings, not
    6.1 decimal innings."""
    whole = int(raw)
    outs = round((raw - whole) * 10)  # .0/.1/.2 -> 0/1/2 outs
    return whole + outs / 3.0


def summarize_pitcher(log: list):
    ip = sum(_true_innings(g["InningPitchedCnt"]) for g in log)
    er = sum(g["EarnedRunCnt"] for g in log)
    h = sum(g["HittingCnt"] for g in log)
    bb = sum(g["BasesONBallsCnt"] for g in log)
    hbp = sum(g["HitBYPitchCnt"] for g in log)
    hr = sum(g["HomeRunCnt"] for g in log)
    k = sum(g["StrikeOutCnt"] for g in log)
    if ip <= 0:
        return None
    era = er * 9 / ip
    whip = (h + bb) / ip
    k9 = k * 9 / ip
    bb9 = bb * 9 / ip
    fip_raw = (13 * hr + 3 * (bb + hbp) - 2 * k) / ip
    return {
        "ip": round(ip, 1), "er": er, "h": h, "bb": bb, "hbp": hbp, "hr": hr, "k": k,
        "era": round(era, 2), "whip": round(whip, 2), "k9": round(k9, 2), "bb9": round(bb9, 2),
        "fip_raw": round(fip_raw, 2),  # league FIP constant added later in model
    }


def summarize_batter(log: list):
    ab = sum(g["HitCnt"] for g in log)  # field name is misleading: this is at-bats
    h = sum(g["HittingCnt"] for g in log)
    bb = sum(g["BasesONBallsCnt"] for g in log)
    hbp = sum(g["HitBYPitchCnt"] for g in log)
    sf = sum(g["SacrificeFlyCnt"] for g in log)
    tb = sum(g["TotalBases"] for g in log)
    pa = sum(g["PlateAppearances"] for g in log)
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

    all_pitchers = {}
    all_batters = {}

    for club_no, team_name in TEAMS.items():
        pitchers, batters = fetch_roster(session, club_no)
        print(f"{team_name}: {len(pitchers)} pitchers, {len(batters)} batters")

        for p in pitchers:
            acnt = p["acnt"]
            referer = f"{BASE}/team/follow?Acnt={acnt}"
            token = get_token(session, referer)
            log = fetch_follow_score(session, token, referer, acnt, "投手")
            hand = fetch_hand(session, acnt)
            season = summarize_pitcher(log)
            relief_log = [g for g in log if g.get("RoleType") != "先發"]
            relief_season = summarize_pitcher(relief_log) if relief_log else None
            starts = sorted(
                [g for g in log if g.get("RoleType") == "先發"],
                key=lambda g: g["GameDate"], reverse=True,
            )[:5]
            last5 = summarize_pitcher(starts) if starts else None
            last7d_relief = [
                {
                    "date": g["GameDate"][:10], "role": g.get("RoleType", ""),
                    "ip": round(_true_innings(g["InningPitchedCnt"]), 2), "pitches": g.get("PitchCnt", 0),
                }
                for g in log if g["GameDate"][:10] >= cutoff7
            ]
            all_pitchers[p["name"]] = {
                "acnt": acnt, "team": team_name, "hand": hand,
                "season": season, "relief_season": relief_season, "last5": last5, "last7d": last7d_relief,
                "recent30_starts": len([g for g in log if g["GameDate"][:10] >= cutoff30 and g.get("RoleType") == "先發"]),
            }
            time.sleep(0.15)

        for b in batters:
            acnt = b["acnt"]
            referer = f"{BASE}/team/follow?Acnt={acnt}"
            token = get_token(session, referer)
            log = fetch_follow_score(session, token, referer, acnt, "野手")
            season = summarize_batter(log)
            if season:
                all_batters[b["name"]] = {"acnt": acnt, "team": team_name, "pos": b["pos"], "season": season}
            time.sleep(0.15)

        print(f"  done {team_name}")

    DATA_DIR.mkdir(exist_ok=True)
    with open(DATA_DIR / "cpbl_pitchers.json", "w", encoding="utf-8") as f:
        json.dump(all_pitchers, f, ensure_ascii=False)
    with open(DATA_DIR / "cpbl_batters.json", "w", encoding="utf-8") as f:
        json.dump(all_batters, f, ensure_ascii=False)
    print("pitchers:", len(all_pitchers), "batters:", len(all_batters))


if __name__ == "__main__":
    main()
