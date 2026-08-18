# -*- coding: utf-8 -*-
"""Strip the standalone-page wrapper (<!doctype>/<html>/<head> open tags,
<meta>, <title>, and the closing </body></html>) from site/index.html,
producing site/artifact.html: just the <style>...<script> content that
Claude Artifacts expects (it supplies its own <head>/<body> skeleton).

Run this after generate_site.py whenever you want to republish the
Artifact view of the site.
"""
import _console  # noqa: F401
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE_DIR = ROOT / "site"


def main():
    src = (SITE_DIR / "index.html").read_text(encoding="utf-8")

    start = src.index("<style>")
    end = src.rindex("</script>") + len("</script>")
    fragment = src[start:end]

    # the slice still contains the standalone page's own </head><body> —
    # Artifacts supply their own; drop that stray transition markup.
    fragment = re.sub(r"</head>\s*<body>\s*", "\n", fragment, count=1)

    out_path = SITE_DIR / "artifact.html"
    out_path.write_text(fragment, encoding="utf-8")
    print("wrote", out_path, f"({len(fragment)} chars)")


if __name__ == "__main__":
    main()
