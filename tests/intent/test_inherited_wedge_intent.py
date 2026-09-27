"""Runs tests/intent/inherited_wedge.json (Zenkai Rules Z1): a hang case that
follows an already-wedged darktable must be classified `inherited_wedge`,
never `hang`; a hang darktable goes unresponsive DURING this case's own step
is still a genuine `hang`. Two oracles per example (Z2): `run_tool_case`'s own
`obs.blocked` string, and the shared `gauntlet.evaluate.is_hang` verdict every
report/progress counter reads (night1-robustness-triage.md item 2)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gauntlet.case import Case, Expect, Step
from gauntlet.evaluate import is_hang
from tests.gauntlet import execute
from tests.gauntlet.corpus import Corpus
from tests.gauntlet.test_session import FakeDt, FakeMcp, _session

EXAMPLES = json.loads((Path(__file__).parent / "inherited_wedge.json").read_text())["examples"]


def _corpus(trial: Path) -> Corpus:
    trial.mkdir()
    (trial / "H.ORF").write_bytes(b"raw")
    doc = {"frames": [{"rel": "H.ORF", "body": "olympus"}],
           "roles": {"olympus": {"hero": "H.ORF", "dark": "H.ORF"}}}
    return Corpus(doc, trial)


class InheritedWedgeIntentTest(unittest.TestCase):
    def run_example(self, ex: dict) -> None:
        inp, want = ex["input"], ex["expect"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            corpus = _corpus(root / "trial")
            mcp = FakeMcp({"darkroom_say": TimeoutError("no answer")}) if inp["times_out"] \
                else FakeMcp()
            s = _session(corpus, root, FakeDt(health=inp["health"]), mcp).open()
            case = Case(id=ex["id"], layer="scenarios", tier="P0", mode="tool",
                        steps=[Step("darkroom_say", {"phrase": "brighter"}, 8.0, True)],
                        expect=Expect("edit"), body="olympus", frame_role="hero")
            obs = execute.run_tool_case(s, case)
        self.assertEqual(obs.blocked, want["blocked"], ex["id"])
        self.assertIs(is_hang("blocked", obs.blocked), want["is_hang"], ex["id"])


def _make(ex: dict):
    def test(self: InheritedWedgeIntentTest) -> None:
        self.run_example(ex)
    return test


for _ex in EXAMPLES:
    setattr(InheritedWedgeIntentTest, "test_" + _ex["id"].replace("-", "_"), _make(_ex))


if __name__ == "__main__":
    unittest.main()
