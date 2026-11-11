"""docs/ZENKAI.md is a verbatim mirror of the vault's Zenkai Rules (Capsule Corp rows 454, 455):
its header's body_sha256 must match the body below it, so an edit to the mirror alone fails here."""

import hashlib
import re
from pathlib import Path

MIRROR = Path(__file__).resolve().parents[1] / "docs" / "ZENKAI.md"
HEADER = re.compile(
    r"^Mirror of vault Knowledge/Capsule Corp - Zenkai Rules\.md, body_sha256 ([0-9a-f]{64})$"
)


def test_zenkai_mirror_body_matches_header_sha():
    header, blank, body = MIRROR.read_text(encoding="utf-8").split("\n", 2)
    m = HEADER.match(header)
    assert m, f"bad header line: {header!r}"
    assert blank == ""
    assert hashlib.sha256(body.encode("utf-8")).hexdigest() == m.group(1)
    assert body.rstrip().endswith("Zenkai Rules v2 (2026-10-20)")
