"""Task 8 + fix rounds 1-3: `style.apply_set` -- batches of 25, the
`partial` outcome, honest batch-level error handling, unknown-status
timeouts, and the ledger-based idempotency guard (Rulings R16 and R18).

`partial` keeps the successes: any `ScouterError` on one batch marks that
batch's ids as failed (or, for a real client-side `BridgeTimeout`, as
"unknown" -- the bridge genuinely does not know whether darktable finished)
and CONTINUES (a stalled worker on frame 140 must not lose frames 1-139),
up to two consecutive batch failures, after which the run stops with
whatever it has (`per_image` gets a `"not_attempted"` row for each id that
never got a batch). `StylePartial` (the exception) fires only when
`applied == 0` AND `failed > 0` AND nothing is `"unknown"` -- fix round 2,
item 5: `applied == 0` with any `"unknown"` id is `outcome="partial"`
instead, never a raised exception, because "we do not know" is never the
same claim as "this failed".

Fix round 2 (Ruling R16) replaced round 1's darktable `multi_name`
heuristic outright: idempotency is now Scouter's OWN per-job ledger,
`<job>/.scouter/styles-applied.json`. Fix round 3 (Ruling R18) fixed the
ledger's own field and comparison: the Lua `style.apply` handler never
rewrites the sidecar, so round 2's "read history_end right after the
apply" was really still a pre-apply read, and its `>=` check was inert.
The ledger now records `history_end_before` (read right BEFORE the apply)
and the guard requires the CURRENT sidecar's `history_end` to be STRICTLY
GREATER than it -- proof the sidecar was genuinely rewritten (committed)
since. Equal is `"unknown"` (nothing committed since the apply); less is
treated as rewound (re-applies, with a warning); `history_end_before=None`
is `"unknown"` too, never "assume still applied". The ledger is flushed
to disk after every successful batch, not once at the end.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest import mock

from darktable_mcp.bridge.client import BridgeError, BridgeTimeoutError
from scouter import registry
from scouter.core import dtbridge, style
from scouter.core.style import UnsupportedMode
from scouter.errors import NotInDarkroom, StylePartial, UsageError, exit_code_for
from tests.scouter_fakes import FakeBridge, TempCache


def _sidecar_xml(history_end: int) -> str:
    """A minimal, well-formed darktable-shaped sidecar -- `xmp.parse` only
    needs the RDF/darktable namespaces and one `rdf:li` history entry to
    consider it well-formed; the entry's own content is irrelevant to the
    ledger guard (fix round 2 dropped the `multi_name` match entirely)."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:darktable="http://darktable.sf.net/"
    darktable:history_end="{history_end}"
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
      darktable:multi_name=""
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

    def one(params: dict) -> dict:
        if params.get("image_id") is not None:
            iid = int(params["image_id"])
            return {"found": iid in images, "image_id": iid, "path": images.get(iid, "")}
        path = params.get("path")
        iid = path_to_id.get(path)
        return {"found": iid is not None, "image_id": iid or 0, "path": path}

    def lookup(params: dict) -> dict:
        # Mirrors the real Lua handler's two forms (ruling R15): a bare
        # record for the single form, `{results: [...]}` in request order
        # for the list form. Without the list form here the fake would push
        # `resolve_images` onto its legacy fallback and these tests would
        # silently stop covering the batched path.
        if params.get("items") is not None:
            return {"results": [one(item) for item in params["items"]]}
        return one(params)

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
        self.job_path: Path | None = None

    def tearDown(self) -> None:
        dtbridge.set_bridge(None)
        os.environ.pop("SCOUTER_CONFIG", None)
        self.cache.stop()

    def _setup_job(self, n: int, slug: str = "hero-job") -> dict[int, str]:
        """A real job dir + `import.json` under a temp library, with
        `SCOUTER_CONFIG` pointed at a matching config -- the ledger lives at
        `<job>/.scouter/styles-applied.json`, so the fix-round-2 tests need
        an actual job directory on disk, not just a bridge fixture. Sets
        `self.job_path` for tests that seed or read the ledger directly.
        """
        from scouter import config as config_module

        library_root = self.root / "library"
        cfg = config_module.Config(library_root=str(library_root), exists=True)
        job_path = config_module.job_dir(cfg, "2026-06-28", slug)
        (job_path / "raw").mkdir(parents=True, exist_ok=True)
        scouter_dir = config_module.scouter_dir(job_path)
        scouter_dir.mkdir(parents=True, exist_ok=True)

        images: dict[int, str] = {}
        files: list[dict] = []
        for i in range(1, n + 1):
            dest = f"raw/img{i:04d}.orf"
            (job_path / dest).write_bytes(b"")
            images[i] = str(job_path / dest)
            files.append({"dest": dest, "seq": i})
        manifest = {"version": 1, "job_slug": slug, "files": files}
        (scouter_dir / "import.json").write_text(json.dumps(manifest))

        config_path = self.root / f"config-{slug}.json"
        config_path.write_text(json.dumps({"version": 1, "library_root": str(library_root)}))
        os.environ["SCOUTER_CONFIG"] = str(config_path)
        self.job_path = job_path
        return images

    def _ledger_path(self) -> Path:
        assert self.job_path is not None
        return self.job_path / ".scouter" / "styles-applied.json"

    def _seed_ledger(self, entries: list[dict]) -> None:
        """Fix round 3b: the ledger file is `{"schema": <n>, "entries": [...]}`,
        not a bare array."""
        self._ledger_path().write_text(json.dumps({"schema": 2, "entries": entries}))

    def _ledger_entries(self) -> list[dict]:
        return json.loads(self._ledger_path().read_text())["entries"]

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
        images = self._setup_job(4, slug="harbour-wedding")
        bridge = _bridge(images, apply=_apply_all_ok)
        dtbridge.set_bridge(bridge)
        result = style.apply_set(name="hero", job_slug="harbour-wedding", confirm=True)
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

    # -- fix round 2 (Ruling R16) / fix round 3 (Ruling R18): ledger-based
    # idempotency guard, comparing history_end_before -----------------------

    def _job_image_path(self, image_id: int) -> str:
        """The path `_setup_job` gave image `image_id` -- the ledger records
        it alongside the id (schema 2), because darktable reuses ids."""
        assert self.job_path is not None
        return str(self.job_path / f"raw/img{image_id:04d}.orf")

    def _seed_one(self, styled_id: int, history_end_before, path: str | None = None) -> None:
        self._seed_ledger(
            [
                {
                    "image_id": styled_id,
                    "path": path if path is not None else self._job_image_path(styled_id),
                    "style": "hero",
                    "applied_at": "2026-01-01T00:00:00Z",
                    "history_end_before": history_end_before,
                }
            ]
        )

    def test_ledger_skip(self) -> None:
        """Fix round 3: the CURRENT sidecar's history_end must be STRICTLY
        GREATER than the ledger's history_end_before -- proof the sidecar
        was genuinely rewritten (committed) since the recorded apply."""
        images = self._setup_job(3)
        styled_id = min(images)
        self._seed_one(styled_id, history_end_before=3)
        Path(images[styled_id] + ".xmp").write_text(_sidecar_xml(history_end=5))

        bridge, result = self._apply_set(images, apply=_apply_all_ok, job_slug="hero-job")
        apply_calls = [p for m, p in bridge.calls if m == "style.apply"]
        applied_ids = {iid for call in apply_calls for iid in call["image_ids"]}
        self.assertNotIn(styled_id, applied_ids)
        statuses = _statuses(result.per_image)
        self.assertEqual(statuses[styled_id], "already_styled")
        self.assertEqual(result.applied, 2)
        self.assertTrue(any("skip decided from Scouter's own record" in w for w in result.warnings))
        self.assertTrue(any("already_styled" in w for w in result.warnings))

    def test_ledger_current_equal_to_before_is_unknown(self) -> None:
        """Fix round 3, item 1: nothing has been committed since the apply
        -- indistinguishable from "never really applied" without a bridge
        check, so this is "unknown", never silently "already styled"."""
        images = self._setup_job(2)
        styled_id = min(images)
        self._seed_one(styled_id, history_end_before=3)
        Path(images[styled_id] + ".xmp").write_text(_sidecar_xml(history_end=3))

        bridge, result = self._apply_set(images, apply=_apply_all_ok, job_slug="hero-job")
        statuses = _statuses(result.per_image)
        self.assertEqual(statuses[styled_id], "unknown")
        row = next(p for p in result.per_image if p["image_id"] == styled_id)
        self.assertIn("sidecar not rewritten since the apply", row["error"])
        self.assertIn("commit history", row["error"])
        apply_calls = [p for m, p in bridge.calls if m == "style.apply"]
        applied_ids = {iid for call in apply_calls for iid in call["image_ids"]}
        self.assertNotIn(styled_id, applied_ids)  # skipped, never applied blind

    def test_ledger_current_less_than_before_is_rewound_and_reapplies(self) -> None:
        """Fix round 3, item 1: a history rewound past the recorded apply is
        treated as NOT styled (re-applies), with a warning."""
        images = self._setup_job(2)
        styled_id = min(images)
        self._seed_one(styled_id, history_end_before=5)
        Path(images[styled_id] + ".xmp").write_text(_sidecar_xml(history_end=3))

        bridge, result = self._apply_set(images, apply=_apply_all_ok, job_slug="hero-job")
        statuses = _statuses(result.per_image)
        self.assertEqual(statuses[styled_id], "applied")
        apply_calls = [p for m, p in bridge.calls if m == "style.apply"]
        applied_ids = {iid for call in apply_calls for iid in call["image_ids"]}
        self.assertIn(styled_id, applied_ids)
        self.assertTrue(any("rewound" in w for w in result.warnings))

    def test_ledger_history_end_before_none_is_unknown(self) -> None:
        """Fix round 3, item 1: history_end_before=None on the ledger record
        (the sidecar was unreadable when it was written) is "unknown" --
        never "assume still applied" the way round 2 read it."""
        images = self._setup_job(2)
        styled_id = min(images)
        self._seed_one(styled_id, history_end_before=None)
        # Even a confirmed, well-formed current sidecar changes nothing --
        # there is no baseline to compare it against.
        Path(images[styled_id] + ".xmp").write_text(_sidecar_xml(history_end=5))

        bridge, result = self._apply_set(images, apply=_apply_all_ok, job_slug="hero-job")
        statuses = _statuses(result.per_image)
        self.assertEqual(statuses[styled_id], "unknown")
        row = next(p for p in result.per_image if p["image_id"] == styled_id)
        self.assertIn("nothing to compare against", row["error"])
        apply_calls = [p for m, p in bridge.calls if m == "style.apply"]
        applied_ids = {iid for call in apply_calls for iid in call["image_ids"]}
        self.assertNotIn(styled_id, applied_ids)

    def test_force_ignores_ledger(self) -> None:
        images = self._setup_job(3)
        styled_id = min(images)
        self._seed_one(styled_id, history_end_before=3)
        Path(images[styled_id] + ".xmp").write_text(_sidecar_xml(history_end=5))

        bridge, result = self._apply_set(
            images, apply=_apply_all_ok, job_slug="hero-job", force=True
        )
        apply_calls = [p for m, p in bridge.calls if m == "style.apply"]
        applied_ids = {iid for call in apply_calls for iid in call["image_ids"]}
        self.assertIn(styled_id, applied_ids)
        self.assertEqual(result.applied, 3)

    def test_force_updates_ledger_entry(self) -> None:
        """Fix round 3, item 4: force=True still writes the ledger (with
        this run's own history_end_before) -- it only skips the SKIP
        decision, a forced re-application is still a real one."""
        images = self._setup_job(1)
        styled_id = min(images)
        self._seed_one(styled_id, history_end_before=1)
        Path(images[styled_id] + ".xmp").write_text(_sidecar_xml(history_end=9))

        bridge, result = self._apply_set(
            images, apply=_apply_all_ok, job_slug="hero-job", force=True
        )
        self.assertEqual(result.applied, 1)
        entries = self._ledger_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["image_id"], styled_id)
        self.assertEqual(entries[0]["history_end_before"], 9)

    def test_all_already_applied_is_ok_not_partial(self) -> None:
        images = self._setup_job(2)
        self._seed_ledger(
            [
                {
                    "image_id": iid,
                    "path": self._job_image_path(iid),
                    "style": "hero",
                    "applied_at": "2026-01-01T00:00:00Z",
                    "history_end_before": 3,
                }
                for iid in images
            ]
        )
        for path in images.values():
            Path(path + ".xmp").write_text(_sidecar_xml(history_end=5))  # > 3, so "styled"

        bridge, result = self._apply_set(images, apply=_apply_all_ok, job_slug="hero-job")
        self.assertEqual(result.outcome, "ok")
        self.assertEqual(result.applied, 0)
        self.assertEqual(result.failed, 0)
        self.assertEqual(bridge.count("style.apply"), 0)

    def test_unknown_sidecar_skips_as_unknown(self) -> None:
        """A ledger record whose CURRENT sidecar cannot be confirmed (here:
        missing entirely) is "unknown", not "already styled" and not
        silently applied -- never a guess either way."""
        images = self._setup_job(2)
        styled_id = min(images)
        self._seed_one(styled_id, history_end_before=3)
        # No sidecar written at all -- the guard cannot verify anything.

        bridge, result = self._apply_set(images, apply=_apply_all_ok, job_slug="hero-job")
        statuses = _statuses(result.per_image)
        self.assertEqual(statuses[styled_id], "unknown")
        unknown_row = next(p for p in result.per_image if p["image_id"] == styled_id)
        self.assertIn("no sidecar on disk yet", unknown_row["error"])
        self.assertEqual(result.applied, 1)  # the other image still applies normally
        apply_calls = [p for m, p in bridge.calls if m == "style.apply"]
        applied_ids = {iid for call in apply_calls for iid in call["image_ids"]}
        self.assertNotIn(styled_id, applied_ids)

    def test_torn_sidecar_also_skips_as_unknown(self) -> None:
        images = self._setup_job(1)
        styled_id = min(images)
        self._seed_one(styled_id, history_end_before=3)
        Path(images[styled_id] + ".xmp").write_text("<not really xml, mid-write>")

        bridge, result = self._apply_set(images, apply=_apply_all_ok, job_slug="hero-job")
        statuses = _statuses(result.per_image)
        self.assertEqual(statuses[styled_id], "unknown")
        self.assertEqual(result.applied, 0)

    def test_guard_error_becomes_unknown(self) -> None:
        """Fix round 2, item 3: the guard call itself is wrapped in
        `except ScouterError` at the call site -- a failure there is
        recorded as "unknown", never propagated past the gate."""
        images = _images(self.root, 3)
        with mock.patch.object(style, "_idempotency_verdict", side_effect=NotInDarkroom("boom")):
            bridge, result = self._apply_set(images, apply=_apply_all_ok)
        self.assertEqual(result.outcome, "partial")
        statuses = _statuses(result.per_image)
        for iid in images:
            self.assertEqual(statuses[iid], "unknown")
        self.assertEqual(result.applied, 0)
        self.assertEqual(result.failed, 0)

    def test_all_unknown_accounting_is_partial_not_style_partial(self) -> None:
        """Fix round 2, item 5: applied == 0 with unknown > 0 is
        outcome="partial", never a raised StylePartial."""
        images = self._setup_job(2)
        self._seed_ledger(
            [
                {
                    "image_id": iid,
                    "path": self._job_image_path(iid),
                    "style": "hero",
                    "applied_at": "2026-01-01T00:00:00Z",
                    "history_end_before": 3,
                }
                for iid in images
            ]
        )
        # No sidecars written -- every target is unconfirmed, so unknown.
        bridge, result = self._apply_set(images, apply=_apply_all_ok, job_slug="hero-job")
        self.assertEqual(result.outcome, "partial")
        self.assertEqual(result.applied, 0)
        statuses = _statuses(result.per_image)
        for iid in images:
            self.assertEqual(statuses[iid], "unknown")

    def test_no_job_slug_means_no_ledger_skip(self) -> None:
        """Fix round 2, item 1: `--images` alone, with no `--job-slug`, has
        no job directory and therefore no ledger -- nothing is ever skipped
        that way, and a warning says so."""
        images = _images(self.root, 3)
        bridge, result = self._apply_set(images, apply=_apply_all_ok)
        self.assertEqual(result.applied, 3)
        self.assertTrue(any("no --job-slug" in w for w in result.warnings))

    def test_ledger_write_after_apply(self) -> None:
        """Fix round 3: `history_end_before` is read from the sidecar
        RIGHT BEFORE the batch that applies it (the Lua handler never
        rewrites the sidecar itself, so a post-apply read would just be
        another pre-apply read -- see the module docstring)."""
        images = self._setup_job(2)
        for path in images.values():
            Path(path + ".xmp").write_text(_sidecar_xml(history_end=3))

        self._apply_set(images, apply=_apply_all_ok, job_slug="hero-job")

        entries = self._ledger_entries()
        self.assertEqual(len(entries), 2)
        for entry in entries:
            self.assertEqual(entry["style"], "hero")
            self.assertEqual(entry["history_end_before"], 3)
            self.assertIn("applied_at", entry)

    def test_crash_after_batch_one_leaves_batch_ones_entries_on_disk(self) -> None:
        """Fix round 3, item 2: the ledger is flushed after EVERY successful
        batch, not once at the end -- an unhandled failure during batch 2
        must not lose what batch 1 already recorded."""
        images = self._setup_job(50)  # 2 batches at batch=25
        for path in images.values():
            Path(path + ".xmp").write_text(_sidecar_xml(history_end=1))
        script = _Script([_apply_all_ok, RuntimeError("simulated crash")])
        with self.assertRaises(RuntimeError):
            self._apply_set(images, apply=script, batch=25, job_slug="hero-job")

        entries = self._ledger_entries()
        self.assertEqual(len(entries), 25)
        self.assertEqual({e["image_id"] for e in entries}, set(list(images)[:25]))

    def test_not_attempted_rows_after_stop(self) -> None:
        """Fix round 2, item 6: `per_image` carries an explicit row for
        every id a consecutive-failure stop never reached."""
        images = _images(self.root, 100)  # 4 batches at batch=25
        timeout = BridgeTimeoutError("darktable stopped responding")
        script = _Script([_apply_all_ok, timeout, timeout, _apply_all_ok])
        bridge, result = self._apply_set(images, apply=script, batch=25)
        not_attempted = [p for p in result.per_image if p["status"] == "not_attempted"]
        self.assertEqual(len(not_attempted), 25)
        for row in not_attempted:
            self.assertIn("stopped after 2 consecutive batch failures", row["error"])
        self.assertEqual({r["image_id"] for r in not_attempted}, set(list(images)[75:100]))

    # -- fix round 3b (final re-review should-fixes) -----------------------

    def test_timeout_writes_provisional_ledger_entry_and_retry_lands_unknown(self) -> None:
        """Item 1: a BridgeTimeout batch writes PROVISIONAL ledger entries
        (history_end_before + outcome="unknown"), so a later retry's guard
        finds a record and compares -- current history_end still equals
        history_end_before (nothing committed) -- landing in "unknown"
        instead of finding no record at all and re-applying blind."""
        images = self._setup_job(25)  # exactly one batch at the default size
        for path in images.values():
            Path(path + ".xmp").write_text(_sidecar_xml(history_end=3))
        script = _Script([BridgeTimeoutError("darktable stopped responding")])

        bridge, result = self._apply_set(images, apply=script, job_slug="hero-job")
        self.assertEqual(result.outcome, "partial")

        entries = self._ledger_entries()
        self.assertEqual(len(entries), 25)
        for entry in entries:
            self.assertEqual(entry["outcome"], "unknown")
            self.assertEqual(entry["history_end_before"], 3)

        # The "retry": a fresh apply_set call against the same job. Nothing
        # committed in between, so every id must land in "unknown" -- not
        # be silently re-applied.
        bridge2, result2 = self._apply_set(images, apply=_apply_all_ok, job_slug="hero-job")
        self.assertEqual(bridge2.count("style.apply"), 0)
        statuses = _statuses(result2.per_image)
        for iid in images:
            self.assertEqual(statuses[iid], "unknown")

    def test_missing_ledger_is_empty_with_no_warning(self) -> None:
        """Item 2: a ledger that has never been written is the ordinary
        first-run state -- empty, no warning, nothing skipped."""
        images = self._setup_job(2)
        # No ledger file at all.
        bridge, result = self._apply_set(images, apply=_apply_all_ok, job_slug="hero-job")
        self.assertEqual(result.applied, 2)
        self.assertFalse(any("unreadable" in w or "malformed" in w for w in result.warnings))

    def test_corrupt_ledger_forces_unknown_with_a_warning(self) -> None:
        """Item 2: a ledger that EXISTS but cannot be parsed is not the same
        as an empty one -- every candidate is forced to "unknown" rather
        than guessed as "nothing on record, apply normally"."""
        images = self._setup_job(2)
        self._ledger_path().parent.mkdir(parents=True, exist_ok=True)
        self._ledger_path().write_text("{not valid json at all")

        bridge, result = self._apply_set(images, apply=_apply_all_ok, job_slug="hero-job")
        self.assertEqual(bridge.count("style.apply"), 0)
        self.assertEqual(result.applied, 0)
        statuses = _statuses(result.per_image)
        for iid in images:
            self.assertEqual(statuses[iid], "unknown")
        self.assertTrue(
            any(str(self._ledger_path()) in w for w in result.warnings),
            f"expected a warning naming the ledger file, got {result.warnings}",
        )

    def test_malformed_shape_ledger_also_forces_unknown(self) -> None:
        """Item 2: valid JSON that is not the `{schema, entries}` shape
        (e.g. a bare array, round 3's own on-disk format) is malformed
        too, not silently accepted as empty."""
        images = self._setup_job(2)
        self._ledger_path().parent.mkdir(parents=True, exist_ok=True)
        self._ledger_path().write_text(json.dumps([{"image_id": 1, "style": "hero"}]))

        bridge, result = self._apply_set(images, apply=_apply_all_ok, job_slug="hero-job")
        self.assertEqual(bridge.count("style.apply"), 0)
        statuses = _statuses(result.per_image)
        for iid in images:
            self.assertEqual(statuses[iid], "unknown")
        self.assertTrue(any("malformed" in w for w in result.warnings))

    def test_ledger_file_carries_schema(self) -> None:
        images = self._setup_job(1)
        self._apply_set(images, apply=_apply_all_ok, job_slug="hero-job")
        payload = json.loads(self._ledger_path().read_text())
        self.assertEqual(payload["schema"], 2)
        self.assertIsInstance(payload["entries"], list)

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


