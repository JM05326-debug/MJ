# -*- coding: utf-8 -*-
"""Game-context adjustment layer: starting-pitcher quality, bullpen
ERA/fatigue, and batting-vs-handedness splits, layered on top of the base
Elo+Poisson model (model.py) for upcoming-game predictions.

Design note: the existing Poisson offense/defense factors (model.py) are
derived from a team's own runs-scored/allowed history, which already
implicitly reflects average pitching-staff and lineup quality. To avoid
double-counting that signal, each context adjustment here is blended
50/50 with the existing factor rather than replacing it outright — new,
more granular information (this start's actual pitcher, this week's
actual bullpen workload) tempers, but does not override, the proven
season-long trend. Every adjustment gracefully falls back to "no change"
when the underlying data (starter identity, hand, sample size) isn't
available, which is common for games more than a few days out.
"""
from __future__ import annotations

from collections import defaultdict

from model import ELO_HOME_ADV, ELO_INITIAL, POISSON_HOME_ADV, elo_win_prob, poisson_win_prob

STARTER_WEIGHT = 0.55          # fraction of a game a starter's own quality governs; rest is bullpen
PITCHER_REGRESSION_IP = 25.0   # innings of "trust" before leaning fully on a starter's own FIP
BULLPEN_REGRESSION_IP = 20.0   # innings of trust for a team's bullpen-ERA sample
FATIGUE_IP_PENALTY = 0.02      # extra run-multiplier per inning above league-average 7-day bullpen usage
FATIGUE_CAP = 1.25
HAND_MIN_SAMPLE = 8            # minimum games vs. a given starter hand before trusting the split
HAND_ADJ_CAP = (0.80, 1.25)
BLEND_WITH_HISTORY = 0.5       # weight on the new context factor vs. the existing history-based factor


def league_fip_constant(pitchers: dict) -> tuple[float, float]:
    """Return (fip_constant, league_era) computed from every pitcher's season line."""
    ip = er = hr = bb = hbp = k = 0.0
    for p in pitchers.values():
        s = p.get("season")
        if not s:
            continue
        ip += s["ip"]
        er += s["er"]
        hr += s["hr"]
        bb += s["bb"]
        hbp += s.get("hbp", 0)
        k += s["k"]
    if ip <= 0:
        return 3.10, 4.00
    league_era = er * 9 / ip
    fip_raw = (13 * hr + 3 * (bb + hbp) - 2 * k) / ip
    return league_era - fip_raw, league_era


def pitcher_factor(name: str, pitchers: dict, fip_constant: float, league_era: float) -> float | None:
    """<1 = better than league-average starter (suppresses opponent runs). None if unknown."""
    p = pitchers.get(name)
    if not p or not p.get("season") or league_era <= 0:
        return None
    s = p["season"]
    ip = s["ip"]
    if ip <= 0:
        return None
    fip = s["fip_raw"] + fip_constant
    raw = max(0.55, min(1.8, fip / league_era))
    blend = ip / (ip + PITCHER_REGRESSION_IP)
    return 1 + blend * (raw - 1)


def team_bullpen_7d_ip(team: str, pitchers: dict) -> float:
    total = 0.0
    for p in pitchers.values():
        if p.get("team") != team:
            continue
        for g in p.get("last7d", []):
            if g.get("role") != "先發":
                total += g["ip"]
    return total


def league_avg_bullpen_ip_7d(pitchers: dict) -> float:
    totals = defaultdict(float)
    teams = set()
    for p in pitchers.values():
        teams.add(p.get("team"))
    for team in teams:
        totals[team] = team_bullpen_7d_ip(team, pitchers)
    if not totals:
        return 0.0
    return sum(totals.values()) / len(totals)


def bullpen_fatigue_factor(team: str, pitchers: dict, league_avg_ip7: float) -> float:
    if league_avg_ip7 <= 0:
        return 1.0
    excess = max(0.0, team_bullpen_7d_ip(team, pitchers) - league_avg_ip7)
    return min(1.0 + excess * FATIGUE_IP_PENALTY, FATIGUE_CAP)


