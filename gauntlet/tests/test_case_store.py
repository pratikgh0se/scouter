import ast
import json
import os
import sys
from pathlib import Path

import pytest

from gauntlet.adapter import load_adapter
from gauntlet.case import Case, Check, Expect, Step, case_id, from_jsonl, to_jsonl
from gauntlet.store import RunDir, atomic_write_text, new_stamp

ENGINE = Path(__file__).resolve().parents[1]


def _case(i: str = "a") -> Case:
    return Case(
        id=case_id("cov", "exposure.exposure", "sony_a7", f"set_{i}"),
        layer="coverage", tier="P0", mode="tool",
        steps=[Step("darkroom_set", {"module": "exposure", "param": "exposure", "value": 1.0},
                    budget_s=3.5, write=True)],
        expect=Expect("edit", checks=[Check("M1", target={"key": "exposure.exposure",
                                                          "value": 1.0, "unit": "EV"})]),
        body="sony_a7", frame_role="hero",
    )


def test_case_id_is_slugged_and_stable():
    assert case_id("scn", "r0001", "Olympus", "tool") == "scn-r0001-olympus-tool"
    assert case_id("cov", "exposure.exposure", "sony a7", "set hi") == \
        "cov-exposure.exposure-sony_a7-set_hi"


def test_jsonl_round_trip_is_byte_identical():
    cases = [_case("a"), _case("b")]
    text = to_jsonl(cases)
    again = to_jsonl(from_jsonl(text))
    assert text == again
    assert json.loads(text.splitlines()[0])["schema"] == "gauntlet-case/1"


def test_bad_tier_mode_kind_refused():
    with pytest.raises(ValueError):
        Case(id="x", layer="l", tier="P9", mode="tool", steps=[], expect=Expect("edit"))
    with pytest.raises(ValueError):
        Case(id="x", layer="l", tier="P0", mode="gui", steps=[], expect=Expect("edit"))
    with pytest.raises(ValueError):
        Case(id="x", layer="l", tier="P0", mode="tool", steps=[], expect=Expect("maybe"))


def test_budget_cap_counts_followups():
    c = _case()
    c.followups = [{"sentence": "more", "steps": [{"tool": "darkroom_say",
                                                   "args": {"phrase": "brighter"},
                                                   "budget_s": 8.0}]}]
    assert c.budget_cap_s() == pytest.approx(3.5 + 30 + 8 + 30)


def test_repro_defaults_to_the_engine_cli():
    assert _case().repro.startswith("python -m gauntlet run --case cov-")


def test_rundir_atomic_results_and_resume(tmp_path):
    run = RunDir(tmp_path, "20260926t000000z").ensure()
    run.write_cases([_case("a")])
    assert run.read_cases()[0].id == _case("a").id
    run.write_result("c1", {"id": "c1", "score": "pass"})
    assert run.done_ids() == {"c1"}
    assert run.read_result("c1")["score"] == "pass"
    assert not list(run.cases_dir.glob("*.tmp"))


def test_atomic_write_leaves_old_file_on_failure(tmp_path, monkeypatch):
    p = tmp_path / "f.json"
    atomic_write_text(p, "old")

    def boom(*a, **k):
        raise OSError("disk")

    monkeypatch.setattr("os.replace", boom)
    with pytest.raises(OSError):
        atomic_write_text(p, "new")
    assert p.read_text() == "old"
    assert not list(tmp_path.glob("*.tmp"))


def test_new_stamp_format():
    s = new_stamp()
    assert len(s) == 16 and s[8] == "t" and s.endswith("z")


def test_load_adapter_needs_module_colon_factory():
    with pytest.raises(ValueError):
        load_adapter("gauntlet.case")


def test_engine_is_stdlib_only_and_imports_no_product():
    """Rows 436/437: the engine is reusable by Capsule Corp. Only stdlib and
    `gauntlet.*` imports are allowed anywhere under gauntlet/ (tests included,
    pytest excepted)."""
    allowed = set(sys.stdlib_module_names) | {"gauntlet", "pytest"}
    bad = []
    for path in ENGINE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for n in names:
                if n.split(".")[0] not in allowed:
                    bad.append(f"{path.relative_to(ENGINE)}: {n}")
    assert bad == []


