"""E12 D1 / decision row 407 call 2: the cull's sharpness gate must be
relative to the job's own distribution, not the fixed absolute
`cull.sharpness_floor` (0.20) tuned on the synthetic fixture. Real ORF/ARW
renders in the E12 live run measured 0.003-0.04 -- every singleton frame in
the job sat below the old floor, so nothing was ever proposed a pick on its
own merits.
"""

from __future__ import annotations

import unittest

from scouter.config import Config
from scouter.core import cull


def _singleton_frames(
    sharpness_values: list[float], *, failed_indices: set[int] = frozenset()
) -> list[dict]:
    """One frame per value, each 10 minutes apart so `_group_bursts` never
    joins any two of them -- every frame is judged by the singleton branch,
    the one E12 D1 is about. `failed_indices` (0-based, into
    `sharpness_values`) marks a frame as `stats_error` -- its `sharpness`
    then IS the neutral stand-in `propose()` would have set (0.0), never a
    real measurement."""
    frames = []
    for i, sharp in enumerate(sharpness_values, start=1):
        minute = i * 10
        failed = (i - 1) in failed_indices
        frame = {
            "seq": i,
            "path": f"/tmp/e12/frame-{i}.orf",
            "image_id": i,
            "captured_at": f"2026-01-01 {minute // 60:02d}:{minute % 60:02d}:00",
            "subsec": 0,
            "camera": "cam",
            "sharpness": 0.0 if failed else sharp,
            "clipped_high_pct": 0.0,
            "luminance_mean": 100.0,
            "rating_now": 0,
        }
        if failed:
            frame["stats_error"] = {"code": "error", "message": "render failed"}
        frames.append(frame)
    return frames


class PercentileHelperTest(unittest.TestCase):
    def test_percentile_matches_known_values(self) -> None:
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        self.assertEqual(cull._percentile(values, 0.0), 1.0)
        self.assertEqual(cull._percentile(values, 1.0), 5.0)
        self.assertEqual(cull._percentile(values, 0.5), 3.0)

    def test_percentile_of_empty_is_zero(self) -> None:
        self.assertEqual(cull._percentile([], 0.5), 0.0)

    def test_percentile_of_one_value_is_that_value(self) -> None:
        self.assertEqual(cull._percentile([0.02], 0.4), 0.02)


class JobRelativeThresholdTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = Config()

    def test_config_default_keeps_top_60_percent(self) -> None:
        self.assertEqual(self.cfg.cull.sharpness_keep_top_pct, 0.6)

    def test_e12_shape_does_not_reject_everything(self) -> None:
        """The exact defect: 39 real frames all measuring 0.003-0.04 against
        a fixed 0.20 floor meant EVERY singleton stayed at `keep_rating`.
        With a job-relative threshold, roughly the sharpest 60% must clear
        it -- some frames must be picked."""
        # 10 frames evenly spread across the E12-reported range.
        values = [0.003 + i * (0.04 - 0.003) / 9 for i in range(10)]
        frames = _singleton_frames(values)
        proposals, _groups, threshold = cull._build_proposals(frames, self.cfg)

        # The old absolute floor (0.20) would have failed every one of these.
        self.assertLess(threshold, 0.20)

        picks = [p for p in proposals if p["rating_proposed"] == self.cfg.cull.pick_rating]
        keeps = [p for p in proposals if p["rating_proposed"] == self.cfg.cull.keep_rating]
        self.assertGreater(len(picks), 0, "a job-relative gate must pick SOME frames")
        self.assertGreater(len(keeps), 0, "and still hold back the softer ones")
        # keep_top_pct=0.6 of 10 frames -> 6 picks, 4 keeps (values evenly
        # spaced, no ties at the exact cut).
        self.assertEqual(len(picks), 6)
        self.assertEqual(len(keeps), 4)

    def test_uniform_distribution_picks_everyone(self) -> None:
        """Every frame equally sharp: the percentile cut equals every value,
        so `sharpness >= threshold` holds for all of them -- correct, they
        really are all equally in the running."""
        frames = _singleton_frames([0.02] * 8)
        proposals, _groups, threshold = cull._build_proposals(frames, self.cfg)
        self.assertEqual(threshold, 0.02)
        for p in proposals:
            self.assertEqual(p["rating_proposed"], self.cfg.cull.pick_rating)

    def test_single_frame_job_is_always_picked(self) -> None:
        frames = _singleton_frames([0.015])
        proposals, _groups, threshold = cull._build_proposals(frames, self.cfg)
        self.assertEqual(threshold, 0.015)
        self.assertEqual(proposals[0]["rating_proposed"], self.cfg.cull.pick_rating)

    def test_absolute_floor_is_a_diagnostic_only(self) -> None:
        """A frame below the job-relative threshold but ABOVE the old
        absolute floor must still be held back -- the floor gates nothing
        any more, it is reported for reference only."""
        cfg = Config()
        frames = _singleton_frames([0.5, 0.5, 0.5, 0.5, 0.5, 0.25])
        # cfg.cull.sharpness_floor (0.20) is well under every one of these,
        # so the OLD logic would have picked all six.
        self.assertLess(cfg.cull.sharpness_floor, 0.25)
        proposals, _groups, threshold = cull._build_proposals(frames, cfg)
        by_seq = {p["seq"]: p for p in proposals}
        # keep_top_pct=0.6 of 6 -> the softest 2-3 (0.25 among them) must be
        # held back even though 0.25 clears the diagnostic floor.
        self.assertEqual(by_seq[6]["rating_proposed"], cfg.cull.keep_rating)
        self.assertIn(
            "diagnostic",
            " ".join(by_seq[6]["reasons"]),
            "the reason must say the absolute floor was not what gated it",
        )

    def test_floor_never_appears_without_being_labelled_diagnostic(self) -> None:
        """The absolute floor may still be printed for reference, but never
        as the thing that gated the rating -- every mention of it must carry
        the word "diagnostic" right there in the same reason."""
        frames = _singleton_frames([0.003, 0.01, 0.02, 0.03, 0.04])
        proposals, _groups, _threshold = cull._build_proposals(frames, self.cfg)
        for p in proposals:
            for reason in p["reasons"]:
                if "floor" in reason:
                    self.assertIn("diagnostic", reason)


class StatsErrorExclusionTest(unittest.TestCase):
    """Review round 1 blocker: `stats_error` frames carry the neutral
    stand-in `sharpness = 0.0`, never a real measurement. Left in the
    percentile, they either (with SOME real frames present) drag the
    threshold down so far every real frame clears it, or (with enough of
    them) collapse it to exactly 0.0 -- which a stats_error frame's own 0.0
    sharpness then also clears, proposing pick_rating on numbers Scouter
    never actually measured."""

    def setUp(self) -> None:
        self.cfg = Config()

    def test_half_failed_does_not_inflate_or_corrupt_the_threshold(self) -> None:
        # 6 real, evenly spaced across the E12-reported range; 4 failed
        # (their own passed-in value is irrelevant -- _singleton_frames
        # overrides it to the 0.0 stand-in).
        real_values = [0.003 + i * (0.04 - 0.003) / 5 for i in range(6)]
        sharpness_values = [0.9, 0.9, 0.9, 0.9] + real_values
        frames = _singleton_frames(sharpness_values, failed_indices={0, 1, 2, 3})

        proposals, _groups, threshold = cull._build_proposals(frames, self.cfg)

        # The threshold must come from the 6 real values ONLY: the same
        # number `test_e12_shape_does_not_reject_everything`-style math
        # gives for a 6-value job at the default 60% keep.
        self.assertAlmostEqual(threshold, real_values[2])

        by_seq = {p["seq"]: p for p in proposals}
        failed_seqs = {1, 2, 3, 4}  # 1-based seq for failed_indices {0,1,2,3}
        real_seqs = set(range(5, 11))

        for seq in failed_seqs:
            self.assertEqual(
                by_seq[seq]["rating_proposed"],
                self.cfg.cull.keep_rating,
                f"a stats_error frame (seq {seq}) must never be proposed a pick",
            )
        picks = {
            seq for seq in real_seqs if by_seq[seq]["rating_proposed"] == self.cfg.cull.pick_rating
        }
        # The real 60%-keep-top math on 6 evenly spaced values: the top 4
        # clear a 0.4-quantile threshold exactly at the 3rd value.
        self.assertEqual(len(picks), 4, by_seq)

    def test_all_failed_never_proposes_a_single_pick(self) -> None:
        frames = _singleton_frames([0.9] * 10, failed_indices=set(range(10)))
        proposals, _groups, threshold = cull._build_proposals(frames, self.cfg)
        self.assertEqual(threshold, 0.0, "no real frame left to derive a percentile from")
        for p in proposals:
            self.assertEqual(
                p["rating_proposed"],
                self.cfg.cull.keep_rating,
                "a threshold of 0.0 must not let a 0.0 stand-in clear it",
            )