class DryRunHonoursTheGuardTest(StyleSetTest):
    """Carried finding 5 (task 8): the dry-run plan must list what the real
    run WOULD APPLY. It used to list every resolved id, including the ones
    the idempotency guard skips, so the preview promised work the apply
    would never do."""

    def test_the_dry_run_plan_excludes_ids_the_guard_would_skip(self) -> None:
        images = self._setup_job(3)
        styled_id = min(images)
        self._seed_one(styled_id, history_end_before=3)
        Path(images[styled_id] + ".xmp").write_text(_sidecar_xml(history_end=5))

        bridge, result = self._apply_set(
            images, apply=_apply_all_ok, job_slug="hero-job", dry_run=True, confirm=True
        )

        self.assertEqual(result.outcome, "dry_run")
        planned_ids = {step["image_id"] for step in result.plan}
        self.assertNotIn(styled_id, planned_ids)
        self.assertEqual(planned_ids, set(images) - {styled_id})
        # And it SAYS so rather than just quietly shortening the plan.
        self.assertTrue(any("already carry" in w for w in result.warnings), result.warnings)
        skipped = next(r for r in result.per_image if r["image_id"] == styled_id)
        self.assertEqual(skipped["status"], "already_styled")
        # Still a dry run: nothing was applied and the ledger was not written.
        self.assertEqual(bridge.count("style.apply"), 0)

    def test_the_dry_run_holds_back_unknowns_and_names_them(self) -> None:
        images = self._setup_job(2)
        styled_id = min(images)
        self._seed_one(styled_id, history_end_before=3)  # no sidecar on disk

        _bridge_, result = self._apply_set(
            images, apply=_apply_all_ok, job_slug="hero-job", dry_run=True, confirm=True
        )

        self.assertNotIn(styled_id, {step["image_id"] for step in result.plan})
        self.assertTrue(any("held back as unknown" in w for w in result.warnings), result.warnings)
        row = next(r for r in result.per_image if r["image_id"] == styled_id)
        self.assertEqual(row["status"], "unknown")

    def test_the_dry_run_plan_matches_what_the_real_run_applies(self) -> None:
        """The property behind the fix, asserted directly."""
        images = self._setup_job(4)
        styled_id = min(images)
        self._seed_one(styled_id, history_end_before=3)
        Path(images[styled_id] + ".xmp").write_text(_sidecar_xml(history_end=5))

        _b1, preview = self._apply_set(
            images, apply=_apply_all_ok, job_slug="hero-job", dry_run=True, confirm=True
        )
        bridge, real = self._apply_set(
            images, apply=_apply_all_ok, job_slug="hero-job", confirm=True
        )
        applied = {iid for m, p in bridge.calls if m == "style.apply" for iid in p["image_ids"]}
        self.assertEqual({step["image_id"] for step in preview.plan}, applied)
        self.assertEqual(real.applied, len(applied))

    def test_force_previews_every_resolved_image(self) -> None:
        """`--force` skips the SKIP decision, so its preview is the full set."""
        images = self._setup_job(3)
        styled_id = min(images)
        self._seed_one(styled_id, history_end_before=3)
        Path(images[styled_id] + ".xmp").write_text(_sidecar_xml(history_end=5))

        _b, result = self._apply_set(
            images,
            apply=_apply_all_ok,
            job_slug="hero-job",
            dry_run=True,
            confirm=True,
            force=True,
        )
        self.assertEqual({step["image_id"] for step in result.plan}, set(images))