def _team_relief_totals(team: str, pitchers: dict, relief_field: str):
    ip = er = 0.0
    for p in pitchers.values():
        if p.get("team") != team:
            continue
        r = p.get(relief_field)
        if r:
            ip += r["ip"]
            er += r["er"]
    return ip, er


def team_bullpen_era(team: str, pitchers: dict, relief_field: str) -> float | None:
    ip, er = _team_relief_totals(team, pitchers, relief_field)
    return round(er * 9 / ip, 2) if ip > 0 else None


def bullpen_quality_factor(team: str, pitchers: dict, relief_field: str, league_era: float) -> float:
    if league_era <= 0:
        return 1.0
    ip, er = _team_relief_totals(team, pitchers, relief_field)
    if ip <= 0:
        return 1.0
    era = er * 9 / ip
    raw = max(0.6, min(1.7, era / league_era))
    blend = ip / (ip + BULLPEN_REGRESSION_IP)
    return 1 + blend * (raw - 1)


def _norm_name(s: str) -> str:
    """Strip the roster '*' marker, the full-width space season tables put
    between family and given name, and any ASCII spaces — so the many name
    forms for one pitcher ('*髙橋　宏斗', '髙橋宏斗', '髙橋宏', 'Howard ') can
    be compared on equal footing."""
    return (s or "").lstrip("*").replace("　", "").replace(" ", "").strip()


def resolve_pitcher_name(raw: str | None, pitchers: dict | None) -> str | None:
    """Map a probable-starter name (playsport.cc and each league schedule
    abbreviate the same pitcher differently) to the matching key in
    `pitchers`. Returns the canonical key, or None when it can't be matched
    *unambiguously* — the caller then treats the starter as unknown rather
    than pinning the wrong pitcher's stat line onto the prediction (NPB has
    several same-surname pitchers, e.g. 髙橋宏斗 / 髙橋光成 / 髙橋快秀)."""
    if not raw:
        return None
    if not pitchers:
        return raw
    if raw in pitchers:
        return raw
    target = _norm_name(raw)
    if not target:
        return None
    for k, v in pitchers.items():
        if _norm_name(k) == target or _norm_name(v.get("full_name", "")) == target:
            return k
    # a bare family name (npb.jp schedules print only that) — take it only
    # if exactly one active pitcher has that surname (declines 村上, which
    # is both 村上頌樹 and 村上泰斗)
    by_surname = [k for k, v in pitchers.items() if _norm_name(v.get("surname", "")) == target]
    if len(by_surname) == 1:
        return by_surname[0]
    # one name a prefix of the other ('髙橋宏' vs roster '髙橋宏斗') — only
    # when unambiguous, and never letting a bare 2-char surname swallow a
    # longer name (that's how you pin 髙橋宏斗's start on 髙橋快秀's stats).
    cands = set()
    for k, v in pitchers.items():
        for cand in (_norm_name(k), _norm_name(v.get("full_name", ""))):
            if not cand or cand == target:
                continue
            shorter = min(cand, target, key=len)
            if len(shorter) >= 3 and (cand.startswith(target) or target.startswith(cand)):
                cands.add(k)
    return next(iter(cands)) if len(cands) == 1 else None


def pick_starter(primary: str | None, secondary: str | None, pitchers: dict | None) -> str | None:
    """Choose which probable-starter name to use, given two feeds for the
    same game. `primary` is playsport.cc (玩運彩) — the preferred source, and
    usually the earliest to post — and `secondary` is the league's own feed
    (rebas for CPBL, npb.jp for NPB).

    Prefer `primary`, unless it can't be resolved to a known pitcher while
    `secondary` can — then `secondary` keeps the starter-quality factor
    (matters for foreign pitchers, where playsport uses romaji 'Howard' and
    npb.jp uses katakana 'ハワード' that matches the roster). Falls back to
    whichever one is non-empty when neither resolves — nothing to lose."""
    primary = (primary or "").strip() or None
    secondary = (secondary or "").strip() or None
    if primary and not resolve_pitcher_name(primary, pitchers) \
            and resolve_pitcher_name(secondary, pitchers):
        return secondary
    return primary or secondary


