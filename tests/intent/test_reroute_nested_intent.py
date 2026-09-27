"""Runs tests/intent/reroute_nested.json (Zenkai Rules Z1). Two observations
per example (Z2): the flag's own value read straight from the ContextVar, and
(for the third example) the full `moves.adjust` result, which no path other
than a genuinely completed, correctly-reset scope can produce."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from scouter.core import darkroom, moves
from scouter.errors import RerouteNested
from tests.test_moves import _MovesFixture

EXAMPLES = json.loads((Path(__file__).parent / "reroute_nested.json").read_text())["examples"]


class RerouteNestedIntentTest(_MovesFixture):
    def run_example(self, ex: dict) -> None:
        inp, want = ex["input"], ex["expect"]
        outer_token = None
        if inp.get("already_active"):
            outer_token = darkroom.NO_WB_REROUTE.set(True)
        try:
            if ex["id"] == "refused-nested-attempt-does-not-affect-the-outer-move":
                real_adjust_core = moves._adjust_core
                attempted = []

                def nesting(module, param, delta, *, resolved, instance, commit, level,
                            job, ms_precommit=0):
                    if not attempted:
                        attempted.append(True)
                        with self.assertRaises(RerouteNested):
                            with darkroom.no_wb_reroute_scope():
                                pass
                    return real_adjust_core(module, param, delta, resolved=resolved,
                                             instance=instance, commit=commit, level=level,
                                             job=job, ms_precommit=ms_precommit)

                # This example runs its own (non-nested) outer scope, so drop
                # the `already_active` one set above and use a real one via
                # moves.adjust itself.
                darkroom.NO_WB_REROUTE.reset(outer_token)
                outer_token = None
                with mock.patch("scouter.core.moves._adjust_core", side_effect=nesting):
                    result = moves.adjust.__wrapped__("exposure", "exposure", 0.2,
                                                       commit=False, job="job-test")
                self.assertEqual(attempted, [True], ex["id"])
                self.assertEqual(result.outcome, want["outcome"], ex["id"])
                self.assertEqual(darkroom.NO_WB_REROUTE.get(), want["flag_after"], ex["id"])
                return

            error = None
            if inp.get("already_active"):
                try:
                    with darkroom.no_wb_reroute_scope():
                        pass
                except RerouteNested as e:
                    error = e
            else:
                with darkroom.no_wb_reroute_scope():
                    self.assertEqual(darkroom.NO_WB_REROUTE.get(), want["flag_during"], ex["id"])
                self.assertEqual(darkroom.NO_WB_REROUTE.get(), want["flag_after"], ex["id"])
                return

            if want["outcome"] == "refused":
                self.assertIsNotNone(error, ex["id"])
                self.assertEqual(error.code, want["code"], ex["id"])
                self.assertEqual(darkroom.NO_WB_REROUTE.get(), want["flag_after"], ex["id"])
            else:
                self.assertIsNone(error, ex["id"])
        finally:
            if outer_token is not None:
                darkroom.NO_WB_REROUTE.reset(outer_token)


def _make(ex: dict):
    def test(self: RerouteNestedIntentTest) -> None:
        self.run_example(ex)
    return test


for _ex in EXAMPLES:
    setattr(RerouteNestedIntentTest, "test_" + _ex["id"].replace("-", "_"), _make(_ex))


if __name__ == "__main__":
    unittest.main()
