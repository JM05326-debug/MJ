# -*- coding: utf-8 -*-
"""Build the static prediction website from data/cpbl_data.json,
data/npb_data.json, and each league's pitcher/batter stat files into
site/index.html.
"""
import _console  # noqa: F401  (must import first to fix console encoding)
import json
from datetime import datetime, date
from pathlib import Path

from model import compute_elo, compute_poisson_ratings
from context import (
    build_vs_hand_splits, contextual_predict_game, league_fip_constant,
    league_avg_bullpen_ip_7d, team_bullpen_era,
)

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SITE_DIR = ROOT / "site"

TEAM_ALIAS = {
    # unify CPBL's pre-2020 franchise name with its current one
    "Lamigo": "樂天桃猿",
}


def load_league_games(filename: str):
    with open(DATA_DIR / filename, encoding="utf-8") as f:
        raw = json.load(f)
    history = []
    for g in raw["history"]:
        v = TEAM_ALIAS.get(g["v"], g["v"])
        h = TEAM_ALIAS.get(g["h"], g["h"])
        history.append({**g, "v": v, "h": h})
    upcoming = []
    for g in raw["upcoming"]:
        v = TEAM_ALIAS.get(g["v"], g["v"])
        h = TEAM_ALIAS.get(g["h"], g["h"])
        upcoming.append({**g, "v": v, "h": h})
    return history, upcoming


def load_players(pitchers_file: str, batters_file: str):
    pitchers, batters = {}, {}
    p_path, b_path = DATA_DIR / pitchers_file, DATA_DIR / batters_file
    if p_path.exists():
        with open(p_path, encoding="utf-8") as f:
            pitchers = json.load(f)
    if b_path.exists():
        with open(b_path, encoding="utf-8") as f:
            batters = json.load(f)
    return pitchers, batters


def load_odds(odds_file: str | None):
    """Best-effort: playsport.cc market odds, keyed by (date, home, away).
    Missing/empty file just means no market-odds pre-fill for this run."""
    if not odds_file:
        return {}
    path = DATA_DIR / odds_file
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        games = json.load(f)
    out = {}
    for g in games:
        out[(g["date"], g["home"], g["away"])] = g
    return out


def team_batting_ops(team: str, batters: dict):
    ab = h = bb = hbp = sf = tb = 0
    for b in batters.values():
        if b.get("team") != team:
            continue
        s = b["season"]
        ab += s["ab"]; h += s["h"]; bb += s["bb"]; hbp += s["hbp"]; sf += s["sf"]; tb += s["tb"]
    if ab <= 0:
        return None
    obp_den = ab + bb + hbp + sf
    obp = (h + bb + hbp) / obp_den if obp_den else 0
    slg = tb / ab
    return round(obp + slg, 3)


def pitcher_display_stats(name: str, pitchers: dict):
    p = pitchers.get(name)
    if not p or not p.get("season"):
        return None
    s = p["season"]
    return {
        "name": p.get("full_name", name), "hand": p.get("hand", ""),
        "era": s["era"], "whip": s["whip"], "k9": s["k9"], "bb9": s["bb9"],
        "last5_era": (p.get("last5") or {}).get("era"),
    }