def hand_of(pitcher_name: str, hand_map: dict) -> str | None:
    raw = hand_map.get(pitcher_name, "")
    if raw.startswith("左投"):
        return "L"
    if raw.startswith("右投"):
        return "R"
    return None


def build_vs_hand_splits(history: list, hand_map: dict) -> dict:
    """team -> {"L": factor|None, "R": factor|None}: the team's own average
    runs scored in games where the opposing starter's hand is known,
    relative to the team's overall average (only kept above HAND_MIN_SAMPLE)."""
    sums = defaultdict(lambda: {"L": [0.0, 0], "R": [0.0, 0], "all": [0.0, 0]})
    for g in history:
        for side, opp_field, runs_field in (("v", "h_pitcher", "vs"), ("h", "v_pitcher", "hs")):
            team = g[side]
            runs = g.get(runs_field)
            if runs is None:
                continue
            sums[team]["all"][0] += runs
            sums[team]["all"][1] += 1
            hand = hand_of(g.get(opp_field, ""), hand_map)
            if hand:
                sums[team][hand][0] += runs
                sums[team][hand][1] += 1

    out = {}
    for team, d in sums.items():
        overall_n = d["all"][1]
        overall_avg = d["all"][0] / overall_n if overall_n else None
        entry = {}
        for hand in ("L", "R"):
            total, n = d[hand]
            if n >= HAND_MIN_SAMPLE and overall_avg:
                factor = (total / n) / overall_avg
                entry[hand] = round(max(HAND_ADJ_CAP[0], min(HAND_ADJ_CAP[1], factor)), 3)
            else:
                entry[hand] = None
        out[team] = entry
    return out


