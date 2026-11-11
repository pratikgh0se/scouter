"""Round 1 gauntlet product fixes: C14 (export all-fail), C13 (undo of a
module's first-ever set restores a verified default)."""

import os
import unittest

from scouter import registry
from scouter.core import darkroom, dtbridge, export, journal, vocab
from scouter.errors import ExportAllFailed, ScouterError
from tests.scouter_fakes import FAKE_CLI_WRITES_PPM, FakeBridge, TempCache, fake_cli_script
from tests.test_darkroom_core import _DarkroomFixture

#: Fails (exit 1) for any input whose path contains "bad", else renders.
_CLI_FAILS_BAD = (
    'for a in "$@"; do case "$a" in *bad*) echo "disk full" >&2; exit 1;; esac; done\n'
    + FAKE_CLI_WRITES_PPM
)
_CLI_FAILS_ALL = 'echo "no space left on device" >&2\nexit 1\n'


class ExportAllFailTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = TempCache()
        self.root = self.cache.start()
        self.good = self.root / "good.ORF"
        self.bad = self.root / "bad.ORF"
        self.bad2 = self.root / "bad2.ORF"
        for p in (self.good, self.bad, self.bad2):
            p.write_bytes(b"raw")
        dtbridge.set_bridge(
            FakeBridge(
                {
                    "image.lookup": lambda p: {"found": True, "image_id": 12, "path": p["path"]},
                }
            )
        )
        self._saved = os.environ.get("SCOUTER_DARKTABLE_CLI")
        self.out = self.root / "exports"

    def tearDown(self) -> None:
        dtbridge.set_bridge(None)
        if self._saved is None:
            os.environ.pop("SCOUTER_DARKTABLE_CLI", None)
        else:
            os.environ["SCOUTER_DARKTABLE_CLI"] = self._saved
        self.cache.stop()

    def _cli(self, body: str) -> None:
        os.environ["SCOUTER_DARKTABLE_CLI"] = str(fake_cli_script(self.root, body))

    def test_every_render_failing_is_an_error(self) -> None:
        self._cli(_CLI_FAILS_ALL)
        with self.assertRaises(ExportAllFailed) as ctx:
            export.run(images=[str(self.bad)], preset="preview", out_dir=str(self.out))
        self.assertEqual(len(ctx.exception.detail["files"]), 1)
        # second oracle: nothing landed on disk
        self.assertEqual(list(self.out.glob("*")) if self.out.exists() else [], [])

    def test_all_fail_surfaces_as_envelope_not_ok(self) -> None:
        self._cli(_CLI_FAILS_ALL)
        env = registry.call(
            "export.run", "cli", images=[str(self.bad)], preset="preview", out_dir=str(self.out)
        )
        self.assertFalse(env.ok)
        self.assertEqual(
            env.error["code"] if isinstance(env.error, dict) else env.error.code,
            "export_all_failed",
        )

    def test_partial_success_still_ok_with_counts(self) -> None:
        self._cli(_CLI_FAILS_BAD)
        result = export.run(
            images=[str(self.good), str(self.bad)],
            preset="preview",
            out_dir=str(self.out),
            confirm=True,
        )
        self.assertEqual((result.exported, result.failed), (1, 1))
        self.assertEqual(len(list(self.out.glob("*"))), 1)

    def test_zero_inputs_unchanged(self) -> None:
        self._cli(_CLI_FAILS_ALL)
        try:
            result = export.run(images=[], preset="preview", out_dir=str(self.out))
        except ExportAllFailed:  # pragma: no cover
            self.fail("zero inputs must not be an all-failed error")
        except ScouterError:
            return  # an input-resolution refusal is the existing behaviour
        self.assertEqual(result.exported, 0)


class UndoFirstSetTest(_DarkroomFixture):
    def _first_set(self, module: str, param: str, enabled=None) -> None:
        entry = journal.begin(
            "job-test",
            verb="darkroom.set",
            image_id=12,
            path=str(self.raw),
            module=module,
            param=param,
            instance=0,
            before={"source": "xmp", "value": None, "enabled": enabled, "history_end": 11},
        )
        journal.complete("job-test", entry.seq, after={"value": 0.5})

    def test_verified_default_is_restored(self) -> None:
        spec = vocab.spec("colorzones", "strength")
        self.assertEqual(spec.default, 0.0)
        self._first_set("colorzones", "strength")
        result = darkroom.undo_last()
        last = [p for m, p in self.bridge.calls if m == "darkroom.action"][-1]
        self.assertEqual(last["effect"], "set")
        self.assertEqual(last["value"], spec.default)
        self.assertTrue(any("disabled it again" in w for w in result.warnings))
        disables = [p for m, p in self.bridge.calls if m == "module.disable"]
        self.assertEqual([d["module"] for d in disables], ["colorzones"])
        self.assertIsNone(journal.latest_open("job-test"))

    def test_no_verified_default_keeps_the_refusal(self) -> None:
        self.assertIsNone(vocab.spec("filmicrgb", "contrast").default)
        self._first_set("filmicrgb", "contrast")
        with self.assertRaises(ScouterError) as ctx:
            darkroom.undo_last()
        self.assertIn("never readable", str(ctx.exception))
        self.assertFalse([1 for m, _ in self.bridge.calls if m == "darkroom.action"])

    def test_module_that_existed_is_never_given_an_invented_before(self) -> None:
        # enabled=True: the module HAD an entry whose value was unreadable.
        self._first_set("colorzones", "strength", enabled=True)
        with self.assertRaises(ScouterError):
            darkroom.undo_last()

    def test_unconfirmed_disable_is_an_error_not_ok(self) -> None:
        # The fixture sidecar keeps exposure enabled, so the disable cannot
        # be confirmed from it: must raise, and the entry stays open.
        self._first_set("exposure", "black")
        with self.assertRaises(ScouterError) as ctx:
            darkroom.undo_last()
        self.assertIn("disabled again", str(ctx.exception))
        entry = journal.latest_open("job-test")
        assert entry is not None
        self.assertFalse(entry.undone)


class SigmoidContrastDefaultTest(_DarkroomFixture):
    """C3: host-verified default (docs/measurements.md, 2026-11-09 probe)."""

    def test_sigmoid_contrast_declares_the_verified_default(self) -> None:
        spec = vocab.spec("sigmoid", "contrast")
        self.assertEqual(spec.default, 1.5)
        self.assertIsNone(spec.default_candidate)
        self.assertEqual(darkroom._declared_default("sigmoid", "contrast"), 1.5)

    def test_first_set_undo_restores_default_and_disables(self) -> None:
        entry = journal.begin(
            "job-test",
            verb="darkroom.set",
            image_id=12,
            path=str(self.raw),
            module="sigmoid",
            param="contrast",
            instance=0,
            before={"source": "xmp", "value": None, "enabled": None, "history_end": 11},
        )
        journal.complete("job-test", entry.seq, after={"value": 1.9})
        # The fixture sidecar keeps sigmoid enabled, so the disable cannot be
        # confirmed there: the undo errors, after restoring the default.
        with self.assertRaises(ScouterError):
            darkroom.undo_last()
        sets = [p for m, p in self.bridge.calls if m == "darkroom.action"]
        self.assertEqual(sets[-1]["value"], 1.5)
        self.assertIn(
            "sigmoid", [p["module"] for m, p in self.bridge.calls if m == "module.disable"]
        )
