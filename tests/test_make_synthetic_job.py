"""`tests/make_synthetic_job.py`: the synthetic 30-frame card builder.

Brief step 1's own assertion lives here: **building twice produces identical
bytes.** That is why the builder takes no `--seed` -- the sources are fixed,
the timestamps are arithmetic, and the names are assigned in capture order --
and a claim like that is worth nothing unless something checks it.

Fix round 1, blocker 3: before this file the builder had NO caller at all. It
was dead code carrying a docstring promising a determinism property, next to a
host test whose comment claimed the property was "asserted below" when nothing
asserted it anywhere. The ruling was: land the assertion or delete the
builder. This lands it, and it is a NON-host test -- it needs the four real
ORFs to copy but never darktable, so it runs in the ordinary suite.

The end-to-end host run uses 30 REAL ORFs instead (see
`tests/test_host_phase2.py`): copies of four files have identical pixels, so
every sharpness inside a burst ties and the cull stage could only ever assert
the tie-break. The builder stays because the burst LAYOUT it produces is
exact and controllable in a way a real shoot is not -- which is what makes it
a unit fixture.
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
import unittest.mock
from datetime import datetime
from pathlib import Path

from scouter.core import exif
from tests import make_synthetic_job

SOURCE_DIR = Path.home() / "Pictures/EditingTrial"
_MISSING = [name for name in make_synthetic_job.SOURCES if not (SOURCE_DIR / name).is_file()]
SKIP_REASON = f"the four source ORFs are not present ({', '.join(_MISSING)})" if _MISSING else ""


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_digests(card: Path) -> dict[str, str]:
    return {p.name: _digest(p) for p in sorted(card.rglob("*.ORF"))}


@unittest.skipUnless(not _MISSING, SKIP_REASON)
class BuildSyntheticJobTest(unittest.TestCase):
    """No darktable, no network, nothing outside its own temp directory. The
    four real ORFs are only READ."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="scouter-synth-test-")
        self.tmp = Path(self._tmpdir.name)
        self.addCleanup(self._tmpdir.cleanup)

    def _build(self, out: Path) -> dict:
        return make_synthetic_job.build(out, source_dir=SOURCE_DIR)

    # -- brief step 1's determinism assertion -------------------------------

    def test_building_twice_produces_identical_bytes(self) -> None:
        """The property the missing `--seed` rests on.

        Two builds into two different directories, compared file by file by
        sha256 -- not just "same number of files". A patched capture time is
        written straight into the copy's EXIF, so any non-determinism in the
        timestamp arithmetic, the source rotation or the naming shows up as a
        digest mismatch here.
        """
        first = self._build(self.tmp / "a")
        second = self._build(self.tmp / "b")

        digests_a = _tree_digests(Path(first["card"]))
        digests_b = _tree_digests(Path(second["card"]))

        self.assertEqual(len(digests_a), 30)
        self.assertEqual(
            digests_a,
            digests_b,
            "two builds must be byte-identical: the builder takes no --seed "
            "precisely because it claims to be deterministic",
        )
        # The returned manifests must agree too, not just the bytes on disk
        # (`card` differs by construction -- it is the output directory).
        self.assertEqual(
            {k: v for k, v in first.items() if k != "card"},
            {k: v for k, v in second.items() if k != "card"},
        )

    def test_rebuilding_in_place_is_also_identical(self) -> None:
        """Idempotence, which is a different claim from determinism: the
        builder removes an existing `card/` first, so a re-run into the SAME
        directory cannot leave a 31st frame from an earlier, differently
        shaped run behind."""
        out = self._tmp_out()
        first = _tree_digests(Path(self._build(out)["card"]))
        stray = Path(self._build(out)["card"]) / "LEFTOVER.ORF"
        stray.write_bytes(b"not a raw")
        second = _tree_digests(Path(self._build(out)["card"]))

        self.assertEqual(first, second)
        self.assertFalse(stray.exists(), "an existing card/ must be removed, not merged into")

    def _tmp_out(self) -> Path:
        return self.tmp / "inplace"

    # -- the layout the fixture exists to provide ---------------------------

    def test_the_layout_is_six_bursts_of_five(self) -> None:
        manifest = self._build(self.tmp / "layout")
        self.assertEqual(manifest["totals"]["frames"], 30)
        self.assertEqual(manifest["bursts"], 6)
        self.assertEqual(manifest["per_burst"], 5)

        by_burst: dict[int, list[dict]] = {}
        for frame in manifest["frames"]:
            by_burst.setdefault(frame["burst"], []).append(frame)
        self.assertEqual(sorted(by_burst), [1, 2, 3, 4, 5, 6])
        for burst, frames in by_burst.items():
            self.assertEqual(len(frames), 5, f"burst {burst} has {len(frames)} frames")

    def test_the_gaps_are_what_cull_propose_needs(self) -> None:
        """1 s inside a burst, 45 s between -- so the 2.0 s default
        `cull.burst_gap_s` splits them into exactly six groups. Read back
        from the FILES' patched EXIF, not from the manifest, so this also
        proves the in-place patch landed."""
        card = Path(self._build(self.tmp / "gaps")["card"])
        stamps = []
        for path in sorted(card.rglob("*.ORF")):
            tags = exif.read_tags(path)
            self.assertIsNotNone(tags.captured_at, f"{path.name} lost its capture time")
            stamps.append(datetime.strptime(tags.captured_at, "%Y-%m-%d %H:%M:%S"))

        gaps = [(b - a).total_seconds() for a, b in zip(stamps, stamps[1:])]
        inside = [g for i, g in enumerate(gaps) if (i + 1) % 5 != 0]
        between = [g for i, g in enumerate(gaps) if (i + 1) % 5 == 0]
        self.assertEqual(set(inside), {1.0}, inside)
        self.assertEqual(set(between), {45.0}, between)
        # And the property that actually matters to cull.propose.
        self.assertTrue(all(g <= 2.0 for g in inside))
        self.assertTrue(all(g > 2.0 for g in between))

    def test_the_copies_differ_from_their_sources_only_in_the_timestamps(self) -> None:
        """The docstring's central claim: byte-identical apart from the two
        patched EXIF values. Checked by patching the copy's timestamps BACK
        to the source's and comparing digests."""
        card = Path(self._build(self.tmp / "bytes")["card"])
        frame = sorted(card.rglob("*.ORF"))[0]
        source = SOURCE_DIR / make_synthetic_job.SOURCES[0]
        source_tags = exif.read_tags(source)
        assert source_tags.captured_at is not None

        self.assertNotEqual(_digest(frame), _digest(source), "the timestamps were patched")
        make_synthetic_job._patch_capture_time(
            frame, datetime.strptime(source_tags.captured_at, "%Y-%m-%d %H:%M:%S")
        )
        self.assertEqual(
            _digest(frame),
            _digest(source),
            "restoring the capture time must restore the file exactly: the "
            "builder must not re-encode anything",
        )

    def test_every_frame_is_a_copy_of_the_source_the_manifest_names(self) -> None:
        """Not merely "of one of the four": each frame is checked against the
        source the manifest says it came from, by undoing that source's own
        capture time. The four sources have four DIFFERENT capture times, so
        restoring the wrong one would not reproduce the bytes."""
        manifest = self._build(self.tmp / "sources")
        card = Path(manifest["card"])
        source_times = {}
        for name in make_synthetic_job.SOURCES:
            tags = exif.read_tags(SOURCE_DIR / name)
            assert tags.captured_at is not None
            source_times[name] = datetime.strptime(tags.captured_at, "%Y-%m-%d %H:%M:%S")
        self.assertEqual(
            len(set(source_times.values())),
            len(make_synthetic_job.SOURCES),
            "this test is only meaningful while the four sources have four "
            "distinct capture times",
        )

        for frame in manifest["frames"]:
            # `card` is `<out>/card`; the frames sit under DCIM/100OLYMP.
            path = card / "DCIM" / "100OLYMP" / frame["name"]
            source = SOURCE_DIR / frame["source"]
            make_synthetic_job._patch_capture_time(path, source_times[frame["source"]])
            self.assertEqual(
                _digest(path),
                _digest(source),
                f"{frame['name']} is not a byte copy of {frame['source']}",
            )

    # -- refusals -----------------------------------------------------------

    def test_a_missing_source_is_refused_by_name(self) -> None:
        empty = self.tmp / "no-sources"
        empty.mkdir()
        with self.assertRaises(FileNotFoundError) as ctx:
            make_synthetic_job.build(self.tmp / "out", source_dir=empty)
        self.assertIn(make_synthetic_job.SOURCES[0], str(ctx.exception))

    def test_a_tag_of_an_unexpected_length_is_refused_not_truncated(self) -> None:
        """A shorter write would leave stale bytes behind and a longer one
        would overrun the next value, so the builder refuses a tag whose
        length it does not understand rather than corrupting the file."""
        card = Path(self._build(self.tmp / "len")["card"])
        frame = sorted(card.rglob("*.ORF"))[0]
        before = frame.read_bytes()

        offsets = exif.datetime_offsets(frame)
        self.assertTrue(offsets)
        with unittest.mock.patch.object(
            make_synthetic_job.exif,
            "datetime_offsets",
            return_value=[(offsets[0][0], 7)],
        ):
            with self.assertRaises(ValueError) as ctx:
                make_synthetic_job._patch_capture_time(frame, datetime(2026, 1, 1, 0, 0, 0))
        self.assertIn("refusing to patch", str(ctx.exception))
        self.assertEqual(frame.read_bytes(), before, "a refused patch must write nothing")

    def test_no_tags_at_all_is_refused(self) -> None:
        card = Path(self._build(self.tmp / "notags")["card"])
        frame = sorted(card.rglob("*.ORF"))[0]
        with unittest.mock.patch.object(
            make_synthetic_job.exif, "datetime_offsets", return_value=[]
        ):
            with self.assertRaises(ValueError) as ctx:
                make_synthetic_job._patch_capture_time(frame, datetime(2026, 1, 1, 0, 0, 0))
        self.assertIn("no DateTime", str(ctx.exception))

    # -- the __main__ guard the brief asked for -----------------------------

    def test_it_is_runnable_by_hand(self) -> None:
        out = self.tmp / "cli"
        code = make_synthetic_job.main(["--out", str(out), "--source-dir", str(SOURCE_DIR)])
        self.assertEqual(code, 0)
        self.assertEqual(len(list((out / "card").rglob("*.ORF"))), 30)


