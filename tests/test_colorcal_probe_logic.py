"""Offline tests (no darktable) for the route A probe's pure logic and the
isolated-library launch argv in tests/host_support.py."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests import host_support as hs
from tests.colorcal_probe_logic import Evidence, noise_floor, route_verdict, unique_run_dir


def good(**over) -> Evidence:
    base = dict(
        pinned=True,
        slots_ok=True,
        sentinel_live=True,
        noop_ok=True,
        illuminants=(2, 2, 2),
        preset=4,
        contaminated=False,
        base_rb=1.0,
        warm_rb=1.05,
        cool_rb=0.95,
        b_landed=True,
        clamped="",
        floor=0.005,
    )
    base.update(over)
    return Evidence(**base)


class RouteVerdictTest(unittest.TestCase):
    def test_all_oracles_agree_promotes(self) -> None:
        self.assertEqual(route_verdict(good())[0], "PROMOTE")

    def test_inverted_rendered_sign_flips(self) -> None:
        self.assertEqual(route_verdict(good(warm_rb=0.95, cool_rb=1.05))[0], "FLIP_SIGN")

    def test_inside_the_noise_floor_does_not_promote(self) -> None:
        self.assertEqual(route_verdict(good(warm_rb=1.0005, cool_rb=0.9995))[0], "DO_NOT_PROMOTE")

    def test_each_sidecar_or_side_effect_failure_blocks_and_names_itself(self) -> None:
        cases = {
            "pinned": dict(pinned=False),
            "slot": dict(slots_ok=False),
            "sentinel": dict(sentinel_live=False),
            "noop": dict(noop_ok=False),
            "illuminant": dict(illuminants=(2, 7)),
            "preset": dict(preset=0),
            "contaminat": dict(contaminated=True),
            "oracle B write": dict(b_landed=False),
        }
        for word, over in cases.items():
            with self.subTest(word):
                verdict, reason = route_verdict(good(**over))
                self.assertEqual(verdict, "DO_NOT_PROMOTE")
                self.assertIn(word, reason)

    def test_a_clamp_is_not_read_as_a_dead_slot_and_names_both_numbers(self) -> None:
        verdict, reason = route_verdict(
            good(slots_ok=False, clamped="cooler: requested 2000 K, landed 4000 K")
        )
        self.assertEqual(verdict, "DO_NOT_PROMOTE")
        self.assertIn("CLAMPED", reason)
        self.assertIn("requested 2000 K, landed 4000 K", reason)
        self.assertNotIn("slot 36 oracle", reason)

    def test_a_wider_measured_floor_blocks_a_small_move(self) -> None:
        self.assertEqual(
            route_verdict(good(warm_rb=1.05, cool_rb=0.95, floor=0.08))[0], "DO_NOT_PROMOTE"
        )

    def test_a_sidecar_failure_outranks_a_flipped_sign(self) -> None:
        self.assertEqual(
            route_verdict(good(slots_ok=False, warm_rb=0.9, cool_rb=1.1))[0], "DO_NOT_PROMOTE"
        )


class RunDirTest(unittest.TestCase):
    def test_unique_and_absent_and_refuses_a_reused_name(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            first = unique_run_dir(root, pid=7, stamp="t1")
            self.assertEqual(first, root / "t1-7")
            self.assertFalse(first.exists())
            first.mkdir()
            with self.assertRaises(FileExistsError):
                unique_run_dir(root, pid=7, stamp="t1")


class LaunchArgvTest(unittest.TestCase):
    def test_default_launch_is_unchanged(self) -> None:
        self.assertEqual(hs.launch_argv(Path("/x/a.ORF")), [str(hs.DARKTABLE), "/x/a.ORF"])

    def test_isolated_launch_carries_library_and_refuses_the_real_one(self) -> None:
        lib = hs.SCRATCH_DIR / "colorcal-probe" / "r" / "library.db"
        argv = hs.launch_argv(Path("/x/a.ORF"), lib)
        self.assertEqual(argv[1:3], ["--library", str(lib.resolve())])
        with self.assertRaises(ValueError):
            hs.launch_argv(Path("/x/a.ORF"), hs.REAL_LIBRARY)


class NoiseFloorTest(unittest.TestCase):
    def test_floor_is_three_times_the_repeat_difference_when_that_is_larger(self) -> None:
        self.assertAlmostEqual(noise_floor(1.0, 1.0, 1.02), 0.06)

    def test_floor_never_drops_below_half_a_percent_of_base(self) -> None:
        self.assertAlmostEqual(noise_floor(2.0, 2.0, 2.0), 0.01)