def test_retain_deletes_all_but_keep_globs_and_reports_bytes_freed(tmp_path):
    run = RunDir(tmp_path, "s").ensure()
    d = run.case_workdir("c1")
    (d / "nested").mkdir(parents=True)
    (d / "sidecar.xmp").write_bytes(b"a" * 10)
    (d / "nested" / "big.raw").write_bytes(b"b" * 2048)
    freed = run.retain("c1", keep=("*.xmp",))
    assert freed == 2048
    assert (d / "sidecar.xmp").exists()
    assert not (d / "nested" / "big.raw").exists()
    assert not (d / "nested").exists()  # emptied by the prune


def test_retain_score_only_empties_the_whole_workdir(tmp_path):
    run = RunDir(tmp_path, "s").ensure()
    d = run.case_workdir("c1")
    d.mkdir(parents=True)
    (d / "import.raw").write_bytes(b"x" * 4096)
    freed = run.retain("c1")
    assert freed == 4096
    assert list(d.iterdir()) == []


def test_retain_missing_workdir_is_a_noop(tmp_path):
    run = RunDir(tmp_path, "s").ensure()
    assert run.retain("never-ran") == 0


def test_retain_never_touches_the_verdict_json_or_the_log(tmp_path):
    run = RunDir(tmp_path, "s").ensure()
    run.write_result("c1", {"id": "c1", "score": "pass"})
    run.log_md.write_text("- **c1**: verified\n")
    d = run.case_workdir("c1")
    (d / "junk").mkdir(parents=True)
    (d / "junk" / "f.bin").write_bytes(b"x" * 100)
    run.retain("c1")
    assert run.read_result("c1") == {"id": "c1", "score": "pass"}
    assert "c1" in run.log_md.read_text()


def test_retain_never_touches_paths_outside_the_case_workdir(tmp_path):
    run = RunDir(tmp_path, "s").ensure()
    outside = tmp_path / "outside.txt"
    outside.write_text("keep me")
    sibling = run.case_workdir("c2")
    sibling.mkdir(parents=True)
    (sibling / "keep.bin").write_bytes(b"y" * 10)
    d = run.case_workdir("c1")
    d.mkdir(parents=True)
    (d / "junk.bin").write_bytes(b"z" * 10)
    run.retain("c1")
    assert outside.read_text() == "keep me"
    assert (sibling / "keep.bin").exists()


def test_du_reports_bytes_per_layer_from_the_workdir(tmp_path):
    run = RunDir(tmp_path, "s").ensure()
    c = _case("a")
    run.write_cases([c])
    d = run.case_workdir(c.id)
    d.mkdir(parents=True)
    (d / "f.bin").write_bytes(b"x" * 500)
    assert run.du() == {"coverage": 500}


def test_du_includes_verdict_json_and_preview_bytes(tmp_path):
    run = RunDir(tmp_path, "s").ensure()
    c = _case("a")
    run.write_cases([c])
    run.write_result(c.id, {"id": c.id, "score": "pass"})
    (run.previews_dir / f"{c.id}.jpg").write_bytes(b"p" * 40)
    want = run.case_path(c.id).stat().st_size + 40
    assert run.du() == {"coverage": want}


def test_du_before_generation_is_empty(tmp_path):
    run = RunDir(tmp_path, "s")
    assert run.du() == {}


# -- fix round 1 (Opus review, t_90e75c20) ----------------------------------

UNSAFE_IDS = ("", ".", "..", "/etc", "a/b", os.sep.join(("a", "b")))


@pytest.mark.parametrize("bad", UNSAFE_IDS)
def test_case_workdir_rejects_unsafe_ids(tmp_path, bad):
    """I1: an empty, `.`/`..`, absolute-looking or separator-carrying id must
    never resolve to a workdir outside `<run>/work`."""
    run = RunDir(tmp_path, "s").ensure()
    with pytest.raises(ValueError):
        run.case_workdir(bad)


