"""Layer 3: bulk (spec 3.3). 9 flows x 2 bodies x {card 7, job 120} = 36,
plus 4 crash-mid-job cases (olympus, 120 frames). P1."""

from __future__ import annotations

from gauntlet.case import Case, Check, Expect, Step, case_id
from tests.gauntlet.layers.common import BODIES, WRITE_TOOLS
from tests.gauntlet.taxonomy import Taxonomy

LAYER, PREFIX = "bulk", "blk"
SIZES = {"card": 7, "job": 120}
PER_FRAME_S = 6.0
CRASH_VERBS = ("job_run", "export_run", "style_apply_set", "library_import")


def _s(tool: str, budget: float, **args) -> Step:
    return Step(tool, args, budget, tool in WRITE_TOOLS)


def _import(n: int) -> Step:
    return _s("library_import", PER_FRAME_S * n, source="{card}", job_slug="{job_slug}",
              confirm=True)


def _hero_edit() -> list[Step]:
    return [_s("darkroom_open", 30.0, image="{job_frame:1}"),
            _s("darkroom_say", 8.0, phrase="warmer")]


def flow_steps(flow: str, n: int) -> tuple[list[Step], list[Check], str]:
    """(steps, checks, expect kind) for one taxonomy bulk flow."""
    ok = lambda i: Check("envelope", step=i, target={"path": "ok", "op": "eq",  # noqa: E731
                                                     "value": True})
    count = lambda path, want: Check("extra", target={"path": path, "op": "gte",  # noqa: E731
                                                      "value": want})
    t = PER_FRAME_S * n
    if flow == "import_and_cull":
        return ([_import(n), _s("cull_propose", t, job_slug="{job_slug}"),
                 _s("cull_apply", t, job_slug="{job_slug}", proposal="{step:1:data.proposal}",
                    confirm=True)],
                [count("imported_n", n), ok(1), ok(2)], "workflow")
    if flow == "hero_then_propagate":
        return ([_import(n), *_hero_edit(), _s("preset_save", 12.0, name="{preset}",
                                               confirm=True),
                 _s("preset_apply_job", t, name="{preset}", job_slug="{job_slug}",
                    confirm=True)],
                [ok(4), count("changed_n", n - 1), count("stats_ok_n", min(10, n - 1))],
                "workflow")
    if flow == "copy_paste_history":
        return ([_import(n), *_hero_edit(),
                 _s("style_extract", 30.0, image="{job_frame:1}", name="{style}",
                    modules=["temperature"], confirm=True),
                 _s("style_apply_set", t, name="{style}", job_slug="{job_slug}", confirm=True)],
                [ok(4), count("changed_n", n - 1)], "workflow")
    if flow == "styles":
        return ([_import(n), *_hero_edit(),
                 _s("style_extract", 30.0, image="{job_frame:1}", name="{style}",
                    modules=["temperature"], confirm=True),
                 _s("style_apply", 30.0, image="{job_frame:2}", name="{style}")],
                [ok(4), count("changed_n", 1)], "workflow")
    if flow == "module_presets":
        return ([_import(n), *_hero_edit(), _s("preset_save", 12.0, name="{preset}",
                                               confirm=True),
                 _s("preset_list", 5.0),
                 _s("preset_apply", 30.0, name="{preset}", image="{job_frame:2}")],
                [ok(3), Check("envelope", step=4, target={"path": "data", "op": "exists"}),
                 ok(5), count("changed_n", 1)], "workflow")
    if flow == "batch_export_profiles":
        return ([_import(n),
                 _s("export_run", t, images="{job_frames}", preset="{export_preset}",
                    out_dir="{out_dir}/a", confirm=True),
                 _s("export_run", t, images="{job_frames}", preset="{export_preset_2}",
                    out_dir="{out_dir}/b", confirm=True),
                 _s("export_verify", 60.0, manifest="{step:1:data.manifest}")],
                [ok(1), ok(2), ok(3), count("export_files", 2 * n)], "workflow")
    if flow == "sidecar_handling":
        return ([_import(n), _s("library_verify", 60.0, job_slug="{job_slug}"),
                 _s("history_read", 10.0, image="{job_frame:1}", source="auto"),
                 _s("library_relocate", 120.0, new_root="{relocate_root}", confirm=True)],
                [ok(1), ok(2), ok(3)], "workflow")
    if flow == "outlier_handling":
        return ([_import(n), _s("outliers_list", t, reference="{job_frame:1}",
                                job_slug="{job_slug}")],
                [ok(1)], "workflow")
    if flow == "time_lapse_deflicker":
        return ([_s("darkroom_say", 8.0, phrase="deflicker the time-lapse")], [], "refuse")
    raise ValueError(f"no plan for bulk flow {flow!r}")


def generate(tax: Taxonomy) -> list[Case]:
    out: list[Case] = []
    for flow in (b["name"] for b in tax.bulk):
        for body in BODIES:
            for size, n in SIZES.items():
                steps, checks, kind = flow_steps(flow, n)
                out.append(Case(
                    id=case_id(PREFIX, flow, body, size), layer=LAYER, tier="P1", mode="tool",
                    steps=steps, expect=Expect(kind, checks),
                    body=body, frame_role="card",
                    source={"file": "taxonomy-modules.json", "bulk": flow},
                    meta={"flow": flow, "frames": n, "roadmap_key":
                          "deflicker" if flow == "time_lapse_deflicker" else None}))
    for verb in CRASH_VERBS:
        n = 120
        steps = [] if verb == "library_import" else [_import(n)]
        if verb == "job_run":
            steps.extend([*_hero_edit(), _s("preset_save", 12.0, name="{preset}", confirm=True)])
            steps.append(_s("job_run", PER_FRAME_S * n, job_slug="{job_slug}", preset="{preset}",
                            hero="{job_frame:1}", confirm=True))
        elif verb == "export_run":
            steps.append(_s("export_run", PER_FRAME_S * n, images="{job_frames}",
                            preset="{export_preset}", out_dir="{out_dir}", confirm=True))
        elif verb == "style_apply_set":
            steps.extend([*_hero_edit(), _s("style_extract", 30.0, image="{job_frame:1}",
                          name="{style}", modules=["temperature"], confirm=True)])
            steps.append(_s("style_apply_set", PER_FRAME_S * n, name="{style}",
                            job_slug="{job_slug}", confirm=True))
        else:
            steps.append(_import(n))
        out.append(Case(
            id=case_id(PREFIX, "crash_mid_job", verb), layer=LAYER, tier="P1", mode="tool",
            steps=steps, expect=Expect("workflow", [
                Check("extra", target={"path": "exactly_once", "op": "truthy"}),
                Check("extra", target={"path": "frames_verified", "op": "gte", "value": n}),
                Check("extra", target={"path": "journal_resumed", "op": "truthy"})]),
            body="olympus", frame_role="card",
            source={"file": "spec", "id": "3.3 crash-mid-job"},
            meta={"flow": "crash_mid_job", "verb": verb, "frames": n, "kill_after_frame": 50,
                  "needs_style": verb == "style_apply_set", "needs_preset": verb == "job_run"}))
    return out


EXPECTED = 9 * 2 * 2 + 4
