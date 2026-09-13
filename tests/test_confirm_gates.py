"""Exit criterion E7: no gated verb reaches darktable without confirm."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from scouter import config, registry
from scouter.core import dtbridge, library
from tests.scouter_fakes import config_fixture

FIXTURE = Path(__file__).parent / "fixtures" / "exif-orf-header.bin"

# `registry.load_verbs()` tracks whether a full load has been attempted (not
# whether `VERB_REGISTRY` happens to be non-empty), so it always walks every
# core module regardless of what some other test module imported directly
# before this one ran. No per-file import workaround needed.


class RefusingBridge:
    """Any call is a test failure: a gated dry run must not touch darktable."""

    WRITE_METHODS = {
        "darkroom.action",
        "module.enable",
        "module.disable",
        "module.reset",
        "history.commit",
        "style.create_from_current",
        "style.apply",
        "snapshot.take",
    }

    def __init__(self, test: unittest.TestCase, raw: str) -> None:
        self.test = test
        self.raw = raw
        self.calls: list[str] = []

    def call(self, method: str, params: dict, timeout: float | None = None):
        self.calls.append(method)
        self.test.assertNotIn(
            method,
            self.WRITE_METHODS,
            f"a gated verb called the writing bridge method {method} without confirm",
        )
        if method == "view.current":
            return {"view": "darkroom"}
        if method == "image.lookup":
            return {"found": True, "image_id": 1, "path": self.raw}
        if method == "darkroom.open":
            return {
                "image_id": 1,
                "path": self.raw,
                "view": "darkroom",
                "opened": False,
                "already_open": True,
            }
        if method == "list_styles":
            # style.extract is gated only when a style of the requested name
            # already exists (design 10.3); GATED_ARGS below asks for "hero",
            # so the fake collides with it to actually exercise the gate.
            return {"styles": [{"name": "hero"}], "count": 1}
        raise AssertionError(f"unexpected read method {method}")


class ConfirmGateTest(unittest.TestCase):
    def setUp(self) -> None:
        from tests.scouter_fakes import TempCache

        self.cache = TempCache()
        root = self.cache.start()
        self.raw = str(root / "hero.ORF")
        (root / "hero.ORF").write_bytes(b"raw")
        # Never /tmp: the gate test writes nothing, but the PATH it plans must
        # still live under the throwaway cache so a regression cannot leave a
        # directory behind in a shared place.
        self.out_dir = str(root / "gate-out")
        self.bridge = RefusingBridge(self, self.raw)
        dtbridge.set_bridge(self.bridge)

        # `library.import`/`library.register` need a passing backup gate and
        # a real job layout to reach their own dry-run branch at all -- unlike
        # every other gated verb here, `library.import` refuses BEFORE the
        # dry-run check if the backup gate fails (design section 4.3), so this
        # harness's usual "just substitute a raw path" trick isn't enough.
        self.cfg = config_fixture(root)
        self._lib_cfg_patch = mock.patch.object(library.config, "load", return_value=self.cfg)
        self._lib_cfg_patch.start()
        self.card_dir = root / "gate-card"
        self.card_dir.mkdir()
        (self.card_dir / "P0001.ORF").write_bytes(FIXTURE.read_bytes())
        self.job_slug = "gate-hero"
        job_path = config.job_dir(self.cfg, "2026-06-28", self.job_slug)
        (job_path / "raw").mkdir(parents=True)

    def tearDown(self) -> None:
        self._lib_cfg_patch.stop()
        dtbridge.set_bridge(None)
        self.cache.stop()

    #: Minimal args per gated verb, WITHOUT confirm.
    GATED_ARGS = {
        "config.init": {"path": None},
        "darkroom.reset": {"module": "exposure"},
        "style.extract": {"image": None, "name": "hero", "modules": []},
        "export.run": {"images": [None, None], "preset": "preview", "out_dir": None},
        "library.import": {"source": None, "job_slug": "gate-hero"},
        "library.register": {"job_slug": "gate-hero"},
    }
    # `style.apply` is deliberately NOT here (final review, should-fix 7): its
    # only would-be gate was mode=overwrite, which darktable cannot do at all,
    # so it is refused with `unsupported_mode` instead of being offered as a
    # confirmable action. `tests/test_style_export.py` asserts that refusal.

    def test_the_gated_set_is_exactly_what_the_design_lists(self) -> None:
        gated = {vid for vid, verb in registry.load_verbs().items() if verb.gated}
        self.assertEqual(gated, set(self.GATED_ARGS))

    def test_every_gated_verb_dry_runs_without_confirm(self) -> None:
        for verb_id, args in self.GATED_ARGS.items():
            with self.subTest(verb=verb_id):
                filled = {
                    k: (
                        self.out_dir
                        if k == "out_dir"
                        else (
                            str(self.card_dir)
                            if k == "source"
                            else (
                                self.raw
                                if v is None
                                else (
                                    [self.raw] * len(v)
                                    if isinstance(v, list) and v and v[0] is None
                                    else v
                                )
                            )
                        )
                    )
                    for k, v in args.items()
                }
                envelope = registry.call(verb_id, "core", **filled)
                self.assertEqual(
                    envelope.outcome,
                    "dry_run",
                    f"{verb_id} without confirm must be a dry run, got {envelope.outcome}",
                )
                self.assertTrue(envelope.data["plan"], f"{verb_id} dry run has no plan")
                self.assertTrue(envelope.data["reason"], f"{verb_id} dry run has no reason")

    #: `history.commit` is the one writer spec 10.1 gives no `dry_run` param:
    #: it flushes darktable's own already-decided history to the XMP and
    #: verifies the write landed -- there is no separate "plan" to preview,
    #: unlike every other writer here, which chooses what to write.
    NO_DRY_RUN_WRITERS = {"history.commit"}

    def test_every_writer_accepts_dry_run(self) -> None:
        for verb_id, verb in registry.load_verbs().items():
            if verb.writes and verb_id not in self.NO_DRY_RUN_WRITERS:
                names = {a.name for a in verb.args}
                self.assertIn("dry_run", names, f"{verb_id} writes but has no dry_run")

    def test_only_writers_are_gated(self) -> None:
        for verb_id, verb in registry.load_verbs().items():
            if verb.gated:
                self.assertTrue(verb.writes, f"{verb_id} is gated but does not write")
                names = {a.name for a in verb.args}
                self.assertIn("confirm", names, f"{verb_id} is gated but has no confirm")
