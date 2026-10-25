"""deploy/tunnel/: plain-text checks of the tunnel image, compose file and scripts (no YAML package)."""

import re
from pathlib import Path

TUNNEL = Path(__file__).resolve().parents[1] / "deploy" / "tunnel"
SHA_FILE = TUNNEL / "maverick-mcp.sha"


def _maverick_sha():
    return SHA_FILE.read_text()


def test_maverick_sha_file_is_one_full_commit():
    text = _maverick_sha()
    assert re.fullmatch(r"[0-9a-f]{40}\n", text), "one line: the 40-character maverick-mcp commit"
