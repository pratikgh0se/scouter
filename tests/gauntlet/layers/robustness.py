"""Layer 5: robustness (spec 3.5). R1-R24 + H1-H4 + 4 watchdog cases = 32.
P0: R1, R3, R12, R17 and H1-H4. Every expectation is a typed answer."""

from __future__ import annotations

from gauntlet.case import Case, Check, Expect, Step, case_id
from tests.gauntlet.taxonomy import Taxonomy

LAYER, PREFIX = "robustness", "rob"
P0 = {"R1", "R3", "R12", "R17", "H1", "H2", "H3", "H4"}
TIMEOUTS = ["bridge_timeout", "commit_timeout", "gui_not_ready", "darktable_unresponsive"]

#: id -> (fault, steps, expect kind, codes). The fault name is
#: what tests/gauntlet/faults.py implements; `steps` run after it is armed.
PLANS: dict[str, tuple] = {
    "R1": ("quit_darktable", [("darkroom_say", {"phrase": "brighter"})], "typed_error",
           ["bridge_timeout", "darktable_not_running"]),
    "R2": ("kill_during_call", [("darkroom_say", {"phrase": "brighter"})], "typed_error",
           TIMEOUTS + ["darktable_not_running"]),
    "R3": ("wedge_worker", [("darkroom_say", {"phrase": "brighter"})], "typed_error", TIMEOUTS),
    "R4": ("relaunch_stale_job", [("darkroom_undo_last", {})], "typed_error",
           ["noop", "usage", "state_locked", "not_found"]),
    "R5": ("delete_sidecar", [("history_read", {"image": "{frame:hero}", "source": "xmp"})],
           "typed_error", ["no_sidecar", "not_found"]),
    "R6": ("chmod_sidecar_readonly", [("darkroom_say", {"phrase": "brighter"})], "typed_error",
           ["permission_denied", "cli_wrote_nothing", "commit_timeout"]),
    "R7": ("stale_sidecar", [("history_read", {"image": "{frame:hero}", "source": "auto"})],
           "workflow", []),
    "R8": ("torn_sidecar_copy", [("history_read", {"image": "{torn_copy}", "source": "xmp"})],
           "typed_error", ["usage", "no_sidecar", "error"]),
    "R9": ("second_library_lock", [("library_import", {"source": "{card}",
                                                       "job_slug": "{job_slug}",
                                                       "confirm": True})],
           "typed_error", ["db_locked", "library_locked"]),
    "R10": ("unicode_path", [("darkroom_open", {"image": "{unicode_copy}"}),
                             ("darkroom_say", {"phrase": "brighter"})], "workflow", []),
    "R11": ("long_path", [("darkroom_open", {"image": "{long_copy}"})], "typed_error",
            ["usage", "not_found", "unsupported_format"]),
    "R12": ("none", [("darkroom_open", {"image": "{missing_file}"})], "typed_error",
            ["not_found", "usage"]),
    "R13": ("none", [("darkroom_open", {"image": "{unsupported_file}"})], "typed_error",
            ["unsupported_format", "usage"]),
    "R14": ("huge_job_one_timeout", [("job_run", {"job_slug": "{job_slug}", "preset": "{preset}",
                                                  "hero": "{job_frame:1}", "confirm": True})],
            "workflow", []),
    "R15": ("duplicate_names", [("darkroom_open", {"image": "{bare_duplicate_name}"})],
            "typed_error", ["usage", "not_found"]),
    "R16": ("two_clients", [("darkroom_say", {"phrase": "brighter"})], "workflow", []),
    "R17": ("slow_bridge", [("darkroom_say", {"phrase": "brighter"})], "typed_error", TIMEOUTS),
    "R18": ("disk_nearly_full", [("export_run", {"images": ["{frame:hero}"],
                                                 "preset": "{export_preset}",
                                                 "out_dir": "{full_disk}", "confirm": True})],
            "typed_error", ["disk_full", "export_all_failed"]),
    "R19": ("chmod_out_dir", [("export_run", {"images": ["{frame:hero}"],
                                              "preset": "{export_preset}",
                                              "out_dir": "{denied_dir}", "confirm": True})],
            "typed_error", ["permission_denied", "export_all_failed"]),
    "R20": ("future_mtime", [("history_read", {"image": "{frame:hero}", "source": "auto"})],
            "workflow", []),
    "R21": ("fake_version", [("vocab_list", {})], "typed_error",
            ["darktable_version_mismatch"]),
    "R22": ("none", [("darkroom_enable", {"module": "liquify"})], "typed_error",
            ["usage", "module_unavailable"]),
    "R23": ("broken_luarc", [("darkroom_say", {"phrase": "brighter"})], "typed_error",
            TIMEOUTS + ["darktable_not_running"]),
    "R24": ("wrong_wb_table", [("wb_kelvin", {"image": "{frame:hero}"})], "workflow", []),
}
#: Workflow checks for the cases whose right answer is not an error.
WORKFLOW_CHECKS = {
    "R7": [Check("envelope", step=0, target={"path": "ok", "op": "eq", "value": True})],
    "R10": [Check("envelope", step=1, target={"path": "ok", "op": "eq", "value": True}),
            Check("extra", target={"path": "sidecar_changed", "op": "truthy"})],
    "R14": [Check("extra", target={"path": "frames_reported", "op": "gte", "value": 200}),
            Check("extra", target={"path": "timeout_frame_reported", "op": "truthy"})],
    "R16": [Check("extra", target={"path": "no_interleaved_writes", "op": "truthy"})],
    "R20": [Check("envelope", step=0, target={"path": "ok", "op": "eq", "value": True})],
    "R24": [Check("extra", target={"path": "flagged_or_refused", "op": "truthy"})],
}
HANG = {
    "H1": ("cold_launch_commit", [("history_commit", {"image": "{frame:hero}", "timeout_s": 10})],
           ["noop", "gui_not_ready"]),
    "H2": ("modal_dialog", [("darkroom_say", {"phrase": "brighter"})], TIMEOUTS),
    "H3": ("full_res_pipe_busy", [("darkroom_say", {"phrase": "brighter"})] * 10, []),
    "H4": ("view_bounces", [("history_commit", {"image": "{frame:hero}"})] * 50, []),
}
#: scenario -> fault (matches faults.watchdog_probe). Two of the four probes
#: use `darkroom_enable` (a RETRY_SAFE verb per scouter/core/watchdog.py's
#: RETRY_SAFE, merged Task 29-30) instead of the brief's `darkroom_say`: see
#: the note in faults.watchdog_probe for why (row 433 adapter-context).
WATCHDOG = {
    "wd_ping": "ping_states", "wd_auto_heal": "auto_heal_once",
    "wd_heal_cap": "heal_cap_reached", "wd_foreign_pid": "foreign_pid",
}


