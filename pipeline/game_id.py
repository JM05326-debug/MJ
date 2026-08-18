# -*- coding: utf-8 -*-
"""Stable, source-of-truth game identifiers.

A game_id must never change across re-fetches (it's the append-only key
used to guarantee a locked prediction is never overwritten), and must be
unique even for same-day doubleheaders between the same two teams — plain
`date+home+away` is NOT safe for that reason.
"""
from __future__ import annotations


def game_id(league: str, game: dict) -> str:
    """league: 'cpbl' or 'npb'. game: a row from data/<league>_data.json
    (history or upcoming).

    CPBL rows always carry `native_id` (Year-KindCode-GameSno) — assigned at
    schedule-creation time, so it's present identically whether the game is
    still upcoming or already in history. Safe to use as the ID.

    NPB rows carry `slug` (date/teamA-teamB-N) too, but — confirmed by
    testing — the site only assigns it once a box-score page exists for the
    game, i.e. NOT for games more than ~a day out. A game locked while
    upcoming (slug=None) must compute the SAME id once it reappears in
    history (slug populated) or the later results-join in collect_results.py
    would silently never match it. So NPB deliberately ignores `slug` for
    identity purposes and always keys off date+home+away instead, which is
    stable across both states. This only risks a collision on a same-day
    doubleheader between the same two teams (rare); the practical effect of
    that collision is just "the second game of the doubleheader doesn't get
    predicted," not a crash or a mismatched join.
    """
    if league == "cpbl":
        native = game.get("native_id")
        if native:
            return f"{league}:{native}"
    return f"{league}:{game['date']}:{game['h']}:{game['v']}"
