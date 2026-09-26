"""Product close, item 4 (row 427 item 5 prep): `job run` chains a job's batch
steps -- cull propose/apply, hero preset apply-set with exclusions, outliers
list, export release -- reporting counts per step and stopping honestly at
the first step that refuses or comes back partial.

Every step function is a fake here: no darktable, no renders, no config.
"""

from __future__ import annotations

import unittest
from unittest import mock

from scouter import registry
from scouter.cli.main import build_parser
from scouter.core import jobrun
from scouter.errors import UsageError
from scouter.types import (
    CullApplyResult,
    CullResult,
    OutliersResult,
    ReleaseResult,
    StyleSetResult,
)


class _Fakes:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.set_outcome = "ok"
        self.release_error: Exception | None = None
        self.propose_error: Exception | None = None

    def propose(self, **kw):
        self.calls.append(("cull.propose", kw))
        if self.propose_error:
            raise self.propose_error
        return CullResult(proposal_path="/j/.scouter/cull-1.json", job_slug=kw["job_slug"],
                          counts={"pick": 3, "keep": 20, "unchanged": 7},
                          stats={"sharpness_threshold": 0.012, "failed": 0},
                          warnings=["spread is small"])

    def apply(self, **kw):
        self.calls.append(("cull.apply", kw))
        if kw.get("dry_run") or not kw.get("confirm"):
            return CullApplyResult(outcome="dry_run", would_change=23, reason="dry")
        return CullApplyResult(applied=23, unchanged=7, verified=22, unverified=1,
                               stale=["/j/raw/x.ORF"])

    def apply_job(self, name, **kw):
        self.calls.append(("preset.apply_job", {"name": name, **kw}))
        if kw.get("dry_run") or not kw.get("confirm"):
            return StyleSetResult(outcome="dry_run", name=name, requested=29, reason="dry")
        return StyleSetResult(outcome=self.set_outcome, name=name, requested=29,
                              applied=29 if self.set_outcome == "ok" else 20,
                              failed=0 if self.set_outcome == "ok" else 9)

    def outliers(self, reference, **kw):
        self.calls.append(("outliers.list", {"reference": reference, **kw}))
        return OutliersResult(checked=29, within_tolerance=25, wb_test="ratio",
                              outliers=[{"path": f"/j/raw/{i}.ORF"} for i in range(4)])

    def release(self, job_slug, preset, **kw):
        self.calls.append(("export.release", {"job_slug": job_slug, "preset": preset, **kw}))
        if self.release_error:
            raise self.release_error
        if kw.get("dry_run") or not kw.get("confirm"):
            return ReleaseResult(outcome="dry_run", job_slug=job_slug, preset=preset,
                                 selected=3, of=30, reason="dry")
        return ReleaseResult(job_slug=job_slug, preset=preset, selected=3, of=30,
                             manifest_path="/j/.scouter/manifest.json")

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


