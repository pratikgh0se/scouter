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

from tests.benchmark import run_editor_benchmark as rb

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
            rb.render(t["plan"], ctx)
            rb.render(t.get("context") or [], ctx)
            rb.render(t["assertions"], ctx)
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
        for role in ("dark", "bright", "hero"):
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
    for role in ("dark", "bright", "hero", "calib"):
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
    assert run.host.quits == 4 and len(run.host.launched) == 4  # calib, A, B, C
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
    assert run.host.quits == 3  # sony has no calib session; quit is always called
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
