"""LIVE (controller-run, darktable-cli): the one-time corpus survey (spec 4.2).

    .venv/bin/python -m tests.gauntlet.corpus_survey [--runs-root DIR]

Renders a 512 px default-pipeline PPM of a COPY of every EditingTrial raw
(each in its own throwaway configdir, via scout.render), reads EXIF from the
copy, deletes the copy, and writes <runs-root>/corpus.json with the roles."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from gauntlet import metrics
from gauntlet.raster import parse_ppm
from tests.gauntlet import corpus


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs-root", default=str(corpus.SCRATCH))
    args = ap.parse_args(argv)
    root = Path(args.runs_root).expanduser()
    from tests.gauntlet.adapter import isolate_process

    isolate_process(root / "_process")
    from scouter.core import exif, scout

    work = root / "survey-work"

    def render_stats(copy: Path) -> dict:
        out = work / "render" / (copy.name + ".ppm")
        out.parent.mkdir(parents=True, exist_ok=True)
        scout.render(str(copy), out, 512, "ppm")
        r = parse_ppm(out.read_bytes())
        s = metrics.region_stats(r, metrics.build_masks(r), ("frame",))["frame"]
        out.unlink()
        return {"L_mean": s["L"], "spread": s["spread"], "p99": s["p99L"],
                "clip_high": s["clip_high"], "rg": s["rg"], "bg": s["bg"]}

    def exif_model_iso(copy: Path) -> tuple[str, int]:
        tags = exif.read_tags(copy)
        return (getattr(tags, "model", "") or "", int(getattr(tags, "iso", 0) or 0))

    before = corpus.sha256_originals()
    frames = corpus.survey(render_stats, exif_model_iso, work)
    doc = corpus.Corpus.write(root / "corpus.json", frames)
    assert corpus.sha256_originals() == before, "an original changed during the survey"
    print(f"{root / 'corpus.json'}: {len(frames)} frames; roles "
          f"{ {b: sorted(r) for b, r in doc['roles'].items()} }")
    return 0


if __name__ == "__main__":
    sys.exit(main())
