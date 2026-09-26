import os
from pathlib import Path
from types import SimpleNamespace

from tests.gauntlet import faults


def _session(tmp_path):
    hero = tmp_path / "frames" / "hero" / "H.ORF"
    hero.parent.mkdir(parents=True)
    hero.write_bytes(b"raw")
    Path(str(hero) + ".xmp").write_text("<x:xmpmeta>" + "a" * 100 + "</x:xmpmeta>")
    staged = SimpleNamespace(frames={"hero": hero}, library=tmp_path / "library.db",
                             env={"SCOUTER_CONFIG": str(tmp_path / "config" / "config.json")},
                             vars={}, job_frames=[])
    return SimpleNamespace(staged=staged, root=tmp_path, body="olympus")


def test_file_faults_arm_and_disarm(tmp_path):
    s = _session(tmp_path)
    v = faults.arm(s, "chmod_sidecar_readonly")
    side = Path(v["_restore_mode"])
    assert oct(side.stat().st_mode & 0o777) == "0o444"
    faults.disarm(s, v)
    assert oct(side.stat().st_mode & 0o777) == "0o644"
    v = faults.arm(s, "torn_sidecar_copy")
    torn = Path(v["torn_copy"] + ".xmp").read_text()
    assert len(torn) < 70
    v = faults.arm(s, "none")
    assert not Path(v["missing_file"]).exists() and Path(v["unsupported_file"]).exists()
    v = faults.arm(s, "future_mtime")
    assert os.path.getmtime(str(s.staged.frames["hero"]) + ".xmp") > 2e9 - 1e9
    v = faults.arm(s, "modal_dialog")
    assert "no modal-dialog test hook" in v["_skip"]