def build_league(name: str, game_file: str, pitchers_file: str, batters_file: str,
                  relief_field: str, today: date, odds_file: str | None = None):
    history, upcoming = load_league_games(game_file)
    pitchers, batters = load_players(pitchers_file, batters_file)
    odds_map = load_odds(odds_file)
    elo_ratings, elo_history = compute_elo(history)
    poisson = compute_poisson_ratings(history, as_of=today)

    hand_map = {n: p.get("hand", "") for n, p in pitchers.items()}
    vs_hand_splits = build_vs_hand_splits(history, hand_map)
    fip_constant, league_era = league_fip_constant(pitchers)
    league_ip7 = league_avg_bullpen_ip_7d(pitchers)

    teams = sorted(elo_ratings.keys(), key=lambda t: -elo_ratings[t])
    rankings = []
    for i, t in enumerate(teams, start=1):
        team_games = sorted(
            [g for g in history if g["h"] == t or g["v"] == t],
            key=lambda g: g["date"],
        )[-10:]
        wins = 0
        for g in team_games:
            is_home = g["h"] == t
            ts = g["hs"] if is_home else g["vs"]
            os_ = g["vs"] if is_home else g["hs"]
            if ts > os_:
                wins += 1
        rankings.append({
            "rank": i,
            "team": t,
            "elo": round(elo_ratings[t], 1),
            "offense": round(poisson["offense"].get(t, 1.0), 3),
            "defense": round(poisson["defense"].get(t, 1.0), 3),
            "recent_form": f"{wins}-{len(team_games) - wins}",
            "team_ops": team_batting_ops(t, batters),
            "bullpen_era": team_bullpen_era(t, pitchers, relief_field),
        })

    upcoming_sorted = sorted(
        [g for g in upcoming if g["date"] >= today.isoformat()],
        key=lambda g: g["date"],
    )
    seen = set()
    predictions = []
    for g in upcoming_sorted:
        key = (g["date"], g["h"], g["v"])
        if key in seen:
            continue
        seen.add(key)
        odds_entry = odds_map.get((g["date"], g["h"], g["v"]))
        h_pitcher = g.get("h_pitcher") or (odds_entry.get("home_pitcher") if odds_entry else None) or None
        v_pitcher = g.get("v_pitcher") or (odds_entry.get("away_pitcher") if odds_entry else None) or None
        pred = contextual_predict_game(
            g["h"], g["v"], elo_ratings, poisson,
            home_starter=h_pitcher, away_starter=v_pitcher,
            pitchers=pitchers, relief_field=relief_field,
            hand_map=hand_map, vs_hand_splits=vs_hand_splits,
            fip_constant=fip_constant, league_era=league_era, league_avg_ip7=league_ip7,
        )
        pred["date"] = g["date"]
        pred["home_pitcher_stats"] = pitcher_display_stats(h_pitcher, pitchers) if h_pitcher else None
        pred["away_pitcher_stats"] = pitcher_display_stats(v_pitcher, pitchers) if v_pitcher else None
        pred["market_odds"] = {
            "home_moneyline": odds_entry.get("home_moneyline"),
            "away_moneyline": odds_entry.get("away_moneyline"),
            "ou_line": odds_entry.get("ou_line"),
            "over_odds": odds_entry.get("over_odds"),
            "under_odds": odds_entry.get("under_odds"),
        } if odds_entry else None
        predictions.append(pred)
        if len(predictions) >= 40:
            break

    last_game_date = max(g["date"] for g in history)

    return {
        "name": name,
        "rankings": rankings,
        "predictions": predictions,
        "team_list": teams,
        "elo_ratings": {t: round(v, 1) for t, v in elo_ratings.items()},
        "poisson": {
            "offense": {t: round(v, 3) for t, v in poisson["offense"].items()},
            "defense": {t: round(v, 3) for t, v in poisson["defense"].items()},
            "league_avg_runs": round(poisson["league_avg_runs"], 3),
        },
        "last_game_date": last_game_date,
        "total_games": len(history),
        "has_player_data": bool(pitchers),
    }


def render_html(cpbl: dict, npb: dict, generated_at: str) -> str:
    template = (ROOT / "scripts" / "template.html").read_text(encoding="utf-8")
    payload = json.dumps({"cpbl": cpbl, "npb": npb}, ensure_ascii=False)
    html = template.replace("__GENERATED_AT__", generated_at)
    html = html.replace("__DATA_JSON__", payload)
    return html


def main():
    today = date.today()
    cpbl = build_league(
        "CPBL 中華職棒", "cpbl_data.json", "cpbl_pitchers.json", "cpbl_batters.json",
        "relief_season", today, odds_file="cpbl_odds.json",
    )
    npb = build_league(
        "NPB 日本職棒", "npb_data.json", "npb_pitchers.json", "npb_batters.json",
        "relief_recent_window", today,
    )
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")

    html = render_html(cpbl, npb, generated_at)
    SITE_DIR.mkdir(exist_ok=True)
    out_path = SITE_DIR / "index.html"
    out_path.write_text(html, encoding="utf-8")
    print("wrote", out_path)
    print("CPBL games:", cpbl["total_games"], "last:", cpbl["last_game_date"],
          "upcoming preds:", len(cpbl["predictions"]), "players:", cpbl["has_player_data"])
    print("NPB games:", npb["total_games"], "last:", npb["last_game_date"],
          "upcoming preds:", len(npb["predictions"]), "players:", npb["has_player_data"])


if __name__ == "__main__":
    main()
