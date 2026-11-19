"""Console entrypoint for the AdForge dashboard.

`pyproject.toml` exposes ``adforge-dashboard`` as a script pointing here, and
the Docker image invokes Streamlit directly. Both paths must land on the same
page, so this module forwards to ``Overview.py`` — Streamlit's multipage
navigation derives sidebar labels from the entrypoint filename, which is why
``Overview.py`` is the canonical file and this is only a shim.
"""

from __future__ import annotations

import sys
from pathlib import Path

DASHBOARD_DIR = Path(__file__).resolve().parent


def run() -> None:
    """Run the dashboard via Streamlit's CLI runner.

    Uses ``streamlit.web.bootstrap`` rather than shelling out so ``argv`` and
    the server flags are set explicitly — the console script has no arguments
    of its own, and Streamlit would otherwise try to parse ours.
    """
    from streamlit.web import cli as stcli

    entrypoint = DASHBOARD_DIR / "Overview.py"
    sys.argv = [
        "streamlit",
        "run",
        str(entrypoint),
        "--server.port=8501",
    ]
    sys.exit(stcli.main())


if __name__ == "__main__":
    run()
