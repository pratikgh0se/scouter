"""Build a fake 30-frame card from the four EditingTrial ORFs.

The copies are byte-identical apart from their EXIF capture times, which are
patched IN PLACE: DateTime (0x0132) and DateTimeOriginal (0x9003) are
fixed-length ASCII ("2026:06:28 15:49:28", 20 bytes with the NUL), so
`core.exif.datetime_offsets` locates them and an equal-length string is written
over them. No re-encoding, no EXIF library, and the reader that finds the
offsets is the reader under test.

Layout: 6 bursts of 5 frames, 1 s apart inside a burst, 45 s between bursts --
so `cull.propose` has real bursts to find with the 2.0 s default gap.

LIMITATION, stated rather than hidden: copies of the same ORF have identical
pixels, so `sharpness` ties inside a burst. The test asserts ONE pick per burst,
not which frame wins, and asserts the result reports tie_broken_by == "seq".

Runnable by hand:

    python3 -m tests.make_synthetic_job --out ~/Pictures/ScouterSynthetic

No `--seed`: the builder is fully deterministic (the source files are fixed,
the timestamps are arithmetic, and the names are `SYNTH<n>.ORF` in order), and
`tests/test_make_synthetic_job.py` asserts that building twice produces
identical bytes -- a non-host test, so the claim is checked on every ordinary
suite run.

This builder is NOT what the end-to-end host test uses.
`tests/test_host_phase2.py::Phase2EndToEndTest` stages 30 REAL ORFs from one
shoot instead (`_stage_real_card`), because copies of four files have
identical pixels: every sharpness inside a burst would tie and the cull stage
could only ever assert the tie-break. This stays a unit fixture because the
burst layout it produces is exact and controllable in a way a real shoot is
not.
"""

from __future__ import annotations

import argparse
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from scouter.core import exif

SOURCES = ("P6281348.ORF", "P6281361.ORF", "P6281368.ORF", "P6281373.ORF")
BURSTS, PER_BURST, GAP_S, BURST_GAP_S = 6, 5, 1, 45

#: The first frame's capture time. The real files all carry
#: "2026:06:28 15:49:28"; the synthetic card starts at a round second so the
#: burst arithmetic below is readable in a failure message.
START = datetime(2026, 6, 28, 15, 0, 0)

#: EXIF's own spelling, and exactly 19 characters + the NUL the real files
#: carry -- the patch is only safe because it is the same length.
_EXIF_FMT = "%Y:%m:%d %H:%M:%S"


def _capture_times() -> list[datetime]:
    """One timestamp per frame: `PER_BURST` frames `GAP_S` apart, then a
    `BURST_GAP_S` jump to the next burst.

    The jump has to be strictly greater than `cull.burst_gap_s` (2.0 s by
    default) for `cull.propose` to see six bursts rather than one long one;
    45 s is far enough that a config with a wider gap still groups them the
    way this fixture intends.
    """
    times: list[datetime] = []
    t = START
    for burst in range(BURSTS):
        if burst:
            t = t + timedelta(seconds=BURST_GAP_S)
        for frame in range(PER_BURST):
            times.append(t if frame == 0 else t + timedelta(seconds=GAP_S * frame))
        t = times[-1]
    return times


def _patch_capture_time(path: Path, when: datetime) -> list[int]:
    """Overwrite every DateTime/DateTimeOriginal value in `path` in place.

    Returns the byte offsets patched, so a caller (and a failing test) can
    see that the write actually found the tags rather than silently doing
    nothing. Refuses rather than truncating if a tag is not the expected
    20-byte fixed-length ASCII: a shorter write would leave stale bytes
    behind and a longer one would overrun the next value.
    """
    stamp = when.strftime(_EXIF_FMT).encode("ascii")
    offsets = exif.datetime_offsets(path)
    if not offsets:
        raise ValueError(f"{path}: no DateTime/DateTimeOriginal tags found to patch")
    patched: list[int] = []
    with path.open("r+b") as fh:
        for at, length in offsets:
            if length != len(stamp) + 1:
                raise ValueError(
                    f"{path}: DateTime value at {at} is {length} bytes, expected "
                    f"{len(stamp) + 1} ('{_EXIF_FMT}' + NUL); refusing to patch a "
                    "tag whose length this builder does not understand"
                )
            fh.seek(at)
            fh.write(stamp + b"\x00")
            patched.append(at)
    return patched


def build(out: Path, *, source_dir: Path = Path.home() / "Pictures/EditingTrial") -> dict:
    """Write `<out>/card/DCIM/100OLYMP/*.ORF` and return a manifest of the plan.

    `out` is created if absent. An EXISTING `card/` directory is removed
    first, so the builder is idempotent and a re-run cannot leave a 31st
    frame from a previous, differently-shaped run behind.
    """
    sources = [source_dir / name for name in SOURCES]
    missing = [str(p) for p in sources if not p.is_file()]
    if missing:
        raise FileNotFoundError(
            f"the synthetic job needs the EditingTrial ORFs; missing: {missing}"
        )

    card = Path(out) / "card"
    if card.exists():
        shutil.rmtree(card)
    dcim = card / "DCIM" / "100OLYMP"
    dcim.mkdir(parents=True)

    times = _capture_times()
    total = BURSTS * PER_BURST
    assert len(times) == total, (len(times), total)

    frames: list[dict] = []
    for index, when in enumerate(times):
        source = sources[index % len(sources)]
        # `SYNTH<n>.ORF`, zero-padded, in capture order: the camera-filename
        # sort `exif.capture_key` falls back to therefore agrees with the
        # capture-time sort, so a tie inside a burst breaks the way the
        # docstring's limitation says it does.
        name = f"SYNTH{index + 1:04d}.ORF"
        dest = dcim / name
        shutil.copyfile(source, dest)
        _patch_capture_time(dest, when)
        frames.append(
            {
                "name": name,
                "source": source.name,
                "burst": index // PER_BURST + 1,
                "captured_at": when.strftime("%Y-%m-%d %H:%M:%S"),
                "bytes": dest.stat().st_size,
            }
        )

    return {
        "version": 1,
        "card": str(card),
        "source_dir": str(source_dir),
        "sources": list(SOURCES),
        "bursts": BURSTS,
        "per_burst": PER_BURST,
        "gap_s": GAP_S,
        "burst_gap_s": BURST_GAP_S,
        "frames": frames,
        "totals": {"frames": total, "bytes": sum(f["bytes"] for f in frames)},
        "limitation": (
            "every frame is a byte copy of one of four real ORFs, so pixels (and "
            "therefore sharpness) repeat: a burst's pick is decided by the seq "
            "tie-break, not by which frame is genuinely sharpest"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, help="directory to build <out>/card/ under")
    parser.add_argument(
        "--source-dir",
        default=str(Path.home() / "Pictures/EditingTrial"),
        help="where the four real ORFs live",
    )
    args = parser.parse_args(argv)
    manifest = build(Path(args.out).expanduser(), source_dir=Path(args.source_dir).expanduser())
    import json

    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