class SharpnessSpreadWarningTest(unittest.TestCase):
    """Review round 1, should-fix: the percentile gate cannot detect a BAD
    job -- a uniformly soft/blurry job still proposes its top 60% a pick,
    and a uniformly sharp job still holds back its bottom 40%, neither ever
    rejected. `_sharpness_spread_warning` flags both by their tiny p10-p90
    spread."""

    def setUp(self) -> None:
        self.cfg = Config()

    def test_uniformly_soft_job_warns(self) -> None:
        frames = _singleton_frames([0.021, 0.019, 0.020, 0.0205, 0.0195, 0.02])
        warning = cull._sharpness_spread_warning(frames, self.cfg)
        self.assertIsNotNone(warning)
        self.assertIn("spread is small", warning)

    def test_uniformly_sharp_job_warns_too(self) -> None:
        frames = _singleton_frames([0.5, 0.49, 0.51, 0.505, 0.495, 0.5])
        warning = cull._sharpness_spread_warning(frames, self.cfg)
        self.assertIsNotNone(warning)

    def test_all_zero_job_warns(self) -> None:
        frames = _singleton_frames([0.0] * 6)
        warning = cull._sharpness_spread_warning(frames, self.cfg)
        self.assertIsNotNone(warning)
        self.assertIn("nothing to distinguish", warning)

    def test_e12_shaped_job_does_not_warn(self) -> None:
        """The real E12 distribution (0.003-0.04, an order of magnitude
        spread) must NOT trip this -- it is a legitimately wide relative
        spread even though the absolute numbers are tiny."""
        values = [0.003 + i * (0.04 - 0.003) / 9 for i in range(10)]
        frames = _singleton_frames(values)
        warning = cull._sharpness_spread_warning(frames, self.cfg)
        self.assertIsNone(warning)

    def test_failed_and_no_time_frames_are_excluded_from_the_spread_too(self) -> None:
        """Consistent with the threshold itself (the blocker fix): a
        stats_error stand-in or a no-time frame must not corrupt the spread
        computation either."""
        real = _singleton_frames([0.003 + i * (0.04 - 0.003) / 9 for i in range(10)])
        failed = _singleton_frames([0.9] * 3, failed_indices={0, 1, 2})
        no_time = [dict(f, captured_at=None) for f in _singleton_frames([0.9])]
        warning = cull._sharpness_spread_warning(real + failed + no_time, self.cfg)
        self.assertIsNone(warning, "the real E12-shaped spread must still be seen")

    def test_fewer_than_two_real_values_is_silent(self) -> None:
        self.assertIsNone(cull._sharpness_spread_warning(_singleton_frames([0.5]), self.cfg))
        self.assertIsNone(cull._sharpness_spread_warning([], self.cfg))


if __name__ == "__main__":
    unittest.main()
