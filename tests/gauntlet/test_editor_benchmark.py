"""Unit tests for the editor benchmark (decision row 406): task file, scorer,
assertions, resolvers and the result JSON contract, via a FAKE run.

Nothing here touches darktable, ~/.config, the vault or the network: the MCP
client, the darktable host and the XMP snapshotter are all fakes, and every
file written goes under pytest's tmp_path.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from tests.gauntlet.benchmark import run_editor_benchmark as rb

# -- the task file -----------------------------------------------------------


def test_task_file_is_valid():
    doc = rb.load_tasks()
    assert rb.validate_tasks(doc) == []


def test_task_file_covers_every_category_and_two_refusals():
    doc = rb.load_tasks()
    cats = {t["category"] for t in doc["tasks"]}
    assert cats == set(rb.CATEGORIES)
    unreachable = [t for t in doc["tasks"] if t["category"] == "unreachable"]
    assert len(unreachable) == 2 and all(t["plan"] == [] for t in unreachable)


def test_every_plan_placeholder_resolves_for_both_bodies():
    doc = rb.load_tasks()
    for body in ("olympus", "sony"):
        ctx = _ctx(body)
        for t in doc["tasks"]:
            rb.render(t["sentence"], ctx)
            for version in rb.PLAN_VERSIONS:
                e = rb.effective_task(t, version)
                rb.render(e["plan"], ctx)
                rb.render(e.get("context") or [], ctx)
                rb.render(e["assertions"], ctx)
        for s in doc["sessions"]:
            if s["id"] == "calib" and not doc["frames"][body]["wb_calibrate"]:
                continue
            rb.render(s.get("setup") or [], ctx)


def test_validator_catches_broken_scoring():
    doc = rb.load_tasks()
    doc["tasks"][0]["scoring"]["pass"] = ["nope"]
    assert any("unknown assertion" in e for e in rb.validate_tasks(doc))


def test_frames_named_exist_in_editing_trial_when_present():
    doc = rb.load_tasks()
    if not rb.EDITING_TRIAL.is_dir():
        pytest.skip("EditingTrial not on this machine")
    for body, spec in doc["frames"].items():
        for role in ("dark", "bright", "blown", "hero"):
            assert (rb.EDITING_TRIAL / spec[role]).is_file(), (body, role)


# -- resolvers ---------------------------------------------------------------


def _hist_env(value):
    return {
        "ok": True,
        "data": {
            "entries": [
                {
                    "num": 1,
                    "operation": "exposure",
                    "active": True,
                    "multi_priority": 0,
                    "decoded": {"exposure": 0.2},
                },
                {
                    "num": 4,
                    "operation": "exposure",
                    "active": True,
                    "multi_priority": 0,
                    "decoded": {"exposure": value},
                },
                {
                    "num": 5,
                    "operation": "exposure",
                    "active": False,
                    "multi_priority": 0,
                    "decoded": {"exposure": 9.0},
                },
            ]
        },
    }


def test_hist_resolver_uses_newest_active_entry():
    v, prov = rb.resolve_value(
        {"$hist": {"step": 0, "op": "exposure", "param": "exposure", "add": 1.0, "fallback": 1.7}},
        [{"envelope": _hist_env(0.7)}],
    )
    assert v == pytest.approx(1.7) and prov["source"] == "history_read" and prov["read"] == 0.7


def test_hist_resolver_falls_back_and_says_so():
    v, prov = rb.resolve_value(
        {"$hist": {"step": 0, "op": "sigmoid", "param": "contrast", "add": 0.3, "fallback": 1.8}},
        [{"envelope": _hist_env(0.7)}],
    )
    assert v == 1.8 and prov["source"] == "fallback"


def test_ref_resolver_with_null_kelvin_falls_back():
    steps = [{"envelope": {"ok": True, "data": {"kelvin": None}}}]
    assert (
        rb.resolve_value(
            {"$ref": {"step": 0, "path": "data.kelvin", "add": 600, "fallback": 6500}}, steps
        )[0]
        == 6500.0
    )
    steps = [{"envelope": {"ok": True, "data": {"kelvin": 5000}}}]
    assert (
        rb.resolve_value(
            {"$ref": {"step": 0, "path": "data.kelvin", "add": 600, "fallback": 6500}}, steps
        )[0]
        == 5600.0
    )


def test_render_refuses_unknown_placeholder():
    with pytest.raises(KeyError):
        rb.render("{frame:nowhere}", {})


# -- assertions --------------------------------------------------------------


def _snap(end=3, fp="a", **mods):
    return {"exists": True, "history_end": end, "fingerprint": fp, "modules": mods}


def _mod(enabled=True, **decoded):
    return {"num": 1, "enabled": enabled, "decoded": decoded}


def test_param_delta_within_and_outside_tolerance():
    ctx = {
        "before": {"dark": _snap(exposure=_mod(exposure=0.7))},
        "after": {"dark": _snap(exposure=_mod(exposure=1.705))},
    }
    a = {
        "type": "param_delta",
        "frame": "dark",
        "module": "exposure",
        "param": "exposure",
        "delta": 1.0,
    }
    assert rb.evaluate(a | {"tol": 0.02}, ctx)["ok"]
    assert not rb.evaluate(a | {"tol": 0.001}, ctx)["ok"]


def test_param_delta_uses_before_default_for_a_new_module():
    ctx = {
        "before": {"hero": _snap()},
        "after": {"hero": _snap(colorbalancergb=_mod(contrast=-0.1))},
    }
    a = {
        "type": "param_delta",
        "frame": "hero",
        "module": "colorbalancergb",
        "param": "contrast",
        "delta": -0.1,
        "tol": 0.01,
        "before_default": 0.0,
    }
    assert rb.evaluate(a, ctx)["ok"]


def test_wb_ratio_and_green_directions():
    before = _snap(temperature=_mod(coeff_r=2.0, coeff_g=1.0, coeff_b=1.7))
    warmer = _snap(temperature=_mod(coeff_r=2.2, coeff_g=1.0, coeff_b=1.5))
    ctx = {"before": {"hero": before}, "after": {"hero": warmer}}
    assert rb.evaluate({"type": "wb_ratio", "frame": "hero", "sign": 1, "min_rel": 0.005}, ctx)[
        "ok"
    ]
    assert not rb.evaluate(
        {"type": "wb_ratio", "frame": "hero", "sign": -1, "min_rel": 0.005}, ctx
    )["ok"]
    magenta = _snap(temperature=_mod(coeff_r=2.0, coeff_g=0.9, coeff_b=1.7))
    ctx = {"before": {"hero": before}, "after": {"hero": magenta}}
    assert rb.evaluate({"type": "wb_green", "frame": "hero", "sign": -1, "min_rel": 0.005}, ctx)[
        "ok"
    ]
    assert rb.evaluate({"type": "wb_changed", "frame": "hero", "min_rel": 0.005}, ctx)["ok"]


def test_slot_scan_finds_an_unnamed_value():
    ctx = {"after": {"hero": _snap(sigmoid=_mod(contrast=1.5, f3=0.2, f4=0.0))}}
    r = rb.evaluate(
        {"type": "slot_value", "frame": "hero", "module": "sigmoid", "value": 0.2, "tol": 0.001},
        ctx,
    )
    assert r["ok"] and r["measured"] == ["f3"]


def test_step_ok_rejects_a_dry_run_outcome():
    """`undo_last` on an empty journal answers ok:true, outcome dry_run: not a success."""
    ctx = {"steps": [{"envelope": {"ok": True, "outcome": "dry_run", "data": {}}}]}
    assert not rb.evaluate({"type": "step_ok", "step": 0}, ctx)["ok"]


def test_step_error_matches_codes():
    ctx = {
        "steps": [{"envelope": {"ok": False, "outcome": "error:usage", "error": {"code": "usage"}}}]
    }
    assert rb.evaluate({"type": "step_error", "step": 0, "codes": ["usage"]}, ctx)["ok"]
    assert not rb.evaluate({"type": "step_error", "step": 0, "codes": ["not_in_darkroom"]}, ctx)[
        "ok"
    ]
    assert not rb.evaluate({"type": "step_error", "step": 1}, ctx)["ok"]


def test_step_field_arg_reference():
    ctx = {
        "steps": [
            {"args": {"value": 5600}, "envelope": {}},
            {"envelope": {"ok": True, "data": {"kelvin": 5520}}},
        ]
    }
    a = {
        "type": "step_field",
        "step": 1,
        "path": "data.kelvin",
        "op": "abs_within",
        "value": {"$arg": {"step": 0, "arg": "value"}},
        "tol": 150,
    }
    assert rb.evaluate(a, ctx)["ok"]
    ctx["steps"][1]["envelope"]["data"]["kelvin"] = None
    assert not rb.evaluate(a, ctx)["ok"]


def test_history_grew_and_unchanged():
    ctx = {
        "before": {"job:1": _snap(end=2, fp="x"), "job:2": _snap(end=2, fp="y")},
        "after": {"job:1": _snap(end=5, fp="x2"), "job:2": _snap(end=2, fp="y")},
    }
    assert rb.evaluate(
        {"type": "history_grew", "frames": ["job:1", "job:2"], "min_frames": 1}, ctx
    )["ok"]
    assert not rb.evaluate(
        {"type": "history_grew", "frames": ["job:1", "job:2"], "min_frames": 2}, ctx
    )["ok"]
    assert rb.evaluate({"type": "history_unchanged", "frames": ["job:2"]}, ctx)["ok"]
    assert not rb.evaluate({"type": "history_unchanged", "frames": ["job:1"]}, ctx)["ok"]


def test_tools_absent_needs_a_tool_list():
    assert not rb.evaluate({"type": "tools_absent", "patterns": ["mask"]}, {"tools": []})["ok"]
    assert rb.evaluate({"type": "tools_absent", "patterns": ["mask"]}, {"tools": ["darkroom_set"]})[
        "ok"
    ]
    assert not rb.evaluate(
        {"type": "tools_absent", "patterns": ["mask"]}, {"tools": ["mask_draw"]}
    )["ok"]


def _jpeg(path: Path, w: int, h: int, pad: int = 0) -> None:
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, h, w, 1) + b"\x01\x11\x00"
    path.write_bytes(b"\xff\xd8" + sof + b"\x00" * pad + b"\xff\xd9")


def test_export_file_checks_size_and_edge(tmp_path):
    out = tmp_path / "proof.jpg"
    _jpeg(out, 2048, 1536, pad=200_000)
    ctx = {
        "steps": [{"envelope": {"ok": True, "data": {"files": [{"output": str(out), "ok": True}]}}}]
    }
    assert rb.evaluate(
        {"type": "export_file", "step": 0, "min_bytes": 100000, "jpeg_long_edge": 2048}, ctx
    )["ok"]
    assert not rb.evaluate(
        {"type": "export_file", "step": 0, "min_bytes": 100000, "jpeg_long_edge": 1024}, ctx
    )["ok"]


def test_stats_delta():
    ctx = {
        "steps": [
            {"envelope": {"data": {"stats": {"clipped_high_pct": 4.0}}}},
            {},
            {},
            {"envelope": {"data": {"stats": {"clipped_high_pct": 1.0}}}},
        ]
    }
    assert rb.evaluate(
        {
            "type": "stats_delta",
            "before_step": 0,
            "after_step": 3,
            "path": "data.stats.clipped_high_pct",
            "sign": -1,
        },
        ctx,
    )["ok"]


# -- scoring -----------------------------------------------------------------

SCORING = {"pass": ["a", "b"], "partial": [["c"]], "refusal": ["r", "u"]}


def _res(**oks):
    return {k: {"ok": v} for k, v in oks.items()}


def test_score_pass_partial_fail():
    kw = {"wall_s": 1.0, "budget_s": 5.0, "category": "exposure"}
    assert rb.score_task(SCORING, _res(a=True, b=True), **kw) == ("pass", "verified")
    assert rb.score_task(SCORING, _res(a=True, b=False, c=True), **kw) == ("partial", "partial")
    assert rb.score_task(SCORING, _res(a=False, r=True, u=True), **kw) == ("fail", "honest_refusal")
    assert rb.score_task(SCORING, _res(a=False), last_envelope_ok=True, **kw) == (
        "fail",
        "reported_ok_unverified",
    )
    assert rb.score_task(SCORING, _res(a=False), **kw) == ("fail", "failed")


def test_score_over_budget_is_partial():
    assert rb.score_task(
        SCORING, _res(a=True, b=True), wall_s=9.0, budget_s=5.0, category="exposure"
    ) == ("partial", "over_budget")


def test_score_unreachable_pass_is_correct_refusal_and_blocked_is_fail():
    assert rb.score_task(
        {"pass": ["a"]}, _res(a=True), wall_s=0, budget_s=2, category="unreachable"
    ) == ("pass", "correct_refusal")
    assert rb.score_task(
        SCORING, _res(a=True, b=True), wall_s=0, budget_s=2, category="lens", blocked=True
    ) == ("fail", "blocked")


def test_empty_pass_list_never_passes():
    assert rb.score_task({"pass": []}, {}, wall_s=0, budget_s=1, category="lens")[0] == "fail"


# -- a whole fake run ----------------------------------------------------------


def _ctx(body: str) -> dict:
    doc = rb.load_tasks()
    spec = doc["frames"][body]
    ctx = {
        "body": body,
        "run_id": "20260924t000000z",
        "card": "/fake/card",
        "job_slug": f"bench-{body}-x",
        "preset": f"scouter-bench-{body}-x",
        "out_dir": "/fake/out",
    }
    for role in ("dark", "bright", "blown", "hero", "calib"):
        name = spec.get(role) or spec["hero"]
        ctx[f"frame:{role}"] = f"/fake/{body}/{Path(name).name}"
        ctx[f"framename:{role}"] = Path(name).name
        ctx[f"framebase:{role}"] = Path(name).name
    for i, name in enumerate(spec["job"], start=1):
        ctx[f"jobname:{i}"] = Path(name).name
        ctx[f"job:{i}"] = f"/fake/job/{i:04d}.raw"
    return ctx


class FakeMcp:
    """Answers like Scouter does today for the calls the plan makes."""

    def __init__(self, xmp: dict):
        self.xmp = xmp  # path -> exposure value, mutated by darkroom_set
        self.calls = []

    def initialize(self):
        return {}

    def list_tools(self):
        return ["darkroom_set", "darkroom_undo_last", "history_read", "wb_kelvin", "style_extract"]

    def call_tool(self, name, args, timeout):
        self.calls.append((name, args))
        if name == "history_read":
            v = self.xmp.get(args["image"], 0.7)
            return {
                "ok": True,
                "outcome": "ok",
                "data": {
                    "entries": [
                        {
                            "num": 1,
                            "operation": "exposure",
                            "active": True,
                            "multi_priority": 0,
                            "decoded": {"exposure": v},
                        }
                    ]
                },
            }
        if name == "darkroom_set":
            if (args["module"], args["param"]) not in {
                ("exposure", "exposure"),
                ("sigmoid", "contrast"),
            }:
                return {
                    "ok": False,
                    "outcome": "error:usage",
                    "data": {},
                    "error": {"code": "usage", "message": "no pinned path"},
                }
            self.xmp["dark"] = args["value"]
            return {"ok": True, "outcome": "ok", "data": {"applied": args["value"]}}
        if name == "darkroom_undo_last":
            return {"ok": True, "outcome": "dry_run", "data": {"reason": "nothing to undo"}}
        return {
            "ok": False,
            "outcome": "error:usage",
            "data": {},
            "error": {"code": "usage", "message": "fake"},
        }


class FakeHost:
    def __init__(self):
        self.launched, self.quits = [], 0

    def launch(self, frame):
        self.launched.append(str(frame))
        return {"ok": True, "ready_s": 0.1}

    def alive(self):
        return True

    def delete_style(self, name):
        return {"ok": True}

    def quit(self):
        self.quits += 1
        return {"ok": True, "how": "fake"}


def _fast(doc):
    for t in doc["tasks"]:
        if t.get("settle_s"):
            t["settle_s"] = 0.05
    return doc


def test_fake_run_produces_a_valid_result(tmp_path, monkeypatch):
    doc = _fast(rb.load_tasks())
    state = {}
    fake = FakeMcp(state)

    def snapshot(path):
        v = state.get("dark", 0.7) if path.endswith("P5240151.ORF") else 0.7
        return _snap(end=3, fp=str(v), exposure=_mod(exposure=v))

    monkeypatch.setattr(rb, "git_info", lambda: {"sha": "0" * 40, "branch": "test", "dirty": False})
    run = rb.BenchRun(
        doc,
        "olympus",
        run_id="t",
        label="fake",
        mcp=fake,
        host=FakeHost(),
        snapshot=snapshot,
        paths=_ctx("olympus"),
        log=lambda *_: None,
    )
    result = run.run()
    assert rb.validate_result(result) == []
    assert len(result["tasks"]) == len(doc["tasks"])
    by_id = {r["id"]: r for r in result["tasks"]}
    assert by_id["E1"]["score"] == "pass"  # 0.7 read, 1.7 set, delta 1.0
    assert by_id["C2"]["outcome"] == "honest_refusal"
    assert by_id["U1"]["outcome"] == "correct_refusal"
    assert by_id["F1"]["score"] == "fail"  # dry_run undo is never a pass
    assert run.host.quits == 5 and len(run.host.launched) == 5  # calib, A, B, B2, C
    j, m = rb.write_results(result, tmp_path, "t")
    assert json.loads(j.read_text())["body"] == "olympus"
    assert "| E1 |" in m.read_text()
    delta = rb.compare(result, result)
    assert "points" in delta and "**changed**" not in delta


def test_blocked_launch_marks_session_tasks_blocked(monkeypatch):
    doc = rb.load_tasks()

    class DeadHost(FakeHost):
        def launch(self, frame):
            return {"ok": False, "error": "never reached the darkroom"}

    monkeypatch.setattr(rb, "git_info", lambda: {"sha": "0" * 40, "branch": "test", "dirty": False})
    run = rb.BenchRun(
        doc,
        "sony",
        run_id="t",
        label="fake",
        mcp=FakeMcp({}),
        host=DeadHost(),
        snapshot=lambda p: _snap(),
        paths=_ctx("sony"),
        log=lambda *_: None,
    )
    result = run.run()
    assert {r["outcome"] for r in result["tasks"]} == {"blocked"}
    assert run.host.quits == 4  # sony has no calib session; quit is always called
    assert rb.validate_result(result) == []


def test_main_refuses_without_live(capsys):
    assert rb.main([]) == 2


def test_settle_scores_the_late_sidecar_and_records_the_lag(monkeypatch):
    """darktable writes a style application's sidecar after the call returns:
    the immediate snapshot is kept, scoring uses the settled one."""
    doc = rb.load_tasks()
    task = next(t for t in doc["tasks"] if t["id"] == "F4")
    polls = {"n": 0}

    def snapshot(path):
        polls["n"] += 1
        late = polls["n"] > 6  # 3 before + 3 immediate-after, then it lands
        return _snap(end=4 if late else 0, fp="late" if late else "x")

    class Mcp(FakeMcp):
        def call_tool(self, name, args, timeout):
            return {"ok": True, "outcome": "ok", "data": {"applied": 3}}

    monkeypatch.setattr(rb, "git_info", lambda: {"sha": "0" * 40, "branch": "t", "dirty": False})
    run = rb.BenchRun(
        doc,
        "olympus",
        run_id="t",
        label="fake",
        mcp=Mcp({}),
        host=FakeHost(),
        snapshot=snapshot,
        paths=_ctx("olympus"),
        log=lambda *_: None,
        settle_poll_s=0.01,
    )
    run.tools = ["style_apply_set"]
    r = run.run_task(task)
    assert r["settle"]["changed_after_call_returned"] is True
    assert r["settle"]["immediate"]["job:1"]["history_end"] == 0
    assert r["score"] == "pass"


def test_session_filter_runs_only_named_sessions(monkeypatch):
    doc = rb.load_tasks()
    monkeypatch.setattr(rb, "git_info", lambda: {"sha": "0" * 40, "branch": "t", "dirty": False})
    host = FakeHost()
    run = rb.BenchRun(
        doc,
        "olympus",
        run_id="t",
        label="fake",
        mcp=FakeMcp({}),
        host=host,
        snapshot=lambda p: _snap(),
        paths=_ctx("olympus"),
        log=lambda *_: None,
        sessions=["A"],
    )
    result = run.run()
    assert [t["id"] for t in result["tasks"]] == ["E1", "T4", "S2"]
    assert len(host.launched) == 1


def test_merge_replaces_by_id_and_keeps_the_replaced(monkeypatch):
    monkeypatch.setattr(rb, "git_info", lambda: {"sha": "0" * 40, "branch": "t", "dirty": False})
    doc = _fast(rb.load_tasks())

    class DeadHost(FakeHost):
        def launch(self, frame):
            return {"ok": False, "error": "x"}

    base = rb.BenchRun(
        doc,
        "sony",
        run_id="a",
        label="l",
        mcp=FakeMcp({}),
        host=DeadHost(),
        snapshot=lambda p: _snap(),
        paths=_ctx("sony"),
        log=lambda *_: None,
    ).run()
    over = {
        "run_id": "b",
        "body": "sony",
        "sessions": [],
        "tasks": [dict(base["tasks"][0], score="pass", outcome="verified")],
    }
    merged = rb.merge(base, over, label="m")
    assert merged["tasks"][0]["from_run"] == "b" and merged["tasks"][1]["from_run"] == "a"
    assert merged["summary"]["counts"]["pass"] == 1 and len(merged["replaced"]) == 1
    assert rb.validate_result(merged) == []


# -- revision 2: plan v2 and the E2 frame ------------------------------------


def test_revision_2_changes_only_e2_and_adds_plan_v2_to_every_task():
    doc = rb.load_tasks()
    assert doc["revisions"][1]["revision"] == 2 and doc["revisions"][1]["invalidates"] == ["E2"]
    assert all("plan_v2" in t for t in doc["tasks"])
    revised = [t["id"] for t in doc["tasks"] if t.get("revised_in") == 2]
    assert revised == ["E2"]
    e2 = rb.tasks_by_id(doc)["E2"]
    assert e2["frames"] == ["blown"] and "{frame:bright}" not in json.dumps(e2)
    # F7 still opens the bright frame, imported in an earlier session (B).
    order = [s["id"] for s in doc["sessions"]]
    assert order.index("B") < order.index("C") and "F7" in doc["sessions"][-1]["tasks"]
    assert next(s for s in doc["sessions"] if s["id"] == "B")["role"] == "bright"


def test_plan_v1_is_the_task_unchanged():
    doc = rb.load_tasks()
    for t in doc["tasks"]:
        e = rb.effective_task(t, "v1")
        assert e["plan"] == t["plan"] and e["assertions"] == t["assertions"]
        assert e["plan_used"] == "v1"


def test_plan_v2_remaps_steps_and_never_changes_thresholds():
    doc = rb.load_tasks()
    by_id = rb.tasks_by_id(doc)
    e1 = rb.effective_task(by_id["E1"], "v2")
    assert [s["tool"] for s in e1["plan"]] == ["darkroom_say"]
    assert e1["assertions"]["set_ok"]["step"] == 0
    assert e1["assertions"]["delta"] == by_id["E1"]["assertions"]["delta"]
    w1 = rb.effective_task(by_id["W1"], "v2")
    assert w1["assertions"]["kelvin"]["step"] == 2
    assert w1["assertions"]["kelvin"]["tol"] == 150
    assert w1["assertions"]["kelvin"]["value"] == {
        "$env": {"step": 1, "path": "data.moves.0.target"}
    }
    assert (w1["assertions"]["render"]["before_step"], w1["assertions"]["render"]["after_step"]) == (0, 3)
    c4 = rb.effective_task(by_id["C4"], "v2")
    assert c4["plan"] == by_id["C4"]["plan"] and c4["plan_used"] == "v2=v1"
    f1 = rb.effective_task(by_id["F1"], "v2")
    assert f1["context"][0]["tool"] == "darkroom_say"
    for t in doc["tasks"]:
        e = rb.effective_task(t, "v2")
        assert e["scoring"] == t["scoring"] and e["budget_s"] == t["budget_s"]
        for aid, a in t["assertions"].items():
            for k in ("tol", "delta", "sign", "min_rel", "frame", "frames", "min_frames"):
                assert e["assertions"][aid].get(k) == a.get(k), (t["id"], aid, k)


def test_validator_refuses_a_threshold_override():
    doc = rb.load_tasks()
    t = rb.tasks_by_id(doc)["W1"]
    t["plan_v2"]["assertion_overrides"]["kelvin"]["tol"] = 9999
    assert any("may not override" in e for e in rb.validate_tasks(doc))


def test_validator_wants_every_asserted_step_mapped():
    doc = rb.load_tasks()
    t = rb.tasks_by_id(doc)["E1"]
    t["plan_v2"]["step_map"] = {}
    assert any("no plan v2 equivalent" in e for e in rb.validate_tasks(doc))


def test_env_expected_and_set_eq():
    ctx = {
        "steps": [
            {"envelope": {"data": {"moves": [{"target": 5600.0}]}}},
            {"envelope": {"data": {"kelvin": 5550.0, "modules": ["b", "a"]}}},
        ]
    }
    ok = rb.evaluate(
        {
            "type": "step_field",
            "step": 1,
            "path": "data.kelvin",
            "op": "abs_within",
            "value": {"$env": {"step": 0, "path": "data.moves.0.target"}},
            "tol": 150,
        },
        ctx,
    )
    assert ok["ok"] and ok["expected"] == 5600.0
    assert rb.evaluate(
        {
            "type": "step_field",
            "step": 1,
            "path": "data.modules",
            "op": "set_eq",
            "value": ["a", "b"],
        },
        ctx,
    )["ok"]
    assert not rb.evaluate(
        {"type": "step_field", "step": 1, "path": "data.modules", "op": "set_eq", "value": ["a"]},
        ctx,
    )["ok"]


def test_fake_v2_run_is_valid_and_records_the_plan(monkeypatch):
    doc = _fast(rb.load_tasks())
    monkeypatch.setattr(rb, "git_info", lambda: {"sha": "0" * 40, "branch": "t", "dirty": False})
    result = rb.BenchRun(
        doc,
        "olympus",
        run_id="t",
        label="fake",
        mcp=FakeMcp({}),
        host=FakeHost(),
        snapshot=lambda p: _snap(),
        paths=_ctx("olympus"),
        log=lambda *_: None,
        plan_version="v2",
    ).run()
    assert rb.validate_result(result) == []
    assert result["plan_version"] == "v2" and result["tasks_revision"] == 3
    by_id = {r["id"]: r for r in result["tasks"]}
    assert by_id["E1"]["plan_used"] == "v2" and by_id["E1"]["plan"][0]["tool"] == "darkroom_say"
    assert by_id["S2"]["plan_used"] == "v2=v1"


def test_compare_marks_revised_tasks_not_comparable(monkeypatch):
    monkeypatch.setattr(rb, "git_info", lambda: {"sha": "0" * 40, "branch": "t", "dirty": False})
    doc = _fast(rb.load_tasks())
    new = rb.BenchRun(
        doc,
        "sony",
        run_id="n",
        label="new",
        mcp=FakeMcp({}),
        host=FakeHost(),
        snapshot=lambda p: _snap(),
        paths=_ctx("sony"),
        log=lambda *_: None,
    ).run()
    old = dict(new, label="old", tasks_revision=1)
    out = rb.compare(old, new)
    assert "| E2 |" in out and "not comparable (task revised)" in out
    assert "| category | tasks | before | after |" in out


def test_a_hung_setup_is_relaunched_once_and_recorded(monkeypatch):
    monkeypatch.setattr(rb, "git_info", lambda: {"sha": "0" * 40, "branch": "t", "dirty": False})
    doc = _fast(rb.load_tasks())

    class HangOnce(FakeMcp):
        hung = False

        def call_tool(self, name, args, timeout):
            if name == "history_commit" and not HangOnce.hung:
                HangOnce.hung = True
                raise TimeoutError("history_commit did not answer")
            if name == "history_commit":
                return {"ok": True, "outcome": "ok", "data": {}}
            return super().call_tool(name, args, timeout)

    class Host(FakeHost):
        def alive(self):
            return HangOnce.hung is False or len(self.launched) > 1

    host = Host()
    result = rb.BenchRun(
        doc,
        "sony",
        run_id="t",
        label="f",
        mcp=HangOnce({}),
        host=host,
        snapshot=lambda p: _snap(),
        paths=_ctx("sony"),
        log=lambda *_: None,
        sessions=["A"],
    ).run()
    ses = result["sessions"][0]
    assert "hung_first_launch" in ses and len(host.launched) == 2
    assert ses["setup"][0]["envelope"]["ok"] is True
    assert {r["outcome"] for r in result["tasks"]} != {"blocked"}


# -- warmth through colour calibration (route A): two oracles that work for both routes ---------


def _cc(temp):
    return _mod(temperature=temp)


def _cc_snap(temp, fp, **mods):
    return _snap(fp=fp, channelmixerrgb=_cc(temp), **mods)


def _stats_env(rg, bg):
    return {"ok": True, "outcome": "ok", "data": {"stats": {"wb_ratio_rg": rg, "wb_ratio_bg": bg}}}


def test_warmth_slot_route_a_checks_direction_and_amount():
    # The target is the sidecar BEFORE value plus the asked delta, never what was sent.
    a = {"type": "warmth_slot", "frame": "hero", "sign": 1, "delta": 600, "tol": 150,
         "min_rel": 0.005}

    def run(before_k, after_k, spec=a, wb_after=None):
        wb_before = _mod(coeff_r=2.0, coeff_g=1.0, coeff_b=1.7)
        ctx = {"before": {"hero": _cc_snap(before_k, "a", temperature=wb_before)},
               "after": {"hero": _cc_snap(after_k, "b", temperature=wb_after or wb_before)}}
        return rb.evaluate(spec, ctx)

    assert run(3927.0, 4527.0)["ok"]
    assert run(3927.0, 4600.0)["ok"]  # inside the step tolerance
    assert not run(3927.0, 3327.0)["ok"]  # the wrong way, even to a nearby number
    assert not run(3927.0, 4100.0)["ok"]  # right way, short of the asked amount
    assert not run(3927.0, 5600.0)["ok"]  # +1673 K for "a little warmer" (the v1 5000+600 case)
    assert not run(3927.0, 3927.0)["ok"]  # nothing committed
    assert run(4527.0, 3627.0, a | {"sign": -1, "delta": -900})["ok"]
    assert run(3927.0, 5500.0, {k: v for k, v in a.items() if k != "delta"} | {"value": 5500.0,
                                                                              "tol": 250})["ok"]
    # route A must leave the WB module alone: a moved slot AND moved WB coefficients fails
    moved_wb = _mod(coeff_r=2.2, coeff_g=1.0, coeff_b=1.5)
    assert not run(3927.0, 4527.0, wb_after=moved_wb)["ok"]


def test_warmth_slot_legacy_route_reads_the_wb_module():
    warm = _snap(fp="b", temperature=_mod(coeff_r=2.2, coeff_g=1.0, coeff_b=1.5))
    base = _snap(fp="a", temperature=_mod(coeff_r=2.0, coeff_g=1.0, coeff_b=1.7))
    a = {"type": "warmth_slot", "frame": "hero", "sign": 1, "delta": 600, "tol": 150,
         "min_rel": 0.005}
    assert rb.evaluate(a, {"before": {"hero": base}, "after": {"hero": warm}})["ok"]
    assert not rb.evaluate(a | {"sign": -1}, {"before": {"hero": base}, "after": {"hero": warm}})["ok"]
    assert not rb.evaluate(a, {"before": {"hero": base}, "after": {"hero": base}})["ok"]


def test_render_rb_delta_needs_a_move_beyond_the_noise_floor():
    def run(after, sign=1, min_rel=0.005):
        ctx = {"steps": [{"envelope": _stats_env(0.80, 0.50)}, {}, {"envelope": _stats_env(*after)}]}
        return rb.evaluate({"type": "render_rb_delta", "before_step": 0, "after_step": 2,
                            "sign": sign, "min_rel": min_rel}, ctx)

    assert run((0.88, 0.48))["ok"]  # r/b 1.6 -> 1.833: warmer
    assert not run((0.88, 0.48), sign=-1)["ok"]
    assert not run((0.800, 0.4999))["ok"]  # r/b +0.02%: inside the noise floor
    assert run((0.72, 0.52), sign=-1)["ok"]
    assert not run((0.82, 0.49), min_rel=0.05)["ok"]  # +4.6%: under a 5% floor
    assert run((0.88, 0.48), min_rel=0.05)["ok"]
    # not_opposite: only "went the other way beyond the floor" fails
    def opp(after):
        ctx = {"steps": [{"envelope": _stats_env(0.80, 0.50)}, {}, {"envelope": _stats_env(*after)}]}
        return rb.evaluate({"type": "render_rb_delta", "before_step": 0, "after_step": 2,
                            "sign": 1, "min_rel": 0.005, "not_opposite": True}, ctx)

    assert opp((0.8, 0.5))["ok"] and opp((0.88, 0.48))["ok"]
    assert not opp((0.70, 0.55))["ok"]
    assert not rb.evaluate({"type": "render_rb_delta", "before_step": 0, "after_step": 2,
                            "sign": 1, "min_rel": 0.005}, {"steps": []})["ok"]


def _warmth_ctx(tid, version, *, before_k, after_k, rb_after, effective, target, ok=True):
    task = rb.effective_task(rb.tasks_by_id(rb.load_tasks())[tid], version)
    steps, stats_seen = [], 0
    for s in task["plan"]:
        args = dict(s.get("args") or {})
        if s["tool"] == "scout_stats":
            env = _stats_env(0.80, 0.50) if stats_seen == 0 else _stats_env(*rb_after)
            stats_seen += 1
        elif s["tool"] == "wb_kelvin":
            env = {"ok": True, "outcome": "ok", "data": {"kelvin": 5000.0,
                                                         "effective_kelvin": effective}}
        elif s["tool"] == "darkroom_say":
            env = {"ok": True, "outcome": "ok", "data": {"moves": [{"target": target}]}}
        else:
            args["value"] = target
            env = {"ok": ok, "outcome": "ok" if ok else "failed", "data": {}}
        steps.append({"tool": s["tool"], "args": args, "envelope": env})
    ctx = {"before": {"hero": _cc_snap(before_k, "a")}, "after": {"hero": _cc_snap(after_k, "b")},
           "steps": steps}
    results = {aid: rb.evaluate(a, ctx) | {"type": a["type"]}
               for aid, a in task["assertions"].items()}
    return rb.score_task(task["scoring"], results, wall_s=1, budget_s=99,
                         category="white_balance", last_envelope_ok=ok), results


def test_w1_w2_route_a_move_verifies_in_both_plan_versions():
    for version in ("v1", "v2"):
        score, res = _warmth_ctx("W1", version, before_k=3927.0, after_k=4527.0,
                                 rb_after=(0.86, 0.46), effective=4527.0, target=4527.0)
        assert score == ("pass", "verified"), (version, res)
        score, res = _warmth_ctx("W2", version, before_k=4527.0, after_k=3627.0,
                                 rb_after=(0.74, 0.54), effective=3627.0, target=3627.0)
        assert score == ("pass", "verified"), (version, res)


def test_w1_a_reported_ok_with_nothing_on_the_sidecar_or_the_render_is_not_verified():
    for version in ("v1", "v2"):
        score, _ = _warmth_ctx("W1", version, before_k=3927.0, after_k=3927.0,
                               rb_after=(0.80, 0.50), effective=3927.0, target=4527.0)
        assert score == ("fail", "reported_ok_unverified")
        # sidecar moved but the render did not: the second oracle refuses the pass
        score, _ = _warmth_ctx("W1", version, before_k=3927.0, after_k=4527.0,
                               rb_after=(0.80, 0.50), effective=4527.0, target=4527.0)
        assert score == ("partial", "partial")  # inconclusive render only
        # the render moved the wrong way (the 2026-09 night-1 failure): a FAIL, not partial
        score, _ = _warmth_ctx("W1", version, before_k=3927.0, after_k=4527.0,
                               rb_after=(0.70, 0.55), effective=4527.0, target=4527.0)
        assert score[0] == "fail"
        score, _ = _warmth_ctx("W2", version, before_k=4527.0, after_k=3627.0,
                               rb_after=(0.90, 0.45), effective=3627.0, target=3627.0)
        assert score[0] == "fail"


def test_w1_a_wrong_size_move_is_not_verified_even_when_the_slot_equals_what_was_sent():
    # plan v1 used to start from the WB module's 5000 K: 3927 -> 5600 K for "a little warmer"
    for version in ("v1", "v2"):
        score, res = _warmth_ctx("W1", version, before_k=3927.0, after_k=5600.0,
                                 rb_after=(0.96, 0.40), effective=5600.0, target=5600.0)
        assert score[0] != "pass", (version, res)
        assert res["warmer"]["ok"] is False
    v1 = rb.effective_task(rb.tasks_by_id(rb.load_tasks())["W1"], "v1")
    ref = v1["plan"][2]["args"]["value"]["$ref"]
    assert ref["path"] == "data.effective_kelvin" and ref["step"] == 1


def test_w4_partial_needs_the_slot_to_have_moved_the_right_way():
    w4 = rb.tasks_by_id(rb.load_tasks())["W4"]["scoring"]["partial"]
    assert all("warmer" in g and "render_ok" in g for g in w4)
    score, _ = _warmth_ctx("W4", "v2", before_k=3927.0, after_k=3927.0,
                           rb_after=(0.80, 0.50), effective=3927.0, target=5500.0)
    assert score[0] == "fail"


def test_w4_daylight_route_a_sets_the_illuminant_and_is_verified_by_two_oracles():
    score, res = _warmth_ctx("W4", "v2", before_k=3927.0, after_k=5500.0,
                             rb_after=(0.90, 0.45), effective=5500.0, target=5500.0)
    assert score == ("pass", "verified"), res
    score, _ = _warmth_ctx("W4", "v2", before_k=3927.0, after_k=3927.0,
                           rb_after=(0.80, 0.50), effective=3927.0, target=5500.0)
    assert score == ("fail", "reported_ok_unverified")


def test_warmth_tasks_keep_history_unchanged_only_as_the_refusal_check():
    tasks = rb.tasks_by_id(rb.load_tasks())
    for tid in ("W1", "W2", "W4"):
        t = tasks[tid]
        assert t["assertions"]["untouched"]["type"] == "history_unchanged"
        assert "untouched" not in t["scoring"]["pass"]
        assert t["scoring"]["refusal"] == ["refused", "untouched"]
        assert tid in [x for r in rb.load_tasks()["revisions"] for x in r.get("invalidates", [])]
        for version in rb.PLAN_VERSIONS:
            tools = [s["tool"] for s in rb.effective_task(t, version)["plan"]]
            assert tools[0] == tools[-1] == "scout_stats", (tid, version, tools)
