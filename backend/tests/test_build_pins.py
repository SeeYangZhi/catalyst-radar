"""Build-time pins that must stay in lockstep.

The Dockerfile installs Playwright's Chromium in an early cached layer
using a literal version pin (so dependency changes don't re-download the
browser stack). The venv's playwright package — resolved from uv.lock —
must be the same version, or it will look for a different browser
revision at runtime and re-download (or crash) inside the container.
"""

import re
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1]


def test_dockerfile_playwright_pin_matches_uv_lock():
    dockerfile = (_BACKEND / "Dockerfile").read_text()
    pin = re.search(r"playwright==(\d+\.\d+\.\d+)", dockerfile)
    assert pin, "Dockerfile no longer pins playwright — update this test's rationale"

    lock = (_BACKEND / "uv.lock").read_text()
    locked = re.search(r'name = "playwright"\nversion = "(\d+\.\d+\.\d+)"', lock)
    assert locked, "playwright missing from uv.lock"

    assert pin.group(1) == locked.group(1), (
        f"Dockerfile pins playwright=={pin.group(1)} but uv.lock resolves "
        f"{locked.group(1)} — bump them together"
    )