class LedgerIdReuseTest(StyleSetTest):
    """Carried finding 5 (task 8): darktable reuses image ids, so the ledger
    records the `path` alongside the id (schema 2) and the guard checks it.
    Without that, an id freed by removing one image and handed to another
    would make the guard skip a style the new file has never carried."""

    def test_a_reused_id_is_applied_not_skipped(self) -> None:
        images = self._setup_job(2)
        styled_id = min(images)
        # The ledger's record is about a file that no longer holds this id.
        self._seed_one(styled_id, history_end_before=3, path="/gone/old-frame.orf")
        Path(images[styled_id] + ".xmp").write_text(_sidecar_xml(history_end=5))

        bridge, result = self._apply_set(images, apply=_apply_all_ok, job_slug="hero-job")

        applied = {iid for m, p in bridge.calls if m == "style.apply" for iid in p["image_ids"]}
        self.assertIn(styled_id, applied, "a reused id must not inherit the old file's record")
        self.assertEqual(_statuses(result.per_image)[styled_id], "applied")
        self.assertTrue(any("reused the id" in w for w in result.warnings), result.warnings)

    def test_a_schema_1_record_without_a_path_is_unknown_never_skipped(self) -> None:
        images = self._setup_job(2)
        styled_id = min(images)
        self._seed_ledger(
            [
                {
                    "image_id": styled_id,
                    "style": "hero",
                    "applied_at": "2026-01-01T00:00:00Z",
                    "history_end_before": 3,
                }
            ]
        )
        Path(images[styled_id] + ".xmp").write_text(_sidecar_xml(history_end=5))

        _b, result = self._apply_set(images, apply=_apply_all_ok, job_slug="hero-job")

        row = next(r for r in result.per_image if r["image_id"] == styled_id)
        self.assertEqual(row["status"], "unknown")
        self.assertIn("predates path-keying", row["error"])

    def test_every_written_ledger_entry_carries_its_path(self) -> None:
        images = self._setup_job(3)
        self._apply_set(images, apply=_apply_all_ok, job_slug="hero-job")
        entries = self._ledger_entries()
        self.assertEqual(len(entries), 3)
        for entry in entries:
            self.assertEqual(entry["path"], images[entry["image_id"]])


class HistoryEndMissingIsItsOwnStateTest(StyleSetTest):
    """Carried finding 5 (task 8): a well-formed sidecar with NO `history_end`
    attribute used to be folded into `current == before`, which reported
    "sidecar not rewritten since the apply; commit history" -- advice that
    cannot help, because there is no history_end to move. It is still
    "unknown"; it now says what is true."""

    def test_a_sidecar_with_no_history_end_says_so(self) -> None:
        images = self._setup_job(2)
        styled_id = min(images)
        self._seed_one(styled_id, history_end_before=3)
        no_end = _sidecar_xml(history_end=5).replace('darktable:history_end="5"', "")
        Path(images[styled_id] + ".xmp").write_text(no_end)

        _b, result = self._apply_set(images, apply=_apply_all_ok, job_slug="hero-job")

        row = next(r for r in result.per_image if r["image_id"] == styled_id)
        self.assertEqual(row["status"], "unknown")
        self.assertIn("carries no history_end attribute at all", row["error"])
        self.assertNotIn("rewound", row["error"])


if __name__ == "__main__":
    unittest.main()
