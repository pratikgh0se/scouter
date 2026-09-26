"""Exit criterion E7: no gated verb reaches darktable without confirm."""

from __future__ import annotations

import json
import os
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
        self._ids: dict[str, int] = {}

    def _id_for(self, path: str) -> int:
        """A stable, distinct id per path, handed out in first-seen order."""
        if path not in self._ids:
            self._ids[path] = len(self._ids) + 1
        return self._ids[path]

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
            # ECHOES what was asked for, rather than answering `self.raw` for
            # every input: two distinct paths must resolve to two distinct
            # results, or a fake this permissive hides real refusals (task
            # 14 found `export.run`'s new collision guard firing here purely
            # because the fake collapsed both inputs onto one path).
            def one(params: dict) -> dict:
                if params.get("path"):
                    path = str(params["path"])
                    return {"found": True, "image_id": self._id_for(path), "path": path}
                iid = int(params.get("image_id") or 1)
                return {"found": True, "image_id": iid, "path": self.raw}

            if params.get("items") is not None:
                return {"results": [one(item) for item in params["items"]]}
            return one(params)
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
        from tests.scouter_fakes import TempCache, write_minimal_style_sidecar

        self.cache = TempCache()
        root = self.cache.start()
        self.raw = str(root / "hero.ORF")
        (root / "hero.ORF").write_bytes(b"raw")
        # A SECOND, distinct raw for the verbs whose gated argument is a list
        # of more than one image. Substituting the same path twice used to be
        # enough; it no longer is, because `export.run` now refuses two
        # inputs that would render into the same output file (carried finding
        # 6) -- and "the same file twice" is not what those gates are about.
        self.raw2 = str(root / "hero2.ORF")
        (root / "hero2.ORF").write_bytes(b"raw")
        write_minimal_style_sidecar(self.raw2)
        # Fix round 2, blocker 1: style.extract(no_filter=True) reads the
        # sidecar too now (never_include applies to the whole active
        # history, not just a requested `modules` list).
        raw_sidecar = write_minimal_style_sidecar(self.raw)
        # merge (task 7 x task 3/9): cull.apply's staleness check (design
        # 8.3) compares each frame's recorded sidecar mtime to the real
        # mtime on disk. The cull_proposal fixture below used to record
        # `{}` (no sidecar existed for self.raw at all, pre-task-7); now one
        # does, so its real mtime_ns has to be in `xmp_mtimes` or the
        # generic dry-run test mistakes an unrelated fixture interaction for
        # staleness.
        self._raw_sidecar_mtime_ns = raw_sidecar.stat().st_mtime_ns
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
        # `library.relocate` refuses a `new_root` that isn't an existing
        # directory before it ever reaches the dry-run branch, so the generic
        # "substitute self.raw" fallback below (a FILE) won't do for it.
        self.relocate_root = root / "gate-relocate-dest"
        self.relocate_root.mkdir()

        # `export.release` reads its candidates from import.json + each
        # raw's own XMP sidecar (file-based, no bridge call), and (fix round
        # 1, should-fix 4) gates on n>1 selected frames even on a FRESH
        # release -- no pre-existing output needed, mirroring export.run's
        # own n>1 gate. Two frames, both rating high enough to be selected,
        # exports/preview/ not created ahead of time.
        scouter_dir = job_path / ".scouter"
        scouter_dir.mkdir(parents=True, exist_ok=True)
        release_files = []
        for seq, name in enumerate(("gate_0001.orf", "gate_0002.orf"), start=1):
            release_raw = job_path / "raw" / name
            release_raw.write_bytes(b"raw")
            Path(str(release_raw) + ".xmp").write_text('<x xmp:Rating="2"/>', encoding="utf-8")
            # `library.import_card`'s dry run reads and shape-checks any
            # EXISTING import.json (task 3 fix round 1's manifest merge), so
            # this fixture's entries need every key `_REQUIRED_FILE_KEYS`
            # names -- not just the ones `export.release` itself reads.
            release_files.append(
                {"dest": f"raw/{name}", "sha256": "deadbeef", "seq": seq, "source_name": name}
            )
        (scouter_dir / "import.json").write_text(
            json.dumps({"files": release_files}), encoding="utf-8"
        )

        # `cull.apply` reads its proposal from a real file (`--proposal`, not
        # an image argument), so this harness's usual "None -> self.raw"
        # substitution does not apply to it -- a one-frame proposal that
        # actually changes a rating is written here instead, so the dry run
        # has a non-empty plan to report.
        self.cull_proposal = root / "cull-gate.json"
        self.cull_proposal.write_text(
            json.dumps(
                {
                    "version": 1,
                    "job_slug": self.job_slug,
                    "frames": [
                        {
                            "path": self.raw,
                            "image_id": 1,
                            "seq": 1,
                            "rating_now": 0,
                            "rating_proposed": 2,
                        }
                    ],
                    "groups": [],
                    "xmp_mtimes": {self.raw: self._raw_sidecar_mtime_ns},
                }
            ),
            encoding="utf-8",
        )
        # `backup.run`'s gate check runs even for a dry run (design 4.3's
        # order is gate -> enumerate -> ...), so this fixture needs a
        # gate-satisfiable config for that dry run to ever be reached.
        self._env_saved = dict(os.environ)
        backup_target = root / "backup-target"
        backup_target.mkdir()
        passfile = root / "backup.pass"
        passfile.write_text("test-passphrase\n")
        passfile.chmod(0o600)
        config_path = root / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "library_root": str(root / "library"),
                    "backup": {
                        "enabled": True,
                        "target": str(backup_target),
                        "encryption": "openssl",
                        "openssl_passfile": str(passfile),
                    },
                }
            )
        )
        os.environ["SCOUTER_CONFIG"] = str(config_path)

    def tearDown(self) -> None:
        self._lib_cfg_patch.stop()
        dtbridge.set_bridge(None)
        self.cache.stop()
        os.environ.clear()
        os.environ.update(self._env_saved)

    #: Minimal args per gated verb, WITHOUT confirm.
    GATED_ARGS = {
        "config.init": {"path": None},
        "darkroom.reset": {"module": "exposure"},
        # no_filter=True: this generic test only exercises the "would
        # replace an existing style" confirm gate, common to every gated
        # verb -- not style.extract's own module-filter refusals (fix round
        # 1, should-fix 8, tested directly in tests/test_style_dtstyle.py),
        # which would otherwise fire first against this fake's empty modules
        # / absent sidecar and mask the thing this test actually checks.
        "style.extract": {"image": None, "name": "hero", "modules": [], "no_filter": True},
        "style.apply_set": {"images": [None, None], "name": "hero"},
        "export.run": {"images": [None, None], "preset": "preview", "out_dir": None},
        "library.import": {"source": None, "job_slug": "gate-hero"},
        "library.register": {"job_slug": "gate-hero"},
        "export.release": {"job_slug": "gate-hero", "preset": "preview"},
        "library.relocate": {"new_root": None},
        # `--proposal` is a JSON file path, not an image -- the generic
        # `None -> self.raw` substitution below is for image-shaped args, so
        # this placeholder is swapped for `self.cull_proposal` explicitly in
        # `test_every_gated_verb_dry_runs_without_confirm` instead.
        "cull.apply": {"proposal": "<cull-proposal>"},
        "backup.run": {},
        # `k_from`/`k_to` need no fixture staging (task 11): the confirm gate
        # fires before any EXIF/camera lookup, so `self.raw`'s fake bytes
        # (not a readable TIFF header) never get that far without confirm.
        "wb.calibrate": {"image": None, "k_from": 3000, "k_to": 3100},
        # preset.save checks `style._existing_styles()` (== list_styles, which
        # this fixture's `RefusingBridge` answers with "hero") BEFORE
        # resolving the darkroom image or touching it in any way -- so no
        # `darkroom.current`/`view.current` fixture wiring is needed here,
        # unlike every other gated verb above.
        "preset.save": {"name": "hero"},
        # preset.apply_job is a thin delegate to style.apply_set: apply_set's
        # own confirm gate (already exercised above) fires first, so no
        # darkroom/bridge fixture wiring is needed here either.
        "preset.apply_job": {"name": "hero", "job_slug": "gate-hero"},
        # job.run without confirm runs NO step: a plan, plus the child dry
        # runs (apply-set, export) that are themselves gated above.
        "job.run": {"job_slug": "gate-hero", "preset": "hero"},
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
                                str(self.relocate_root)
                                if k == "new_root"
                                else (
                                    self.raw
                                    if v is None
                                    else (
                                        [self.raw, self.raw2][: len(v)]
                                        if isinstance(v, list) and v and v[0] is None
                                        else v
                                    )
                                )
                            )
                        )
                    )
                    for k, v in args.items()
                }
                if verb_id == "cull.apply":
                    filled["proposal"] = str(self.cull_proposal)
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