@unittest.skipUnless(not _MISSING, SKIP_REASON)
class BuilderTouchesNothingElseTest(unittest.TestCase):
    """The sources are READ-ONLY as far as this builder is concerned."""

    def test_the_source_orfs_are_never_modified(self) -> None:
        before = {
            name: (_digest(SOURCE_DIR / name), (SOURCE_DIR / name).stat().st_mtime_ns)
            for name in make_synthetic_job.SOURCES
        }
        with tempfile.TemporaryDirectory(prefix="scouter-synth-ro-") as d:
            make_synthetic_job.build(Path(d), source_dir=SOURCE_DIR)
        after = {
            name: (_digest(SOURCE_DIR / name), (SOURCE_DIR / name).stat().st_mtime_ns)
            for name in make_synthetic_job.SOURCES
        }
        self.assertEqual(before, after, "the EditingTrial originals must never be written")

    def test_it_writes_only_under_the_given_out_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scouter-synth-scope-") as d:
            out = Path(d) / "nested" / "deeper"
            manifest = make_synthetic_job.build(out, source_dir=SOURCE_DIR)
            written = sorted(p for p in Path(d).rglob("*") if p.is_file())
            self.assertTrue(written)
            for path in written:
                self.assertTrue(
                    str(path).startswith(str(out)),
                    f"{path} is outside the requested out directory",
                )
            self.assertEqual(Path(manifest["card"]).parent, out)


if __name__ == "__main__":
    unittest.main()