def _steps(pairs) -> list[Step]:
    return [Step(t, dict(a), 12.0, t not in ("vocab_list", "history_read", "wb_kelvin"))
            for t, a in pairs]


def generate(tax: Taxonomy) -> list[Case]:
    rows = {r["id"]: r for r in tax.measurement["robustness"]}
    out: list[Case] = []
    for rid in sorted(rows, key=lambda x: int(x[1:])):
        fault, pairs, kind, codes = PLANS[rid]
        exp = Expect(kind, WORKFLOW_CHECKS.get(rid, []), codes=list(codes))
        out.append(Case(id=case_id(PREFIX, rid), layer=LAYER, tier="P0" if rid in P0 else "P1",
                        mode="tool", steps=_steps(pairs), expect=exp,
                        body="sony_a7" if rid == "R24" else "olympus",
                        frame_role="hero", source={"file": "measurement-and-robustness.json",
                                                   "id": rid},
                        meta={"fault": fault, "condition": rows[rid]["condition"]}))
    for hid, (fault, pairs, codes) in HANG.items():
        checks = [Check("extra", target={"path": "every_call_answered", "op": "truthy"}),
                  Check("extra", target={"path": "hung_calls", "op": "eq", "value": 0})]
        exp = Expect("workflow", checks, codes=list(codes))
        out.append(Case(id=case_id(PREFIX, hid), layer=LAYER, tier="P0", mode="tool",
                        steps=_steps(pairs), expect=exp, body="olympus", frame_role="hero",
                        source={"file": "measurement-and-robustness.json", "id": hid},
                        meta={"fault": fault, "allowed_codes": list(codes)}))
    for wid, fault in WATCHDOG.items():
        out.append(Case(id=case_id(PREFIX, wid), layer=LAYER, tier="P1", mode="tool",
                        steps=[Step("watchdog_probe", {"scenario": fault}, 90.0, False)],
                        expect=Expect("workflow", [Check("extra", target={
                            "path": "scenario_ok", "op": "truthy"})]),
                        body="olympus", frame_role="hero",
                        source={"file": "spec", "id": f"6/{wid}"},
                        meta={"fault": fault, "allow_heal": wid == "wd_auto_heal"}))
    return out


EXPECTED = 24 + 4 + 4