@pytest.mark.parametrize("bad", UNSAFE_IDS)
def test_retain_rejects_unsafe_ids_and_deletes_nothing(tmp_path, bad):
    """I1: retain() must refuse before it ever walks anything -- probed with
    the exact `".."`/absolute shapes the review found deleted the run's own
    evidence (cases/, log.md, previews)."""
    run = RunDir(tmp_path, "s").ensure()
    run.write_result("c1", {"id": "c1", "score": "pass"})
    run.log_md.write_text("- **c1**: verified\n")
    kept = run.case_workdir("c1")
    kept.mkdir(parents=True)
    (kept / "keep.bin").write_bytes(b"x" * 10)

    with pytest.raises(ValueError):
        run.retain(bad)

    assert run.read_result("c1") == {"id": "c1", "score": "pass"}
    assert "c1" in run.log_md.read_text()
    assert (kept / "keep.bin").exists()


def test_retain_unlinks_a_dangling_symlink_without_following_it(tmp_path):
    """I2: a dangling symlink used to raise FileNotFoundError from stat()."""
    run = RunDir(tmp_path, "s").ensure()
    d = run.case_workdir("c1")
    d.mkdir(parents=True)
    (d / "ghost").symlink_to(tmp_path / "does-not-exist")
    run.retain("c1")
    assert not (d / "ghost").exists()
    assert list(d.iterdir()) == []


def test_retain_unlinks_a_symlink_to_a_directory_without_touching_its_target(tmp_path):
    """I2: a symlink to a directory used to raise NotADirectoryError from
    rmdir(); the target itself must never be entered or deleted."""
    run = RunDir(tmp_path, "s").ensure()
    target = tmp_path / "real-dir"
    target.mkdir()
    (target / "untouched.bin").write_bytes(b"x" * 10)
    d = run.case_workdir("c1")
    d.mkdir(parents=True)
    (d / "link").symlink_to(target)
    run.retain("c1")
    assert not (d / "link").exists()
    assert (target / "untouched.bin").exists()  # the real content is never touched


def test_retain_tolerates_a_path_vanishing_mid_walk(tmp_path, monkeypatch):
    """I2: a race with the case's own still-finishing writes (unlink/rmdir
    racing a FileNotFoundError) must not blow up the whole retain() call."""
    run = RunDir(tmp_path, "s").ensure()
    d = run.case_workdir("c1")
    d.mkdir(parents=True)
    (d / "f.bin").write_bytes(b"x" * 10)
    orig_unlink = Path.unlink

    def flaky_unlink(self, *a, **k):
        if self.name == "f.bin":
            raise FileNotFoundError("vanished")
        return orig_unlink(self, *a, **k)
    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    assert run.retain("c1") == 0  # freed nothing for the path that vanished, but never raised


def test_du_counts_a_real_scouter_preview_directory(tmp_path):
    """I4: Scouter's own previews are `previews/<id>/before.png` + `after.png`
    directories, never a bare `<id>.*` file -- `du()` must count both."""
    run = RunDir(tmp_path, "s").ensure()
    c = _case("a")
    run.write_cases([c])
    pd = run.previews_dir / c.id
    pd.mkdir(parents=True)
    (pd / "before.png").write_bytes(b"b" * 30)
    (pd / "after.png").write_bytes(b"a" * 20)
    assert run.du() == {"coverage": 50}


def test_budget_cap_honours_a_case_declared_cap():
    """(final fix C1): a case whose work is not expressed as steps (an agent
    conversation) declares its own cap in meta["cap_s"]; the engine uses the
    larger of that and the step sum, never the bare 30 s fallback."""
    c = Case(id="x", layer="l", tier="P0", mode="agent", steps=[], expect=Expect("refuse"),
             meta={"cap_s": 1260.0})
    assert c.budget_cap_s() == pytest.approx(1260.0)
    c2 = _case()
    c2.meta["cap_s"] = 1.0  # a smaller declared cap never shrinks the step sum
    assert c2.budget_cap_s() == pytest.approx(3.5 + 30)
