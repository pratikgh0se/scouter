"""Task 8 + fix round 1: `style.apply_set` -- batches of 25, the `partial`
outcome, honest batch-level error handling, unknown-status timeouts, and
the idempotency guard.

`partial` keeps the successes: any `ScouterError` on one batch marks that
batch's ids as failed (or, for a real client-side `BridgeTimeout`, as
"unknown" -- the bridge genuinely does not know whether darktable finished)
and CONTINUES (a stalled worker on frame 140 must not lose frames 1-139),
up to two consecutive batch failures, after which the run stops with
whatever it has. `StylePartial` (the exception) is reserved for the one
case nothing survives: zero images applied, and something actually failed
(everything already carrying the style is not a failure).
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from darktable_mcp.bridge.client import BridgeError, BridgeTimeoutError
from scouter import registry
from scouter.core import dtbridge, style
from scouter.core.style import UnsupportedMode
from scouter.errors import StylePartial, UsageError, exit_code_for
from tests.scouter_fakes import FakeBridge, TempCache

MULTI_NAME_SIDECAR = """<?xml version="1.0" encoding="UTF-8"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:darktable="http://darktable.sf.net/"
    darktable:history_end="1"
    darktable:xmp_version="4"
    darktable:iop_order_version="1">
   <darktable:history>
    <rdf:Seq>
     <rdf:li
      darktable:num="0"
      darktable:operation="exposure"
      darktable:enabled="1"
      darktable:modversion="1"
      darktable:params="00"
      darktable:multi_name="{style_name}"
      darktable:multi_priority="0"
      darktable:blendop_params=""
      darktable:blendop_version="0"
      darktable:multi_name_hand_edited="0"/>
    </rdf:Seq>
   </darktable:history>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
"""

PLAIN_SIDECAR = MULTI_NAME_SIDECAR.replace("{style_name}", "")


def _images(root: Path, n: int, start: int = 1) -> dict[int, str]:
    """`n` distinct fake raws, ids `start..start+n-1`, never written to disk --
    `image.lookup` is the only thing that touches them (unless a test writes
    a real `.xmp` sidecar for one, e.g. the idempotency tests)."""
    return {start + i: str(root / f"img{start + i:04d}.orf") for i in range(n)}


def _bridge(images: dict[int, str], *, style_name: str = "hero", apply=None) -> FakeBridge:
    """A FakeBridge whose `image.lookup` resolves by path OR by id, driven by
    `images` -- the stock fixture's fixed-dict response cannot do this (every
    call would resolve to the same one id), which apply_set's batch tests need."""
    path_to_id = {p: i for i, p in images.items()}

    def lookup(params: dict) -> dict:
        if params.get("image_id") is not None:
            iid = int(params["image_id"])
            return {"found": iid in images, "image_id": iid, "path": images.get(iid, "")}
        path = params.get("path")
        iid = path_to_id.get(path)
        return {"found": iid is not None, "image_id": iid or 0, "path": path}

    responses: dict = {
        "image.lookup": lookup,
        "list_styles": {"styles": [{"name": style_name}], "count": 1},
    }
    if apply is not None:
        responses["style.apply"] = apply
    return FakeBridge(responses)


def _apply_all_ok(params: dict) -> dict:
    ids = params["image_ids"]
    return {"applied": len(ids), "missed": [], "errors": []}


def _apply_failing(fail_ids: set[int]):
    def _respond(params: dict) -> dict:
        ids = params["image_ids"]
        missed = [i for i in ids if i in fail_ids]
        return {"applied": len(ids) - len(missed), "missed": missed, "errors": []}

    return _respond


class _Script:
    """One `style.apply` response (or exception) per call, in order."""

    def __init__(self, steps: list) -> None:
        self.steps = list(steps)
        self.n = 0

    def __call__(self, params: dict) -> dict:
        step = self.steps[self.n]
        self.n += 1
        if isinstance(step, BaseException):
            raise step
        return step(params) if callable(step) else step


def _statuses(per_image: list[dict]) -> dict[int, str]:
    return {row["image_id"]: row["status"] for row in per_image if row["image_id"] is not None}


class StyleSetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = TempCache()
        self.root = self.cache.start()

    def tearDown(self) -> None:
        dtbridge.set_bridge(None)
        self.cache.stop()

    def _apply_set(self, known_images: dict[int, str], *, apply=None, style_name="hero", **kwargs):
        """`known_images` builds the FakeBridge's `image.lookup` table; the
        `images` argument passed to `apply_set` itself defaults to the same
        set but can be overridden in `kwargs` (e.g. to add an unresolvable
        stray path the bridge does not know about)."""
        bridge = _bridge(known_images, style_name=style_name, apply=apply)
        dtbridge.set_bridge(bridge)
        kwargs.setdefault("name", style_name)
        kwargs.setdefault("images", list(known_images.values()))
        kwargs.setdefault("confirm", True)
        return bridge, style.apply_set(**kwargs)

    # -- batching --------------------------------------------------------

    def test_batches_at_the_batch_size(self) -> None:
        images = _images(self.root, 60)
        bridge, result = self._apply_set(images, apply=_apply_all_ok, batch=25)
        apply_calls = [p for m, p in bridge.calls if m == "style.apply"]
        self.assertEqual(len(apply_calls), 3)
        self.assertEqual([len(c["image_ids"]) for c in apply_calls], [25, 25, 10])
        self.assertEqual(result.outcome, "ok")
        self.assertEqual(result.applied, 60)
        self.assertEqual(result.requested, 60)

    def test_partial_keeps_successes(self) -> None:
        images = _images(self.root, 60)
        fail_ids = {5, 17, 33, 58}
        bridge, result = self._apply_set(images, apply=_apply_failing(fail_ids), batch=25)
        self.assertEqual(result.outcome, "partial")
        self.assertEqual(result.applied, 56)
        self.assertEqual(result.failed, 4)
        self.assertEqual(len(result.per_image), 60)
        self.assertEqual(exit_code_for("partial"), 9)
        self.assertEqual(sorted(result.missed), sorted(fail_ids))
        statuses = _statuses(result.per_image)
        for iid in fail_ids:
            self.assertEqual(statuses[iid], "failed")

    def test_nothing_applied_raises(self) -> None:
        images = _images(self.root, 10)
        with self.assertRaises(StylePartial):
            self._apply_set(images, apply=_apply_failing(set(images)), batch=25)

    def test_one_batch_timeout_does_not_lose_earlier_batches(self) -> None:
        images = _images(self.root, 50)  # 2 batches at batch=25
        script = _Script([_apply_all_ok, BridgeTimeoutError("darktable stopped responding")])
        bridge, result = self._apply_set(images, apply=script, batch=25)
        self.assertEqual(result.outcome, "partial")
        self.assertEqual(result.applied, 25)
        self.assertEqual(result.batches, 2)
        applied_ids = {p["image_id"] for p in result.per_image if p["status"] == "applied"}
        self.assertEqual(applied_ids, set(list(images)[:25]))

    def test_two_consecutive_batch_failures_stop_the_run(self) -> None:
        images = _images(self.root, 100)  # 4 batches at batch=25
        timeout = BridgeTimeoutError("darktable stopped responding")
        script = _Script([_apply_all_ok, timeout, timeout, _apply_all_ok])
        bridge, result = self._apply_set(images, apply=script, batch=25)
        self.assertEqual(result.batches, 3)  # the 4th batch is never attempted
        self.assertEqual(result.applied, 25)
        self.assertEqual(result.failed, 0)  # both timed-out batches are "unknown", not "failed"
        unknown_count = sum(1 for p in result.per_image if p["status"] == "unknown")
        self.assertEqual(unknown_count, 50)
        never_attempted = result.requested - result.applied - result.failed - unknown_count
        self.assertEqual(never_attempted, 25)  # the 4th batch's ids
        self.assertTrue(
            any("never attempted" in w for w in result.warnings),
            f"expected a warning naming the unattempted count, got {result.warnings}",
        )
        self.assertEqual(result.outcome, "partial")

    # -- fix round 1: batch-level ScouterError, not only BridgeTimeout ---

    def test_errors_branch_real_apply_failure(self) -> None:
        """A definite bridge refusal (DbLocked, via a real `BridgeError`
        message) on one batch is recorded as `status="failed"`, not
        "unknown", never propagates, and earlier/later batches still run."""
        images = _images(self.root, 75)  # 3 batches at batch=25
        script = _Script(
            [_apply_all_ok, BridgeError("database is locked (simulated)"), _apply_all_ok]
        )
        bridge, result = self._apply_set(images, apply=script, batch=25)
        self.assertEqual(result.batches, 3)
        self.assertEqual(result.applied, 50)
        self.assertEqual(result.failed, 25)
        self.assertEqual(result.outcome, "partial")
        statuses = _statuses(result.per_image)
        second_batch_ids = list(images)[25:50]
        for iid in second_batch_ids:
            self.assertEqual(statuses[iid], "failed")
            self.assertNotEqual(statuses[iid], "unknown")

    # -- fix round 1: unknown status for a real client-side timeout ------

    def test_unknown_status_timeout(self) -> None:
        images = _images(self.root, 50)  # 2 batches at batch=25
        script = _Script([_apply_all_ok, BridgeTimeoutError("no response within budget")])
        bridge, result = self._apply_set(images, apply=script, batch=25)
        self.assertEqual(result.outcome, "partial")
        self.assertEqual(result.applied, 25)
        # unknown ids are excluded from both applied and failed
        self.assertEqual(result.failed, 0)
        self.assertEqual(result.missed, [])
        statuses = _statuses(result.per_image)
        timed_out_ids = list(images)[25:]
        for iid in timed_out_ids:
            self.assertEqual(statuses[iid], "unknown")
        unknown_rows = [p for p in result.per_image if p["status"] == "unknown"]
        self.assertEqual(len(unknown_rows), 25)
        for row in unknown_rows:
            self.assertIn("may or may not have applied", row["error"])
        self.assertTrue(
            any("unknown status" in w for w in result.warnings),
            f"expected a warning naming the unknown ids, got {result.warnings}",
        )

    def test_dry_run_checks_the_style_exists(self) -> None:
        images = _images(self.root, 3)
        with self.assertRaises(UsageError):
            self._apply_set(
                images, apply=_apply_all_ok, style_name="hero", name="not-hero", dry_run=True
            )
        bridge = dtbridge.get_bridge()
        self.assertEqual(bridge.count("style.apply"), 0)

    def test_overwrite_refused_names_the_render_path(self) -> None:
        images = _images(self.root, 3)
        with self.assertRaises(UnsupportedMode) as ctx:
            self._apply_set(images, apply=_apply_all_ok, mode="overwrite")
        self.assertIn("export.release", str(ctx.exception))

    def test_exclude_removes_the_hero(self) -> None:
        images = _images(self.root, 5)
        hero_id = min(images)
        hero_path = images[hero_id]
        bridge, result = self._apply_set(images, apply=_apply_all_ok, exclude=[hero_path])
        self.assertEqual(result.requested, 4)
        self.assertNotIn(hero_id, {p["image_id"] for p in result.per_image})

    def test_exclude_by_id(self) -> None:
        images = _images(self.root, 5)
        hero_id = min(images)
        bridge, result = self._apply_set(images, apply=_apply_all_ok, exclude=[hero_id])
        self.assertEqual(result.requested, 4)
        self.assertNotIn(hero_id, {p["image_id"] for p in result.per_image})

    def test_gated_without_confirm(self) -> None:
        images = _images(self.root, 5)
        bridge, result = self._apply_set(images, apply=_apply_all_ok, confirm=False)
        self.assertEqual(result.outcome, "dry_run")
        self.assertEqual(bridge.count("style.apply"), 0)
        self.assertTrue(result.plan)

    def test_resolves_from_import_json_when_no_images_given(self) -> None:
        """`--job-slug` alone: every file in `<job>/.scouter/import.json`."""
        from scouter import config as config_module

        library_root = self.root / "library"
        cfg = config_module.Config(library_root=str(library_root), exists=True)
        job_path = config_module.job_dir(cfg, "2026-06-28", "harbour-wedding")
        (job_path / "raw").mkdir(parents=True)
        scouter_dir = config_module.scouter_dir(job_path)
        scouter_dir.mkdir(parents=True)

        images: dict[int, str] = {}
        files: list[dict] = []
        for i in range(1, 5):
            dest = f"raw/img{i:04d}.orf"
            (job_path / dest).write_bytes(b"")
            images[i] = str(job_path / dest)
            files.append({"dest": dest, "seq": i})
        manifest = {"version": 1, "job_slug": "harbour-wedding", "files": files}
        (scouter_dir / "import.json").write_text(json.dumps(manifest))

        import os

        config_path = self.root / "config.json"
        config_path.write_text(json.dumps({"version": 1, "library_root": str(library_root)}))
        os.environ["SCOUTER_CONFIG"] = str(config_path)
        try:
            bridge = _bridge(images, apply=_apply_all_ok)
            dtbridge.set_bridge(bridge)
            result = style.apply_set(name="hero", job_slug="harbour-wedding", confirm=True)
        finally:
            os.environ.pop("SCOUTER_CONFIG", None)
        self.assertEqual(result.requested, 4)
        self.assertEqual(result.applied, 4)

    # -- fix round 1: resolution never kills the set --------------------

    def test_unresolvable_raw_becomes_a_failure_row_not_a_crash(self) -> None:
        images = _images(self.root, 4)
        stray = str(self.root / "not-in-darktable.orf")
        bridge, result = self._apply_set(
            images, apply=_apply_all_ok, images=[*images.values(), stray]
        )
        self.assertEqual(result.outcome, "partial")
        self.assertEqual(result.requested, 5)
        self.assertEqual(result.applied, 4)
        self.assertEqual(result.failed, 1)
        failed_rows = [p for p in result.per_image if p["status"] == "failed"]
        self.assertEqual(len(failed_rows), 1)
        self.assertEqual(failed_rows[0]["path"], stray)
        self.assertIsNone(failed_rows[0]["image_id"])

    def test_unresolvable_raw_does_not_kill_the_dry_run(self) -> None:
        images = _images(self.root, 4)
        stray = str(self.root / "not-in-darktable.orf")
        bridge, result = self._apply_set(
            images,
            apply=_apply_all_ok,
            images=[*images.values(), stray],
            confirm=False,
        )
        self.assertEqual(result.outcome, "dry_run")
        self.assertEqual(result.requested, 5)
        self.assertTrue(any("did not resolve" in w for w in result.warnings))

    # -- fix round 1: nits -------------------------------------------------

    def test_batch_out_of_range_is_usage_error(self) -> None:
        images = _images(self.root, 3)
        with self.assertRaises(UsageError):
            self._apply_set(images, apply=_apply_all_ok, batch=0)
        with self.assertRaises(UsageError):
            self._apply_set(images, apply=_apply_all_ok, batch=101)

    def test_empty_images_list_is_usage_error(self) -> None:
        images = _images(self.root, 3)
        bridge = _bridge(images, apply=_apply_all_ok)
        dtbridge.set_bridge(bridge)
        with self.assertRaises(UsageError):
            style.apply_set(name="hero", images=[], confirm=True)
        self.assertEqual(bridge.count("style.apply"), 0)

    # -- fix round 1: idempotency guard -----------------------------------

    def test_already_has_style_skip(self) -> None:
        images = _images(self.root, 3)
        styled_id = min(images)
        styled_path = images[styled_id]
        Path(styled_path + ".xmp").write_text(MULTI_NAME_SIDECAR.format(style_name="hero"))
        for iid, path in images.items():
            if iid != styled_id:
                Path(path + ".xmp").write_text(PLAIN_SIDECAR)

        bridge, result = self._apply_set(images, apply=_apply_all_ok)
        apply_calls = [p for m, p in bridge.calls if m == "style.apply"]
        applied_ids = {iid for call in apply_calls for iid in call["image_ids"]}
        self.assertNotIn(styled_id, applied_ids)
        statuses = _statuses(result.per_image)
        self.assertEqual(statuses[styled_id], "already_has_style")
        self.assertEqual(result.applied, 2)
        self.assertTrue(any("already carried this style" in w for w in result.warnings))

    def test_force_applies_even_when_already_has_style(self) -> None:
        images = _images(self.root, 3)
        styled_id = min(images)
        styled_path = images[styled_id]
        Path(styled_path + ".xmp").write_text(MULTI_NAME_SIDECAR.format(style_name="hero"))
        for iid, path in images.items():
            if iid != styled_id:
                Path(path + ".xmp").write_text(PLAIN_SIDECAR)

        bridge, result = self._apply_set(images, apply=_apply_all_ok, force=True)
        apply_calls = [p for m, p in bridge.calls if m == "style.apply"]
        applied_ids = {iid for call in apply_calls for iid in call["image_ids"]}
        self.assertIn(styled_id, applied_ids)
        self.assertEqual(result.applied, 3)

    def test_all_already_applied_is_ok_not_partial(self) -> None:
        images = _images(self.root, 2)
        for path in images.values():
            Path(path + ".xmp").write_text(MULTI_NAME_SIDECAR.format(style_name="hero"))
        bridge, result = self._apply_set(images, apply=_apply_all_ok)
        self.assertEqual(result.outcome, "ok")
        self.assertEqual(result.applied, 0)
        self.assertEqual(result.failed, 0)
        self.assertEqual(bridge.count("style.apply"), 0)

    # -- fix round 1: registry integration ---------------------------------

    def test_partial_envelope_through_registry_has_exit_code_nine(self) -> None:
        images = _images(self.root, 10)
        fail_ids = {min(images)}
        bridge = _bridge(images, apply=_apply_failing(fail_ids))
        dtbridge.set_bridge(bridge)
        envelope = registry.call(
            "style.apply_set",
            "core",
            name="hero",
            images=list(images.values()),
            confirm=True,
        )
        self.assertEqual(envelope.outcome, "partial")
        self.assertFalse(envelope.ok)
        self.assertEqual(exit_code_for(envelope.outcome), 9)
        self.assertEqual(envelope.data["applied"], 9)
        self.assertEqual(envelope.data["failed"], 1)


if __name__ == "__main__":
    unittest.main()
