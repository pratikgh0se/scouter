"""`cull.propose` / `cull.apply`: bursts, picks, reasons with numbers, and a
gated, verified, batched apply (task 6)."""

from __future__ import annotations

import json
import os
import time
import unittest
from pathlib import Path
from unittest import mock

from scouter.cli import tables
from scouter.config import Config
from scouter.core import cull, dtbridge, xmp
from scouter.errors import UsageError
from tests.scouter_fakes import FakeBridge, TempCache


def _frame(
    seq: int,
    *,
    captured_at: str | None = "2026-01-01 10:00:00",
    subsec: int = 0,
    camera: str = "cam",
    sharpness: float = 0.5,
    clipped: float = 0.0,
    luminance: float = 100.0,
    rating_now: int = 0,
    image_id: int | None = None,
    path: str | None = None,
) -> dict:
    return {
        "seq": seq,
        "path": path or f"/tmp/scouter-cull-test/frame-{seq}.orf",
        "image_id": image_id if image_id is not None else seq,
        "captured_at": captured_at,
        "subsec": subsec,
        "camera": camera,
        "sharpness": sharpness,
        "clipped_high_pct": clipped,
        "luminance_mean": luminance,
        "rating_now": rating_now,
    }


class BurstGroupingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = Config()

    def test_burst_boundary_at_the_gap(self) -> None:
        one_second_apart = [
            _frame(1, captured_at="2026-01-01 10:00:00"),
            _frame(2, captured_at="2026-01-01 10:00:01"),
        ]
        groups = cull._group_bursts(one_second_apart, gap_s=2.0)
        self.assertEqual([g["frames"] for g in groups], [[1, 2]], "1.0s apart must be one burst")

        two_and_a_half_apart = [
            _frame(1, captured_at="2026-01-01 10:00:00", subsec=0),
            _frame(2, captured_at="2026-01-01 10:00:02", subsec=50),
        ]
        groups = cull._group_bursts(two_and_a_half_apart, gap_s=2.0)
        self.assertEqual([g["frames"] for g in groups], [[1], [2]], "2.5s apart must be two bursts")

        exactly_two_apart = [
            _frame(1, captured_at="2026-01-01 10:00:00"),
            _frame(2, captured_at="2026-01-01 10:00:02"),
        ]
        groups = cull._group_bursts(exactly_two_apart, gap_s=2.0)
        self.assertEqual(
            [g["frames"] for g in groups], [[1, 2]], "exactly 2.0s apart must join (<=)"
        )

    def test_one_pick_per_burst(self) -> None:
        # 6 bursts of 5, 1s apart inside a burst, 45s between bursts -- the
        # design's own 30-frame fixture shape (spec section 19.1).
        frames = []
        seq = 1
        for b in range(6):
            base = b * 45
            for i in range(5):
                t = base + i
                minute, sec = divmod(t, 60)
                captured_at = f"2026-01-01 10:{minute:02d}:{sec:02d}"
                sharpness = 0.35 if i == 2 else 0.30  # frame index 2 is the pick
                frames.append(_frame(seq, captured_at=captured_at, sharpness=sharpness))
                seq += 1

        proposals, groups = cull._build_proposals(frames, self.cfg)
        bursts = [g for g in groups if g["burst_id"] != "no-time"]
        self.assertEqual(len(bursts), 6)
        for g in bursts:
            self.assertEqual(len(g["frames"]), 5)

        picks = [p for p in proposals if p["rating_proposed"] == self.cfg.cull.pick_rating]
        self.assertEqual(len(picks), 6, "exactly one pick per burst")

    def test_tie_breaks_by_seq_and_says_so(self) -> None:
        frames = [
            _frame(1, captured_at="2026-01-01 10:00:00", sharpness=0.4),
            _frame(2, captured_at="2026-01-01 10:00:01", sharpness=0.4),
            _frame(3, captured_at="2026-01-01 10:00:02", sharpness=0.2),
        ]
        proposals, groups = cull._build_proposals(frames, self.cfg)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["tie_broken_by"], "seq")

        picks = [p for p in proposals if p["rating_proposed"] == self.cfg.cull.pick_rating]
        self.assertEqual(len(picks), 1, "a tie still produces exactly one pick")
        self.assertEqual(picks[0]["seq"], 1, "the lower seq wins a sharpness tie")

    def test_no_time_frames_are_untouched(self) -> None:
        frames = [
            _frame(1, captured_at=None, rating_now=1, sharpness=0.9),
            _frame(2, captured_at=None, rating_now=0, sharpness=0.1),
        ]
        proposals, groups = cull._build_proposals(frames, self.cfg)
        no_time_groups = [g for g in groups if g["burst_id"] == "no-time"]
        self.assertEqual(len(no_time_groups), 1)
        self.assertEqual(no_time_groups[0]["frames"], [1, 2])

        for p in proposals:
            self.assertEqual(p["rating_proposed"], p["rating_now"], "never changed")
            self.assertEqual(len(p["reasons"]), 1)
            self.assertIn("no capture time", p["reasons"][0])

    def test_reasons_carry_numbers(self) -> None:
        frames = [
            _frame(1, captured_at="2026-01-01 10:00:00", sharpness=0.5),
            _frame(2, captured_at="2026-01-01 10:00:01", sharpness=0.3),
            _frame(3, captured_at=None, rating_now=2),
            _frame(
                4,
                captured_at="2026-01-01 11:00:00",
                sharpness=0.01,
                clipped=9.0,
                luminance=5.0,
            ),
            _frame(5, captured_at="2026-01-01 12:00:00", sharpness=0.9, rating_now=3),
        ]
        proposals, _groups = cull._build_proposals(frames, self.cfg)
        self.assertTrue(proposals)
        for p in proposals:
            self.assertTrue(p["reasons"])
            for reason in p["reasons"]:
                self.assertTrue(
                    any(ch.isdigit() for ch in reason),
                    f"reason has no number: {reason!r}",
                )


class ProposeOneTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = Config()

    def test_never_proposes_zero(self) -> None:
        # Dark, clipped and soft: every signal says "don't pick this" -- and
        # the floor under a proposed rating is still `keep_rating`, never 0.
        frame = _frame(1, sharpness=0.05, clipped=10.0, luminance=10.0, rating_now=0)
        burst = {"burst_id": "b1", "members": [frame]}
        rating, reasons = cull._propose_one(frame, burst, best_seq=1, cfg=self.cfg)
        self.assertEqual(rating, self.cfg.cull.keep_rating)
        self.assertGreater(rating, 0)
        self.assertEqual(len(reasons), 3, reasons)

    def test_existing_higher_rating_kept(self) -> None:
        frame = _frame(1, sharpness=0.5, rating_now=3)
        burst = {"burst_id": "b1", "members": [frame]}
        rating, reasons = cull._propose_one(frame, burst, best_seq=1, cfg=self.cfg)
        self.assertEqual(rating, 3)
        self.assertTrue(any("keeping your existing rating 3" in r for r in reasons))


class TablesTest(unittest.TestCase):
    def test_pretty_table_has_one_row_per_frame(self) -> None:
        payload = {
            "proposals": [
                {
                    "seq": i,
                    "burst_id": "b1",
                    "rating_now": 0,
                    "rating_proposed": 2,
                    "sharpness": 0.5,
                    "reasons": [f"reason {i}"],
                }
                for i in range(1, 6)
            ],
            "counts": {"pick": 1, "keep": 4, "unchanged": 0, "no_time": 0},
        }
        rendered = tables.render("cull.propose", payload)
        self.assertIsNotNone(rendered)
        lines = rendered.splitlines()
        data_lines = lines[2:7]  # header, separator, then one row per frame
        self.assertEqual(len(data_lines), 5)
        for i, line in enumerate(data_lines, start=1):
            self.assertTrue(line.startswith(str(i)), line)

    def test_unregistered_verb_falls_back_to_none(self) -> None:
        self.assertIsNone(tables.render("outliers.list", {}))


def _rate_response(params: dict) -> dict:
    """A generic `image.rate` stand-in: rates every id, honestly."""
    return {
        "missed": [],
        "per_image": [
            {"image_id": img_id, "rating_before": 0, "rating_after": params["rating"]}
            for img_id in params["image_ids"]
        ],
    }


class CullApplyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = TempCache()
        self.tmp = self.cache.start()
        self._config_patch = mock.patch.object(cull.config, "load", return_value=Config())
        self._config_patch.start()
        self._saved_timeout = cull.VERIFY_TIMEOUT_S
        self._saved_interval = cull.VERIFY_INTERVAL_S
        cull.VERIFY_TIMEOUT_S = 0.03
        cull.VERIFY_INTERVAL_S = 0.01

    def tearDown(self) -> None:
        cull.VERIFY_TIMEOUT_S = self._saved_timeout
        cull.VERIFY_INTERVAL_S = self._saved_interval
        self._config_patch.stop()
        dtbridge.set_bridge(None)
        self.cache.stop()

    def _write_proposal(self, frames: list[dict], xmp_mtimes: dict | None = None) -> Path:
        document = {
            "version": 1,
            "job_slug": "smoke",
            "frames": frames,
            "groups": [],
            "xmp_mtimes": xmp_mtimes or {},
        }
        path = self.tmp / "cull-test.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def test_apply_refuses_stale_proposal(self) -> None:
        raw_a = str(self.tmp / "a.orf")
        raw_b = str(self.tmp / "b.orf")
        sidecar_a = xmp.sidecar_path(raw_a)
        sidecar_b = xmp.sidecar_path(raw_b)
        sidecar_a.write_text('<x xmp:Rating="0"/>', encoding="utf-8")
        sidecar_b.write_text('<x xmp:Rating="0"/>', encoding="utf-8")
        frames = [
            {"path": raw_a, "image_id": 1, "seq": 1, "rating_now": 0, "rating_proposed": 2},
            {"path": raw_b, "image_id": 2, "seq": 2, "rating_now": 0, "rating_proposed": 2},
        ]
        xmp_mtimes = {
            raw_a: sidecar_a.stat().st_mtime_ns,
            raw_b: sidecar_b.stat().st_mtime_ns,
        }
        proposal_path = self._write_proposal(frames, xmp_mtimes)

        future = time.time() + 5
        os.utime(sidecar_a, (future, future))

        with self.assertRaises(UsageError) as ctx:
            cull.apply(proposal=str(proposal_path), confirm=True)
        self.assertEqual(ctx.exception.code, "usage")
        self.assertIn(raw_a, ctx.exception.detail["stale_frames"])
        self.assertNotIn(raw_b, ctx.exception.detail["stale_frames"])

    def test_apply_batches_at_fifty(self) -> None:
        frames = [
            {
                "path": str(self.tmp / f"f{i}.orf"),
                "image_id": i,
                "seq": i,
                "rating_now": 0,
                "rating_proposed": 2,
            }
            for i in range(130)
        ]
        proposal_path = self._write_proposal(frames)
        bridge = FakeBridge({"image.rate": _rate_response})
        dtbridge.set_bridge(bridge)

        result = cull.apply(proposal=str(proposal_path), confirm=True)

        self.assertEqual(bridge.count("image.rate"), 3, "130 ids at 50/call is 3 calls")
        self.assertEqual(result.applied, 130)

    def test_apply_reports_unverified_separately(self) -> None:
        # No sidecar is ever written for these frames, so the poll can never
        # observe the rating -- exactly the "landed in the DB, sidecar not
        # caught up yet" case.
        frames = [
            {
                "path": str(self.tmp / f"u{i}.orf"),
                "image_id": i,
                "seq": i,
                "rating_now": 0,
                "rating_proposed": 2,
            }
            for i in range(3)
        ]
        proposal_path = self._write_proposal(frames)
        dtbridge.set_bridge(FakeBridge({"image.rate": _rate_response}))

        result = cull.apply(proposal=str(proposal_path), confirm=True)

        self.assertEqual(result.outcome, "ok")
        self.assertEqual(result.verified, 0)
        self.assertEqual(result.unverified, 3)
        self.assertTrue(any("sidecar" in w for w in result.warnings), result.warnings)

    def test_apply_skips_unchanged_frames(self) -> None:
        frames = [
            {
                "path": str(self.tmp / "c0.orf"),
                "image_id": 0,
                "seq": 0,
                "rating_now": 2,
                "rating_proposed": 2,
            },
            {
                "path": str(self.tmp / "c1.orf"),
                "image_id": 1,
                "seq": 1,
                "rating_now": 0,
                "rating_proposed": 2,
            },
            {
                "path": str(self.tmp / "c2.orf"),
                "image_id": 2,
                "seq": 2,
                "rating_now": 1,
                "rating_proposed": 1,
            },
            {
                "path": str(self.tmp / "c3.orf"),
                "image_id": 3,
                "seq": 3,
                "rating_now": 0,
                "rating_proposed": 2,
            },
        ]
        proposal_path = self._write_proposal(frames)
        seen_ids: list[int] = []

        def rate(params: dict) -> dict:
            seen_ids.extend(params["image_ids"])
            return _rate_response(params)

        dtbridge.set_bridge(FakeBridge({"image.rate": rate}))

        result = cull.apply(proposal=str(proposal_path), confirm=True)

        self.assertEqual(result.unchanged, 2)
        self.assertEqual(sorted(seen_ids), [1, 3], "only the changed frames go to image.rate")
        self.assertEqual(result.applied, 2)

    def test_apply_dry_run_reports_would_change(self) -> None:
        frames = [
            {
                "path": str(self.tmp / "d0.orf"),
                "image_id": 0,
                "seq": 0,
                "rating_now": 0,
                "rating_proposed": 2,
            },
            {
                "path": str(self.tmp / "d1.orf"),
                "image_id": 1,
                "seq": 1,
                "rating_now": 2,
                "rating_proposed": 2,
            },
        ]
        proposal_path = self._write_proposal(frames)
        bridge = FakeBridge({"image.rate": _rate_response})
        dtbridge.set_bridge(bridge)

        result = cull.apply(proposal=str(proposal_path), dry_run=True)

        self.assertEqual(result.outcome, "dry_run")
        self.assertEqual(result.would_change, 1)
        self.assertEqual(bridge.count("image.rate"), 0, "a dry run never writes")


if __name__ == "__main__":
    unittest.main()