def build_context_features(
    home: str, away: str, elo_ratings: dict, poisson: dict, *,
    home_starter: str | None = None, away_starter: str | None = None,
    pitchers: dict | None = None, relief_field: str = "relief_season",
    hand_map: dict | None = None, vs_hand_splits: dict | None = None,
    fip_constant: float = 0.0, league_era: float = 4.0, league_avg_ip7: float = 0.0,
) -> dict:
    """Compute the raw ingredients of a contextual prediction — Elo/Poisson
    win probabilities, expected runs, and the per-signal adjustment notes —
    without yet turning them into a final blended verdict. This is the
    single function both the live prediction path (contextual_predict_game,
    below) and the offline training/backfill pipeline call, so online and
    offline feature computation can never drift apart (classic MLOps
    training/serving skew, avoided by construction). Any missing signal
    (pitchers=None, hand_map=None, vs_hand_splits=None, or an unrecognized
    starter name) simply leaves that entry in `notes` as None and the
    corresponding factor unadjusted from the base Elo/Poisson value —
    this is also exactly the degradation historical backfill relies on."""
    rh = elo_ratings.get(home, ELO_INITIAL)
    rv = elo_ratings.get(away, ELO_INITIAL)
    elo_p_home = elo_win_prob(rh, rv)

    league_avg = poisson["league_avg_runs"]
    off_h = poisson["offense"].get(home, 1.0)
    def_h = poisson["defense"].get(home, 1.0)
    off_v = poisson["offense"].get(away, 1.0)
    def_v = poisson["defense"].get(away, 1.0)

    notes = {"home_pitcher_factor": None, "away_pitcher_factor": None,
             "home_bullpen_factor": None, "away_bullpen_factor": None,
             "home_hand_factor": None, "away_hand_factor": None}

    def_h_ctx, def_v_ctx = def_h, def_v
    off_h_ctx, off_v_ctx = off_h, off_v

    if pitchers is not None:
        # Normalize each probable starter to its canonical key in `pitchers`
        # before any lookup — playsport.cc and the league schedules each
        # abbreviate names differently and NPB stat lines are keyed by full
        # name. Unresolved names stay as-is (factor just comes back None).
        home_starter = resolve_pitcher_name(home_starter, pitchers) or home_starter
        away_starter = resolve_pitcher_name(away_starter, pitchers) or away_starter

        # home pitching (starter + bullpen) shapes the AWAY team's expected runs
        home_pen_factor = bullpen_quality_factor(home, pitchers, relief_field, league_era) * \
            bullpen_fatigue_factor(home, pitchers, league_avg_ip7)
        away_pen_factor = bullpen_quality_factor(away, pitchers, relief_field, league_era) * \
            bullpen_fatigue_factor(away, pitchers, league_avg_ip7)
        notes["home_bullpen_factor"] = round(home_pen_factor, 3)
        notes["away_bullpen_factor"] = round(away_pen_factor, 3)

        home_starter_f = pitcher_factor(home_starter, pitchers, fip_constant, league_era) if home_starter else None
        away_starter_f = pitcher_factor(away_starter, pitchers, fip_constant, league_era) if away_starter else None
        notes["home_pitcher_factor"] = round(home_starter_f, 3) if home_starter_f else None
        notes["away_pitcher_factor"] = round(away_starter_f, 3) if away_starter_f else None

        if home_starter_f is not None:
            pitching_ctx = STARTER_WEIGHT * home_starter_f + (1 - STARTER_WEIGHT) * home_pen_factor
        else:
            pitching_ctx = home_pen_factor
        def_h_ctx = (1 - BLEND_WITH_HISTORY) * def_h + BLEND_WITH_HISTORY * (def_h * pitching_ctx)

        if away_starter_f is not None:
            pitching_ctx = STARTER_WEIGHT * away_starter_f + (1 - STARTER_WEIGHT) * away_pen_factor
        else:
            pitching_ctx = away_pen_factor
        def_v_ctx = (1 - BLEND_WITH_HISTORY) * def_v + BLEND_WITH_HISTORY * (def_v * pitching_ctx)

    if vs_hand_splits is not None and hand_map is not None:
        # home team bats against the AWAY starter; away team bats against the HOME starter
        home_bat_vs_hand = hand_of(away_starter, hand_map) if away_starter else None
        away_bat_vs_hand = hand_of(home_starter, hand_map) if home_starter else None

        if home_bat_vs_hand:
            f = vs_hand_splits.get(home, {}).get(home_bat_vs_hand)
            if f is not None:
                notes["home_hand_factor"] = f
                off_h_ctx = (1 - BLEND_WITH_HISTORY) * off_h + BLEND_WITH_HISTORY * (off_h * f)
        if away_bat_vs_hand:
            f = vs_hand_splits.get(away, {}).get(away_bat_vs_hand)
            if f is not None:
                notes["away_hand_factor"] = f
                off_v_ctx = (1 - BLEND_WITH_HISTORY) * off_v + BLEND_WITH_HISTORY * (off_v * f)

    lam_home = league_avg * off_h_ctx * def_v_ctx * POISSON_HOME_ADV
    lam_away = league_avg * off_v_ctx * def_h_ctx

    poi_p_home, poi_tie, poi_p_away = poisson_win_prob(lam_home, lam_away)

    return {
        "home": home, "away": away,
        "home_starter": home_starter or "", "away_starter": away_starter or "",
        "elo_home_rating": rh, "elo_away_rating": rv,
        "elo_home_win_pct": elo_p_home * 100,
        "poisson_home_win_pct": poi_p_home * 100,
        "lam_home": lam_home, "lam_away": lam_away,
        "elo_p_home": elo_p_home, "poisson_p_home": poi_p_home,
        "context": notes,
    }


