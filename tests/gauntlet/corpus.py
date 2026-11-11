"""The image corpus (spec 4): originals guard, read-only copies, the one-time
survey, frame roles, and bulk cards built by duplication.

EditingTrial originals are never opened, imported, edited or exported: they
are only READ to make 0644 copies under the scratch root."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

TRIAL = Path(os.environ.get("HTC_TRIAL", str(Path.home() / "Pictures/EditingTrial")))
SCRATCH = Path.home() / "Pictures" / "ScouterScratch" / "gauntlet"
RAW_EXT = {".orf", ".arw"}
MODEL_BODY = {"E-M10 Mark III": "olympus", "E-M10MarkIII": "olympus", "ILCE-7": "sony_a7",
              "NEX-5N": "sony_nex5n"}
#: The bench's frames block (editor_tasks.json) wins where it names a role.
BENCH_ROLES = {
    "olympus": {"dark": "P5240151.ORF", "bright": "P6281368.ORF", "blown": "P5240140.ORF",
                "hero": "P6281348.ORF"},
    "sony_a7": {"dark": "sony/DSC01425.ARW", "blown": "sony/DSC01431.ARW",
                "hero": "sony/DSC01426.ARW"},
}
ROLES = ("dark", "bright", "blown", "hero", "flat", "warm_cast", "cool_cast", "noisy", "neutral",
         "hero_bright")
FRAME_BYTES = 20 * 1024 * 1024

#: Cluster D / row 458 (2026-09-27): the olympus `hero` (P6281348.ORF) surveys
#: at L_mean 119.2 -- near the darkest frame in the corpus -- so the 10
#: touch-floor residual phrases (see tests/test_touch_floor.py
#: KNOWN_RESIDUAL) have too little headroom for a "touch" move to clear its
#: floor. `hero_bright` is a normally-exposed stand-in: P5240141.ORF, picked
#: from the embedded-JPEG-preview luminance survey as the frame closest to
#: mid-grey (L_mean 126.99 vs. mid-grey 128.0; runners-up P5240125.ORF at
#: 125.12 and P5240146.ORF at 131.17). It lives under a COPY outside
#: EditingTrial (SCRATCH/frames-bright), not among the surveyed TRIAL
#: originals, so it is injected as an absolute path rather than picked by
#: `pick_roles`'s stat comparisons.
EXTRA_ROLES: dict[str, dict[str, str]] = {
    "olympus": {"hero_bright": str(SCRATCH / "frames-bright" / "P5240141.ORF")},
}
#: sha256 of each EXTRA_ROLES copy, recorded at copy time so a test can catch
#: silent drift or corruption of the frames-bright copy.
EXTRA_ROLE_SHA256: dict[str, dict[str, str]] = {
    "olympus": {
        "hero_bright": "e96d46c74bd3984215ee4735be0907e2ec9731836cc242ca57e99b0acc624626",
    },
}


class OriginalPathRefused(ValueError):
    """A path under the EditingTrial originals was about to be used."""


def under_trial(path: str | Path) -> bool:
    real = os.path.realpath(os.path.expanduser(str(path)))
    root = os.path.realpath(str(TRIAL))
    return real == root or real.startswith(root + os.sep)


def guard(path: str | Path) -> str:
    if under_trial(path):
        raise OriginalPathRefused(f"refusing an EditingTrial original: {path}")
    return str(path)


def copy_readonly(src: Path, dst: Path) -> Path:
    """Read an original, write a writable 0644 copy (never the other way)."""
    guard(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    dst.chmod(0o644)
    return dst


def originals(root: Path | None = None) -> list[Path]:
    root = root or TRIAL
    return sorted(p for p in root.rglob("*") if p.suffix.lower() in RAW_EXT and p.is_file())


def sha256_originals(root: Path | None = None) -> dict[str, str]:
    out = {}
    base = root or TRIAL
    for p in originals(base):
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        out[str(p.relative_to(base))] = h.hexdigest()
    return out


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_extra_roles() -> dict[str, str]:
    """sha256 every EXTRA_ROLES copy against EXTRA_ROLE_SHA256; returns the
    mismatches (empty dict means every copy is byte-identical to what was
    recorded when it was made)."""
    mismatches = {}
    for body, roles in EXTRA_ROLES.items():
        for role, abs_path in roles.items():
            want = EXTRA_ROLE_SHA256.get(body, {}).get(role)
            p = Path(abs_path)
            if want is None or not p.exists():
                mismatches[f"{body}.{role}"] = "missing"
                continue
            got = sha256_file(p)
            if got != want:
                mismatches[f"{body}.{role}"] = f"want {want}, got {got}"
    return mismatches


def free_space_ok(path: Path, need_bytes: int) -> bool:
    path.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(path).free > need_bytes + 2 * 1024**3  # keep 2 GB spare


@dataclass
class Surveyed:
    rel: str
    body: str
    iso: int
    L_mean: float
    spread: float
    p99: float
    clip_high: float
    rg: float
    bg: float


def survey(render_stats, exif_model_iso, work: Path, root: Path | None = None) -> list[Surveyed]:
    """render_stats(copy_path) -> {"L_mean","spread","p99","clip_high","rg","bg"};
    exif_model_iso(copy_path) -> (model, iso). Runs on COPIES in `work`."""
    base = root or TRIAL
    out = []
    for src in originals(base):
        rel = str(src.relative_to(base))
        copy = copy_readonly(src, work / rel)
        model, iso = exif_model_iso(copy)
        body = MODEL_BODY.get(model.strip(), "unknown")
        s = render_stats(copy)
        out.append(Surveyed(rel, body, int(iso or 0), s["L_mean"], s["spread"], s["p99"],
                            s["clip_high"], s["rg"], s["bg"]))
        copy.unlink()
    return out


def pick_roles(frames: list[Surveyed]) -> dict[str, dict[str, str]]:
    roles: dict[str, dict[str, str]] = {}
    for body in sorted({f.body for f in frames} - {"unknown"}):
        fs = sorted((f for f in frames if f.body == body), key=lambda f: f.rel)
        med = sorted(f.L_mean for f in fs)[len(fs) // 2]
        pick = {
            "dark": min(fs, key=lambda f: f.L_mean),
            "bright": max((f for f in fs if f.clip_high < 0.01) or fs, key=lambda f: f.p99),
            "blown": max(fs, key=lambda f: f.clip_high),
            "hero": min(fs, key=lambda f: abs(f.L_mean - med)),
            "flat": min(fs, key=lambda f: f.spread),
            "warm_cast": max(fs, key=lambda f: f.rg),
            "cool_cast": min(fs, key=lambda f: f.rg),
            "noisy": max(fs, key=lambda f: f.iso),
            "neutral": min(fs, key=lambda f: abs(f.rg - 1) + abs(f.bg - 1)),
        }
        roles[body] = {r: f.rel for r, f in pick.items()}
        for r, rel in BENCH_ROLES.get(body, {}).items():
            if any(f.rel == rel for f in fs):
                roles[body][r] = rel
        for r, abs_path in EXTRA_ROLES.get(body, {}).items():
            roles[body][r] = abs_path
    # EXTRA_ROLES point at copies outside TRIAL, so they don't depend on the
    # survey finding any frames for that body (e.g. an empty/partial survey
    # in a test) -- make sure they're still present.
    for body, extra in EXTRA_ROLES.items():
        roles.setdefault(body, {}).update(extra)
    return roles


class Corpus:
    """corpus.json: {"frames": [Surveyed...], "roles": {body: {role: rel}}}."""

    def __init__(self, doc: dict, root: Path | None = None) -> None:
        self.doc = doc
        self.root = root or TRIAL

    @classmethod
    def load(cls, path: Path, root: Path | None = None) -> Corpus:
        """Backfills EXTRA_ROLES missing from a stale cached corpus.json so a
        `KeyError` never reaches a lookup; a cache that already names the
        role is left as written."""
        doc = json.loads(path.read_text(encoding="utf-8"))
        roles = doc.setdefault("roles", {})
        for body, extra in EXTRA_ROLES.items():
            roles.setdefault(body, {})
            for role, abs_path in extra.items():
                roles[body].setdefault(role, abs_path)
        return cls(doc, root)

    @staticmethod
    def write(path: Path, frames: list[Surveyed]) -> dict:
        doc = {"schema": "scouter-gauntlet-corpus/1", "frames": [asdict(f) for f in frames],
               "roles": pick_roles(frames)}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return doc

    def roles(self, body: str) -> dict[str, Path]:
        return {r: self.root / rel for r, rel in self.doc["roles"][body].items()}

    def job_sources(self, body: str, n: int) -> list[Path]:
        fs = sorted(f["rel"] for f in self.doc["frames"] if f["body"] == body)
        return [self.root / fs[i % len(fs)] for i in range(n)]


def build_card(corpus: Corpus, body: str, n: int, card_dir: Path) -> list[Path]:
    """`DUP####_<orig>` copies, round robin (spec 3.3). Refuses when the disk
    cannot hold them plus 2 GB."""
    if not free_space_ok(card_dir, n * FRAME_BYTES):
        raise OSError(f"not enough free disk for a {n}-frame card under {card_dir}")
    out = []
    for i, src in enumerate(corpus.job_sources(body, n)):
        out.append(copy_readonly(src, card_dir / "DCIM" / "100GAUNT" / f"DUP{i:04d}_{src.name}"))
    return out


def delete_card(card_root: Path) -> None:
    guard(card_root)
    shutil.rmtree(card_root, ignore_errors=True)
