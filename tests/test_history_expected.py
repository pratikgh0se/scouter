"""Row 434: a no_bounce rewrite counts as the commit only when the sidecar
history advanced as the write expects (history_end not back, newest item is
the moved module, and it is new). Intent examples: tests/intent/history_commit_expected.json
(night1-20260926 recorded cases). The Lua twin is tested in tests/lua/test_dispatcher.lua."""

import json
import shutil
import time
import unittest
from pathlib import Path

from scouter.core import dtbridge, history
from scouter.errors import CommitTimeout
from tests.scouter_fakes import FakeBridge, TempCache

INTENT = Path(__file__).resolve().parent / "intent" / "history_commit_expected.json"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "P6281348.ORF.xmp"


def xmp(history_end: int, newest_op: str, newest_params: str, stamp: str = "1") -> str:
    items = []
    for n in range(history_end):
        op, params = (newest_op, newest_params) if n == history_end - 1 else (f"op{n}", f"p{n}")
        items.append(
            f'     <rdf:li\n      darktable:num="{n}"\n      darktable:operation="{op}"\n'
            f'      darktable:params="{params}"/>\n'
        )
    return (
        '<?xml version="1.0"?>\n<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
        ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
        '  <rdf:Description xmlns:darktable="http://darktable.sf.net/"\n'
        f'   darktable:change_timestamp="{stamp}"\n'
        f'   darktable:history_end="{history_end}"\n'
        f'   darktable:history_current_hash="h{history_end}{newest_params}">\n'
        "   <darktable:history>\n    <rdf:Seq>\n" + "".join(items)
        + "    </rdf:Seq>\n   </darktable:history>\n  </rdf:Description>\n"
        " </rdf:RDF>\n</x:xmpmeta>\n"
    )


class IntentExamplesTest(unittest.TestCase):
    def test_every_recorded_case_gets_the_answer_a_human_calls_right(self) -> None:
        examples = json.loads(INTENT.read_text())["examples"]
        self.assertGreaterEqual(len(examples), 5)
        for ex in examples:
            b, a = ex["before"], ex["after"]
            ok, why = history._history_advanced_as_expected(
                xmp(b["history_end"], *b["newest"]),
                xmp(a["history_end"], *a["newest"]),
                ex["expect_module"],
            )
            self.assertEqual(ok, ex["right"], f"{ex['case']}: {why}")
            if not ex["right"]:
                self.assertTrue(why, ex["case"])

    def test_same_end_with_new_params_merged_into_the_top_item_is_the_commit(self) -> None:
        ok, _ = history._history_advanced_as_expected(
            xmp(112, "exposure", "a"), xmp(112, "exposure", "b"), "exposure"
        )
        self.assertTrue(ok)

    def test_same_end_and_same_newest_item_is_not(self) -> None:
        before = xmp(112, "exposure", "a")
        after = before.replace('params="p3"', 'params="pX"')
        ok, why = history._history_advanced_as_expected(before, after, "exposure")
        self.assertFalse(ok)
        self.assertIn("unchanged", str(why))


class CommitWithExpectModuleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = TempCache()
        root = self.cache.start()
        self.raw = root / "P6281348.ORF"
        self.raw.write_bytes(b"raw")
        self.sidecar = Path(str(self.raw) + ".xmp")
        shutil.copy(FIXTURE, self.sidecar)
        self.sidecar.write_text(xmp(112, "exposure", "black_m0.0002", "s"))
        self.sent: list[dict] = []
        self.base = {
            "image.lookup": {"found": True, "image_id": 12, "path": str(self.raw)},
        }

    def tearDown(self) -> None:
        dtbridge.set_bridge(None)
        self.cache.stop()

    def _bridge(self, rewrite: str) -> None:
        def commit(params: dict) -> dict:
            self.sent.append(params)
            time.sleep(0.01)
            self.sidecar.write_text(rewrite)
            return {"strategy_used": "no_bounce", "ok": True, "view_after": "darkroom",
                    "cpu_ms": 10, "sidecar_written": True}

        dtbridge.set_bridge(FakeBridge({**self.base, "history.commit": commit}))

    def test_a_stale_history_with_a_new_hash_is_not_committed(self) -> None:
        # cov-phrase-faded_blacks-olympus-touch: 112 -> 92, reported ok.
        self._bridge(xmp(92, "exposure", "black_0.0098", "db"))
        with self.assertRaises(CommitTimeout) as ctx:
            history.commit_expecting(image=str(self.raw), timeout_s=0.3, expect_change=True,
                                     expect_module="exposure")
        self.assertIn("went back from 112 to 92",
                      str(ctx.exception.detail.get("sidecar_rewritten_not_this_edit")))
        self.assertEqual(self.sent[0].get("expect_module"), "exposure")

    def test_the_expected_newest_item_is_committed(self) -> None:
        self._bridge(xmp(113, "exposure", "black_m0.0027", "db"))
        result = history.commit_expecting(image=str(self.raw), timeout_s=2.0, expect_change=True,
                                          expect_module="exposure")
        self.assertTrue(result.committed)

    def test_without_expect_module_a_history_end_that_went_back_is_still_refused(self) -> None:
        # Row 461 rule 1: the public verb, moves._precommit and the preset
        # flushes pass no module; a regressed history is refused for them too.
        self._bridge(xmp(92, "exposure", "black_0.0098", "db"))
        with self.assertRaises(CommitTimeout) as ctx:
            history.commit(image=str(self.raw), timeout_s=0.3, expect_change=True)
        self.assertIn("went back from 112 to 92", str(ctx.exception.detail.get("stale_rewrite")))
        self.assertNotIn("expect_module", self.sent[0])

    def test_without_expect_module_a_forward_change_still_commits(self) -> None:
        self._bridge(xmp(113, "sigmoid", "c1.7", "db"))
        result = history.commit(image=str(self.raw), timeout_s=2.0, expect_change=True)
        self.assertTrue(result.committed)

    def test_the_precommit_path_refuses_a_stale_rewrite(self) -> None:
        # moves.py:318 / presets.py:135: expect_change=False, one attempt, no module.
        from scouter.core import darkroom

        self._bridge(xmp(92, "exposure", "black_0.0098", "db"))
        with self.assertRaises(CommitTimeout):
            darkroom._commit("j", 12, strategy="auto", attempts=1, expect_change=False)


class StaleRewriteReportedByThePluginTest(unittest.TestCase):
    """Row 461: the plugin flags `stale_rewrite`; Python refuses it at once and
    restores the pre-commit sidecar when the plugin could not."""

    def setUp(self) -> None:
        self.cache = TempCache()
        root = self.cache.start()
        self.raw = root / "P6281348.ORF"
        self.raw.write_bytes(b"raw")
        self.sidecar = Path(str(self.raw) + ".xmp")
        self.pre = xmp(112, "exposure", "black_m0.0002", "s")
        self.sidecar.write_text(self.pre)

    def tearDown(self) -> None:
        dtbridge.set_bridge(None)
        self.cache.stop()

    def _bridge(self, *, restored: bool) -> None:
        def commit(params: dict) -> dict:
            self.sidecar.write_text(xmp(92, "exposure", "black_0.0098", "db"))
            if restored:
                self.sidecar.write_text(self.pre)
            return {"strategy_used": "no_bounce", "ok": False, "view_after": "darkroom",
                    "sidecar_written": False, "stale_rewrite": True,
                    "stale_rewrite_restored": restored or None,
                    "detail": "stale_rewrite: history_end went back from 112 to 92"}

        dtbridge.set_bridge(FakeBridge({
            "image.lookup": {"found": True, "image_id": 12, "path": str(self.raw)},
            "history.commit": commit,
        }))

    def test_python_restores_when_the_plugin_could_not(self) -> None:
        self._bridge(restored=False)
        t0 = time.monotonic()
        with self.assertRaises(CommitTimeout) as ctx:
            history.commit_expecting(image=str(self.raw), timeout_s=3.0, expect_change=False,
                                     strategy="no_bounce")
        self.assertLess(time.monotonic() - t0, 1.0, "refused at once, no poll to the deadline")
        self.assertTrue(ctx.exception.detail["stale_rewrite_restored"])
        self.assertEqual(ctx.exception.detail["sidecar_end_now"], 112)
        self.assertEqual(self.sidecar.read_text(), self.pre)
        self.assertIn("stale_rewrite", str(ctx.exception))

    def test_plugin_restore_is_reported_and_never_a_noop(self) -> None:
        self._bridge(restored=True)
        with self.assertRaises(CommitTimeout) as ctx:
            history.commit_expecting(image=str(self.raw), timeout_s=3.0, expect_change=False)
        self.assertTrue(ctx.exception.detail["stale_rewrite_restored"])
        self.assertEqual(self.sidecar.read_text(), self.pre)


if __name__ == "__main__":
    unittest.main()
