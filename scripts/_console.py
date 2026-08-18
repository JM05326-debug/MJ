# -*- coding: utf-8 -*-
"""Force UTF-8 stdout/stderr so printing Chinese/Japanese team & player
names never crashes on a Windows console using a legacy codepage (cp950
Big5 cannot encode several Japanese kanji forms like 広, 稲, 鴎 etc)."""
import sys

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")
