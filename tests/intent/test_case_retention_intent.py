# mutation-check: targets-post-date-range -- RunDir.retain is new in this
# range (card t_90e75c20); no marker needed.
"""Runs tests/intent/case_retention.json (Zenkai Rules Z1). Two oracles per
example (Z2): whether any file remains under the case's workdir afterward,
and the exact byte count freed (gauntlet/store.py:RunDir.retain)."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from gauntlet.store import RunDir

EXAMPLES = json.loads((Path(__file__).parent / "case_retention.json").read_text())["examples"]


class CaseRetentionIntentTest(unittest.TestCase):
    def run_example(self, ex: dict, tmp_path: Path) -> None:
        inp, want = ex["input"], ex["expect"]
        run = RunDir(tmp_path, "s").ensure()
        d = run.case_workdir("c1")
        if inp["files"]:
            d.mkdir(parents=True)
            for name, size in inp["files"].items():
                (d / name).write_bytes(b"x" * size)
        freed = run.retain("c1", keep=tuple(inp["keep"]))
        # Oracle 1: whether any file remains under the workdir afterward.
        remaining = d.exists() and any(p.is_file() for p in d.rglob("*"))
        self.assertEqual(remaining, want["kept_exists"], ex["id"])
        # Oracle 2: the exact byte count freed.
        self.assertEqual(freed, want["freed_bytes"], ex["id"])


def _make(ex: dict):
    def test(self: CaseRetentionIntentTest) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            self.run_example(ex, Path(d))
    return test


for _ex in EXAMPLES:
    setattr(CaseRetentionIntentTest, "test_" + _ex["id"].replace("-", "_"), _make(_ex))