def contextual_predict_game(
    home: str, away: str, elo_ratings: dict, poisson: dict, *,
    home_starter: str | None = None, away_starter: str | None = None,
    pitchers: dict | None = None, relief_field: str = "relief_season",
    hand_map: dict | None = None, vs_hand_splits: dict | None = None,
    fip_constant: float = 0.0, league_era: float = 4.0, league_avg_ip7: float = 0.0,
) -> dict:
    """Extends the base Elo+Poisson prediction with starter/bullpen/
    handedness context. Any missing signal simply falls back to the base
    model's behavior for that piece. Thin wrapper around
    build_context_features() that turns the raw ingredients into the final
    blended verdict + confidence rating."""
    feats = build_context_features(
        home, away, elo_ratings, poisson,
        home_starter=home_starter, away_starter=away_starter,
        pitchers=pitchers, relief_field=relief_field,
        hand_map=hand_map, vs_hand_splits=vs_hand_splits,
        fip_constant=fip_constant, league_era=league_era, league_avg_ip7=league_avg_ip7,
    )
    elo_p_home = feats["elo_p_home"]
    poi_p_home = feats["poisson_p_home"]
    lam_home, lam_away = feats["lam_home"], feats["lam_away"]
    notes = feats["context"]

    blended_home = (elo_p_home + poi_p_home) / 2.0

    # feats already carries the starter names resolved to canonical keys —
    # use those so the confidence "known starter" check and the returned
    # display name agree with what actually drove the factors.
    home_starter = feats["home_starter"] or home_starter
    away_starter = feats["away_starter"] or away_starter

    conf_score, conf_label = confidence_rating(
        blended_home, elo_p_home, poi_p_home, notes,
        home_starter, away_starter, pitchers,
    )

    return {
        "home": home, "away": away,
        "home_starter": home_starter or "", "away_starter": away_starter or "",
        "elo_home_rating": round(feats["elo_home_rating"], 1), "elo_away_rating": round(feats["elo_away_rating"], 1),
        "elo_home_win_pct": round(elo_p_home * 100, 1),
        "poisson_home_win_pct": round(poi_p_home * 100, 1),
        "blended_home_win_pct": round(blended_home * 100, 1),
        "blended_away_win_pct": round((1 - blended_home) * 100, 1),
        "predicted_home_runs": round(lam_home, 2),
        "predicted_away_runs": round(lam_away, 2),
        "context": notes,
        "confidence_score": conf_score,
        "confidence_label": conf_label,
    }


CONFIDENCE_MIN_STARTER_IP = 10.0  # below this, a "known" starter still counts as low-information

CONFIDENCE_BANDS = (
    (70, "高信心"),
    (55, "中高"),
    (40, "中"),
    (25, "偏低"),
)  # below the lowest threshold falls through to "不建議"


def confidence_rating(
    blended_home: float, elo_p_home: float, poi_p_home: float, notes: dict,
    home_starter: str | None, away_starter: str | None, pitchers: dict | None,
) -> tuple[float, str]:
    """Composite confidence score (0-100), NOT a re-statement of win
    probability: a near-50/50 game caps out low here regardless of how
    much context data is available, while two methods agreeing closely
    and a fuller picture (known starters with a real innings sample,
    resolved handedness splits) both raise it independently of the edge."""
    edge = abs(blended_home - 0.5) * 2.0

    agreement = max(0.0, 1.0 - abs(elo_p_home - poi_p_home) * 2.0)

    def _starter_known(starter, factor):
        if factor is None or not starter or not pitchers:
            return False
        ip = (pitchers.get(starter) or {}).get("season", {}).get("ip", 0)
        return ip >= CONFIDENCE_MIN_STARTER_IP

    points = 0
    points += _starter_known(home_starter, notes.get("home_pitcher_factor"))
    points += _starter_known(away_starter, notes.get("away_pitcher_factor"))
    points += notes.get("home_hand_factor") is not None
    points += notes.get("away_hand_factor") is not None
    completeness = points / 4.0

    score = 100 * (0.5 * edge + 0.3 * agreement + 0.2 * completeness)
    score = round(min(100.0, max(0.0, score)), 1)

    label = "不建議"
    for threshold, name in CONFIDENCE_BANDS:
        if score >= threshold:
            label = name
            break

    return score, label