class JobRunTest(unittest.TestCase):
    def setUp(self) -> None:
        self.f = _Fakes()
        patches = [
            mock.patch("scouter.core.cull.propose", self.f.propose),
            mock.patch("scouter.core.cull.apply", self.f.apply),
            mock.patch("scouter.core.presets.apply_job", self.f.apply_job),
            mock.patch("scouter.core.style.outliers", self.f.outliers),
            mock.patch("scouter.core.export.release", self.f.release),
            mock.patch("scouter.core.jobrun._SETTLE_AFTER_STYLE_S", 0.0),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_job(self, **kw):
        return jobrun.run.__wrapped__(job_slug="harbour", job="job-test", **kw)

    def rows(self, result) -> dict:
        return {row["step"]: row for row in result.steps}

    def test_without_confirm_it_is_a_dry_run_that_writes_nothing(self) -> None:
        r = self.run_job(preset="hero-harbour", hero="/j/raw/h.ORF")
        self.assertEqual(r.outcome, "dry_run")
        self.assertTrue(r.reason)
        self.assertEqual([p["step"] for p in r.plan], ["cull", "preset", "outliers", "export"])
        self.assertNotIn("cull.propose", self.f.names(), "no renders in a dry run")
        for name, kw in self.f.calls:
            self.assertFalse(kw.get("confirm"), f"{name} was confirmed in a dry run")
        by = {p["step"]: p for p in r.plan}
        self.assertEqual(by["preset"]["counts"]["requested"], 29)
        self.assertEqual(by["export"]["counts"]["selected"], 3)

    def test_full_chain_in_order_with_counts(self) -> None:
        r = self.run_job(preset="hero-harbour", hero="/j/raw/h.ORF", exclude=["/j/raw/b.ORF"],
                         confirm=True)
        self.assertEqual(r.outcome, "ok")
        self.assertEqual(self.f.names(), ["cull.propose", "cull.apply", "preset.apply_job",
                                          "outliers.list", "export.release"])
        calls = dict(self.f.calls)
        self.assertEqual(calls["cull.apply"]["proposal"], "/j/.scouter/cull-1.json")
        self.assertEqual(calls["preset.apply_job"]["exclude"], ["/j/raw/b.ORF", "/j/raw/h.ORF"])
        self.assertEqual(calls["outliers.list"]["reference"], "/j/raw/h.ORF")
        self.assertEqual(calls["export.release"]["preset"], "client-proof")
        rows = self.rows(r)
        self.assertEqual(rows["cull.propose"]["counts"]["pick"], 3)
        self.assertEqual(rows["cull.propose"]["counts"]["sharpness_threshold"], 0.012)
        self.assertEqual(rows["cull.apply"]["counts"]["stale"], 1)
        self.assertEqual(rows["preset"]["counts"], {"requested": 29, "applied": 29, "failed": 0,
                                                    "missed": 0})
        self.assertEqual(rows["outliers"]["counts"]["outliers"], 4)
        self.assertEqual(rows["export"]["counts"]["selected"], 3)
        self.assertEqual(r.completed, 5)
        self.assertIsNone(r.stopped_at)
        self.assertIn("spread is small", " ".join(r.warnings))

    def test_missing_preset_and_hero_skip_their_steps(self) -> None:
        r = self.run_job(confirm=True)
        self.assertEqual(r.outcome, "ok")
        rows = self.rows(r)
        self.assertEqual(rows["preset"]["outcome"], "skipped")
        self.assertEqual(rows["outliers"]["outcome"], "skipped")
        self.assertNotIn("preset.apply_job", self.f.names())
        self.assertNotIn("outliers.list", self.f.names())

    def test_a_partial_step_stops_the_chain(self) -> None:
        self.f.set_outcome = "partial"
        r = self.run_job(preset="hero-harbour", hero="/j/raw/h.ORF", confirm=True)
        self.assertEqual(r.outcome, "partial")
        self.assertEqual(r.stopped_at, "preset")
        self.assertNotIn("outliers.list", self.f.names())
        self.assertNotIn("export.release", self.f.names())
        self.assertIn("preset", r.reason)

    def test_a_refusing_later_step_is_reported_not_raised(self) -> None:
        self.f.release_error = UsageError("empty selection: no frame rated >= 2")
        r = self.run_job(confirm=True)
        self.assertEqual(r.outcome, "partial")
        self.assertEqual(r.stopped_at, "export")
        row = self.rows(r)["export"]
        self.assertEqual(row["outcome"], "error:usage")
        self.assertIn("empty selection", row["error"]["message"])

    def test_a_refusing_first_step_raises_its_own_error(self) -> None:
        self.f.propose_error = UsageError("no import.json")
        with self.assertRaises(UsageError):
            self.run_job(confirm=True)

    def test_steps_subset(self) -> None:
        r = self.run_job(steps=["export"], export_preset="client-final", confirm=True)
        self.assertEqual(self.f.names(), ["export.release"])
        self.assertEqual(r.completed, 1)

    def test_explicit_preset_step_without_a_preset_is_usage(self) -> None:
        with self.assertRaises(UsageError):
            self.run_job(steps=["preset"], confirm=True)
        with self.assertRaises(UsageError):
            self.run_job(steps=["outliers"], confirm=True)

    def test_unknown_step_is_usage(self) -> None:
        with self.assertRaises(UsageError):
            self.run_job(steps=["retouch"], confirm=True)


class JobRunRegistryTest(unittest.TestCase):
    def test_registered_gated_and_on_the_cli(self) -> None:
        verb = registry.load_verbs()["job.run"]
        self.assertTrue(verb.gated)
        self.assertTrue(verb.writes)
        ns = build_parser().parse_args(
            ["job", "run", "--job-slug", "harbour", "--preset", "hero", "--hero", "/h.ORF",
             "--exclude", "/a.ORF,/b.ORF", "--steps", "cull,export", "--dry-run"])
        self.assertEqual(ns._verb, "job.run")
        self.assertEqual(ns.exclude, ["/a.ORF", "/b.ORF"])
        self.assertEqual(ns.steps, ["cull", "export"])


if __name__ == "__main__":
    unittest.main()
