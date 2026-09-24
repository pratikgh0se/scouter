# Scouter Phase 3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task by task. Steps use checkbox (`- [ ]`) syntax for tracking. Do not start task N+1 until task N's tests pass. One git worktree per task where files overlap; **`git stash` is forbidden**. Tasks 2-8 each add verbs and edit the shared contract files listed under Global Constraints, so they run **sequentially**. Task 9 may run in parallel with task 8 (disjoint files). Task 10 is host-only and waits for the controller's GO.

**Goal:** Let Pratik edit one picture by conversation: phrases like "warmer", "lift the shadows", "less contrast", "stronger colour grade" become relative moves that read the current value, clamp and set it live in darktable. Moves undo as a group, the result saves as a named preset (a darktable style) that applies to another image or a whole job, and an `educative: off | brief | full` layer explains each move.

**Architecture:** Five new pure-Python core modules sit on top of the unchanged phase 1/2 engine. `core/vocab.py` holds the module-parameter table, with status derived from `dtbridge.ACTION_PATHS` and `xmp.LAYOUTS`. `core/phrases.py` is the deterministic phrase→Move resolver. `core/moves.py` holds `darkroom.get`, `darkroom.adjust` and `darkroom.say`, which read then call `darkroom.set_param.__wrapped__`. `core/explain.py` is the educative phrasing layer, with a session set in job state. `core/presets.py` holds save/list/apply/apply_job, over `style.extract`/`apply`/`apply_set`. There is one new Lua bridge method (`darkroom.apply_style`: apply inside a view bounce). Every capability is a registered verb, reachable from Python, the CLI and MCP.

**Tech Stack:** Python 3.10+, stdlib only (`dataclasses`, `re`, `difflib`, `contextvars`, `json`, `hashlib`, `shutil`, `unittest`) plus `mcp>=2,<3` for the adapter. Lua 5.4 in darktable 5.4.1 (Lua API 9.6.0). Tests: `uv run --extra dev python -m pytest tests -q`. Baseline on `main` 33ebaef: **1164 passed, 18 skipped**.

**Spec:** `docs/superpowers/specs/2026-09-24-scouter-phase3-design.md`. Also read `docs/superpowers/specs/2026-09-14-scouter-phase2-design.md` (what exists), `.superpowers/sdd/2026-09-14-scouter-phase2/progress.md` (rulings R1-R22), and `README.md` §"Honest limits" and §"Honest limits, phase 2".

---

## Global Constraints

Binding on every task. Copied verbatim from the spec (§3.1, §17, §18) and the phase brief. Do not "improve" them mid-task.

- **stdlib only.** Dependencies are the stdlib plus `mcp>=2,<3` (MCP adapter only). **No new dependencies**: nothing is added to `pyproject.toml` and nothing to `uv.lock`.
- **Tests never touch real darktable, `~/.config`, the vault or the network.** Only `tests/test_host_phase3.py` (task 10) may launch darktable, gated by `SCOUTER_HOST_TESTS=1` and the binary's presence.
- **conftest pins.** `tests/conftest.py` is **not edited**. Its pinned paths (`OBSIDIAN_VAULT_PATH`, `SCOUTER_CACHE`, `SCOUTER_CONFIG`, `SCOUTER_DARKTABLE_LIBRARY_DB`, `XDG_CACHE_HOME`, `DARKTABLE_MCP_PLUGIN_PATH`) and the `SCOUTER_DARKTABLE_CLI` host guard stay exactly as they are. A test that needs its own tree uses `tests.conftest.allowed_tree`.
- **CLI/MCP parity.** Every verb is a plain typed function in `scouter/core/`, registered in `VERB_REGISTRY`, reachable from the CLI and MCP with the same name, arguments and result shape. Nothing is CLI-only or MCP-only.
- **One metrics event per top-level verb.** Every new verb is `@metrics.instrumented("<id>")`. Nested verbs are called through `.__wrapped__`.
- **Dry runs** make no darktable call that writes, opens, imports or moves the view.
- **Ruling 30:** a darkroom write refuses when `darkroom.current` differs from the job's opened image (`darkroom._require_job_image`).
- **Whitelist rule:** `dtbridge.ACTION_PATHS` gains a pair only from a host probe that answered non-`nan` (task 10). Production code never passes `probe_unpinned=True`.
- **The engine does not change.** `darkroom.set_param`, `darkroom.nudge`, the commit path, the no-op shortcut and the undo tiers keep their behaviour. `undo_last` is only refactored (task 6) and gains a preset refusal (task 8).
- **Honest limits in the result:** no guessed value, no phantom success, no invented capability. Refusals are typed (`usage` 2, `value_unknown` 20, `not_pinned` 21, `partial` 9).
- **Never overwrite blind:** files, presets, fixtures, goldens.
- **Shared contract files.** Any task that adds or changes a verb also edits, in the same commit: `tests/scouter_smoke_args.py` (`SMOKE_ARGS` entry), `tests/test_layer_parity.py` (`EXPECTED_VERBS`), `scouter/mcp/schemas.py` (tool spec), and `tests/fixtures/scouter_mcp_golden.json`. Regenerate the golden with `uv run --extra dev python -c "from scouter.mcp import schemas; import json; print(json.dumps(sorted(schemas.TOOL_SPECS, key=lambda s: s['name']), indent=2, sort_keys=True), end='')" > tests/fixtures/scouter_mcp_golden.json` and name the added tools in the commit message.
- **Goldens are never rewritten by a test.** On mismatch the test fails with a diff and the regeneration command. On first creation it writes the file and fails, so a human commits it.
- **Units are internal** in every argument and result field. Display units (`%`, `°`) appear only in `summary`, `explain`, and `vocab.list`'s display columns (P3-R1).
- Commits end with `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>` (or the implementer's own model line). Never push.

## Review Focus

Input classes the spec implies that are most likely to bite. Each has a test in the owning task.

1. **A phrase with a direction word plus a negative "by N"** ("warmer by -300k") must refuse, not double-negate. Test in task 3: `test_negative_by_amount_is_refused`.
2. **A relative move at a clamp limit** must be a zero-write no-op, not a set to the same value. Test in task 5: `test_adjust_at_the_limit_sends_nothing`.
3. **A hue move past 360°** must wrap, not clamp or go negative. Test in task 5: `test_hue_wraps_modulo_360`.
4. **An MCP session across seconds** must keep one journal, so undo works in conversation. Test in task 9: `test_session_job_is_pinned_once_and_respects_an_explicit_one`.
5. **`preset.save` right after `undo_last`** must not include an undone module in the default module list. Test in task 7: `test_session_modules_skip_undone_and_failed_writes`.

---

## File map

**Create:** `scouter/core/vocab.py`, `scouter/core/phrases.py`, `scouter/core/explain.py`, `scouter/core/moves.py`, `scouter/core/presets.py`, `tests/host_support.py`, `tests/test_phase3_foundations.py`, `tests/test_vocab.py`, `tests/test_phrases.py`, `tests/test_explain.py`, `tests/test_moves.py`, `tests/test_say.py`, `tests/test_presets.py`, `tests/test_presets_apply.py`, `tests/test_phase3_closures.py`, `tests/test_host_phase3.py`, `tests/fixtures/vocab_golden.json`, `tests/fixtures/phrases_golden.json`, `tests/fixtures/explain_golden.json`.

**Modify:** `scouter/config.py`, `scouter/errors.py`, `scouter/types.py`, `scouter/core/journal.py`, `scouter/registry.py`, `scouter/mcp/schemas.py`, `scouter/mcp/server.py`, `scouter/core/darkroom.py` (undo only), `scouter/core/wb.py` (version check), `scouter/__init__.py`, `darktable_mcp/lua/darktable_mcp.lua`, `darktable_mcp/bridge/client.py`, `tests/lua/test_dispatcher.lua`, `tests/test_host_phase2.py` (import from host_support), `tests/scouter_smoke_args.py`, `tests/test_layer_parity.py`, `tests/fixtures/scouter_mcp_golden.json`, `README.md`, `docs/measurements.md` (task 10). After the host run: `scouter/core/dtbridge.py` (`ACTION_PATHS`), `scouter/core/xmp.py` (`LAYOUTS`).

---

### Task 1: Foundations — `educative` config key, two error classes, the result types, journal groups

**Files:**
- Modify: `scouter/config.py` (Config field, `TOP_KEYS`, `load`, `init` starter)
- Modify: `scouter/errors.py` (`ValueUnknown`, `NotPinned`, `EXIT_CODES`, `ERROR_CLASSES`)
- Modify: `scouter/types.py` (seven new results, `UndoResult.undone_group`)
- Modify: `scouter/core/journal.py` (`GROUP` contextvar, `Entry.group`)
- Test: `tests/test_phase3_foundations.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `scouter.config.EDUCATIVE_LEVELS: tuple[str, ...] = ("off", "brief", "full")`; `Config.educative: str = "brief"`.
  - `scouter.errors.ValueUnknown` (code `value_unknown`, exit 20), `scouter.errors.NotPinned` (code `not_pinned`, exit 21).
  - `scouter.types.VocabResult`, `ResolveResult`, `ValueResult`, `AdjustResult`, `SayResult`, `PresetResult`, `PresetListResult` (fields below); `UndoResult.undone_group: list[dict]`.
  - `scouter.core.journal.GROUP: contextvars.ContextVar[str | None]`; `Entry.group: str | None = None`, stamped by `journal.begin` from `GROUP.get()`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_phase3_foundations.py
"""Phase 3 task 1: the educative key, the two new error classes, journal groups."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scouter import config, errors
from scouter.core import journal
from scouter.errors import UsageError
from scouter.types import AdjustResult, UndoResult
from tests.scouter_fakes import TempCache


class EducativeConfigTest(unittest.TestCase):
    def _load(self, body: dict) -> config.Config:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "config.json"
            p.write_text(json.dumps(body))
            return config.load(str(p))

    def test_default_is_brief_with_no_file(self) -> None:
        self.assertEqual(config.load("/nonexistent/scouter/config.json").educative, "brief")

    def test_each_level_loads(self) -> None:
        for level in ("off", "brief", "full"):
            with self.subTest(level=level):
                self.assertEqual(self._load({"educative": level}).educative, level)

    def test_an_unknown_level_is_a_usage_error_naming_the_three(self) -> None:
        with self.assertRaises(UsageError) as cm:
            self._load({"educative": "verbose"})
        self.assertIn("off, brief, full", str(cm.exception))

    def test_starter_file_carries_the_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "config.json"
            config.init(str(target), confirm=True)
            self.assertEqual(json.loads(target.read_text())["educative"], "brief")


class NewErrorClassesTest(unittest.TestCase):
    def test_codes_and_exit_codes(self) -> None:
        self.assertEqual(errors.ValueUnknown("x").code, "value_unknown")
        self.assertEqual(errors.NotPinned("x").code, "not_pinned")
        self.assertEqual(errors.EXIT_CODES["value_unknown"], 20)
        self.assertEqual(errors.EXIT_CODES["not_pinned"], 21)
        self.assertIn(errors.ValueUnknown, errors.ERROR_CLASSES)
        self.assertIn(errors.NotPinned, errors.ERROR_CLASSES)
        self.assertEqual(errors.exit_code_for("error:not_pinned"), 21)

    def test_both_carry_a_fix(self) -> None:
        self.assertIn("darkroom set", errors.ValueUnknown.fix)
        self.assertIn("host", errors.NotPinned.fix)


class JournalGroupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = TempCache()
        self.cache.start()

    def tearDown(self) -> None:
        self.cache.stop()

    def _begin(self):
        return journal.begin(
            "job-test", verb="darkroom.set", image_id=1, path="/x.orf",
            module="exposure", param="exposure", instance=0, before={"value": 1.0},
        )

    def test_group_defaults_to_none(self) -> None:
        self.assertIsNone(self._begin().group)

    def test_group_is_stamped_from_the_contextvar_and_reset(self) -> None:
        token = journal.GROUP.set("say-7")
        try:
            entry = self._begin()
        finally:
            journal.GROUP.reset(token)
        self.assertEqual(entry.group, "say-7")
        self.assertEqual(journal.read_all("job-test")[-1].group, "say-7")
        self.assertIsNone(self._begin().group)

    def test_an_old_journal_line_without_group_still_loads(self) -> None:
        path = journal.paths.journal_path("job-test")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "seq": 1, "ts": "2026-09-14T00:00:00+00:00", "verb": "darkroom.set",
            "image_id": 1, "path": "/x.orf", "module": "exposure", "param": "exposure",
            "instance": 0, "before": {}, "after": {"value": 1.0}, "undone": False,
            "outcome": None, "commit": True, "note": None,
        }) + "\n")
        self.assertIsNone(journal.read_all("job-test")[0].group)


class ResultTypesTest(unittest.TestCase):
    def test_adjust_result_defaults_serialise(self) -> None:
        d = AdjustResult(module="exposure", param="exposure").to_dict()
        for key in ("delta", "previous", "target", "applied", "clamped", "source",
                    "committed", "noop", "summary", "explain", "unit"):
            self.assertIn(key, d)

    def test_undo_result_gains_undone_group(self) -> None:
        self.assertEqual(UndoResult().to_dict()["undone_group"], [])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --extra dev python -m pytest tests/test_phase3_foundations.py -q`
Expected: FAIL (`ImportError: cannot import name 'AdjustResult'`).

- [ ] **Step 3: Implement**

`scouter/config.py`, after `CONFIG_VERSION = 1`:

```python
#: Row 346: the educative mode's three levels (design §7.1).
EDUCATIVE_LEVELS: tuple[str, ...] = ("off", "brief", "full")
```

In `class Config`, after `stats: ...`:

```python
    educative: str = "brief"
```

Change `TOP_KEYS`:

```python
TOP_KEYS = {"version", "library_root", "cameras", "educative", "_comment", *SECTIONS}
```

In `load`, after the `cameras` block and before the `for name, (attr, klass) in SECTIONS.items():` loop:

```python
    if "educative" in raw:
        level = raw["educative"]
        if level not in EDUCATIVE_LEVELS:
            raise UsageError(
                f"`educative` must be one of {', '.join(EDUCATIVE_LEVELS)}, got {level!r}",
                detail={"config": str(cfg_path), "educative": level},
            )
        kwargs["educative"] = level
```

In `init`'s `starter` dict, after `"cameras": ...`:

```python
        "educative": "brief",
```

`scouter/errors.py`, after `ExportAllFailed`:

```python
class ValueUnknown(ScouterError):
    """Phase 3 §15: a relative move needs a current value Scouter cannot read."""

    code = "value_unknown"
    fix = (
        "Scouter cannot read this parameter's current value (see detail.reason). "
        "Set it absolutely with `scouter darkroom set <module> <param> <value>`; "
        "relative moves on it need its XMP slot pinned by a host probe (or, for "
        "white balance, `scouter wb calibrate`)."
    )


class NotPinned(ScouterError):
    """Phase 3 §15: a vocabulary row with no pinned darktable action path."""

    code = "not_pinned"
    fix = (
        "this parameter has no pinned darktable action path; pinning is a host-probe "
        "job (docs/superpowers/specs/2026-09-24-scouter-phase3-design.md §10.1). "
        "`scouter vocab list` shows which words work today."
    )
```

Add `"value_unknown": 20,` and `"not_pinned": 21,` to `EXIT_CODES` after `"no_sidecar": 19,`. Append `ValueUnknown, NotPinned,` to `ERROR_CLASSES`.

`scouter/types.py`: add `undone_group: list[dict] = field(default_factory=list)` to `UndoResult`. Then append:

```python
@dataclass
class VocabResult(Payload):
    rows: list[dict] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)


@dataclass
class ResolveResult(Payload):
    phrase: str = ""
    normalised: str = ""
    key: str = ""
    intensity: float = 1.0
    explicit: bool = False
    moves: list[dict] = field(default_factory=list)


@dataclass
class ValueResult(Payload):
    module: str = ""
    param: str = ""
    instance: int = 0
    value: float | None = None
    display_value: float | None = None
    unit: str = ""
    source: str | None = None
    enabled: bool | None = None
    fresh: bool = False
    status: str = ""


@dataclass
class AdjustResult(Payload):
    module: str = ""
    param: str = ""
    instance: int = 0
    unit: str = ""
    delta: float | None = None
    previous: float | None = None
    target: float | None = None
    applied: float | None = None
    clamped: bool = False
    source: str | None = None
    committed: bool = False
    noop: bool = False
    summary: str = ""
    explain: list[str] = field(default_factory=list)


@dataclass
class SayResult(Payload):
    phrase: str = ""
    key: str = ""
    intensity: float = 1.0
    group: str | None = None
    moves: list[dict] = field(default_factory=list)
    refused: list[dict] = field(default_factory=list)
    summary: str = ""
    explain: list[str] = field(default_factory=list)


@dataclass
class PresetResult(Payload):
    name: str = ""
    action: str = ""  # "save" | "apply"
    modules: list[str] = field(default_factory=list)
    dtstyle_path: str | None = None
    memory_path: str | None = None
    replaced: bool = False
    applied_to: str | None = None  # "darkroom" | "library"
    history_end_before: int | None = None
    history_end_after: int | None = None
    explain: list[str] = field(default_factory=list)


@dataclass
class PresetListResult(Payload):
    presets: list[dict] = field(default_factory=list)
    darktable_only: list[str] = field(default_factory=list)
    missing_in_darktable: list[str] = field(default_factory=list)
```

`scouter/core/journal.py`: add `import contextvars` and, below the imports:

```python
#: Phase 3 §6.4: the move group every journal entry begun inside it belongs to.
#: `darkroom.say` sets it around its moves; no write verb's signature changes.
GROUP: contextvars.ContextVar[str | None] = contextvars.ContextVar("scouter_move_group", default=None)
```

Add `group: str | None = None` as the last field of `Entry`. In `begin`, pass `group=GROUP.get()` into `Entry(...)`. `read_all` builds `Entry(**data)`, and a missing key falls back to the default. Confirm this by reading `read_all`. If it filters keys, add `"group"` to that filter.

- [ ] **Step 4: Run the tests to verify they pass, then the full suite**

Run: `uv run --extra dev python -m pytest tests/test_phase3_foundations.py -q && uv run --extra dev python -m pytest tests -q`
Expected: `11 passed`, then the full suite `0 failed`. The count grows by 11 over the baseline 1164. `tests/test_paths_errors.py` still passes: the new codes are unique.

- [ ] **Step 5: Commit**

```bash
git add scouter/config.py scouter/errors.py scouter/types.py scouter/core/journal.py tests/test_phase3_foundations.py
git commit -m "phase 3 task 1: educative config key, value_unknown/not_pinned, result types, journal groups"
```

**Validation contract (done when):**
- `uv run --extra dev python -m pytest tests/test_phase3_foundations.py -q` → last line matches `11 passed`.
- `uv run --extra dev python -m pytest tests -q` → last line contains `passed` and no `failed`.
- `uv run --extra dev python -c "from scouter.errors import EXIT_CODES as E; print(E['value_unknown'], E['not_pinned'])"` → `20 21`.
- `uv run --extra dev python -c "from scouter.config import Config; print(Config().educative)"` → `brief`.
- `git diff main --stat -- tests/conftest.py pyproject.toml uv.lock` → empty output.

---

### Task 2: The vocabulary table and `vocab.list`

**Files:**
- Create: `scouter/core/vocab.py`
- Modify: `scouter/registry.py` (`_CORE_MODULES` += `"scouter.core.vocab"`)
- Modify: `scouter/mcp/schemas.py` (tool `vocab_list`) + the shared contract files
- Test: `tests/test_vocab.py`, `tests/fixtures/vocab_golden.json`

**Interfaces:**
- Consumes: `dtbridge.ACTION_PATHS`, `xmp.LAYOUTS`, `types.VocabResult` (task 1).
- Produces: `ParamSpec` (spec §4.1), `PARAMS: tuple[ParamSpec, ...]`, `STATUSES`, `CONCEPT_IDS: tuple[str, ...]`, `spec(module, param) -> ParamSpec | None`, `status(s) -> str`, `xmp_named(module, param) -> bool`, `clamp(s, value) -> tuple[float, bool]`, `to_display(s, value) -> float`, `closest_live(s) -> ParamSpec | None`, `table() -> list[dict]`, verb `vocab.list` → `list_vocab(module: str | None = None, *, job=None) -> VocabResult`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_vocab.py
"""Phase 3 task 2: the vocabulary table, its derived status, and its golden."""

from __future__ import annotations

import difflib
import json
import unittest
from pathlib import Path
from unittest import mock

from scouter.core import dtbridge, vocab, xmp
from scouter.errors import UsageError

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "vocab_golden.json"
REGEN = (
    "uv run --extra dev python -c \"from scouter.core import vocab; import json; "
    "print(json.dumps(vocab.table(), indent=2, sort_keys=True))\" > tests/fixtures/vocab_golden.json"
)


class VocabGoldenTest(unittest.TestCase):
    maxDiff = None

    def test_the_table_matches_the_golden(self) -> None:
        actual = json.dumps(vocab.table(), indent=2, sort_keys=True) + "\n"
        if not GOLDEN.exists():
            GOLDEN.write_text(actual)
            self.fail(f"golden created at {GOLDEN}; review and commit it")
        expected = GOLDEN.read_text()
        if expected != actual:
            diff = "\n".join(difflib.unified_diff(expected.splitlines(), actual.splitlines(),
                                                  "golden", "actual", lineterm=""))
            self.fail(f"vocabulary changed; if intended, regenerate with:\n{REGEN}\n{diff}")


class VocabShapeTest(unittest.TestCase):
    def test_every_row_is_complete_and_sane(self) -> None:
        seen = set()
        for s in vocab.PARAMS:
            with self.subTest(row=f"{s.module}.{s.param}"):
                self.assertNotIn((s.module, s.param), seen)
                seen.add((s.module, s.param))
                self.assertIn(s.unit, ("EV", "K", "ratio", "%", "°", "—"))
                self.assertIn(s.value_source, ("xmp", "wb_table", "none"))
                self.assertIn(s.concept, vocab.CONCEPT_IDS)
                if s.reachable:
                    self.assertLess(s.lo, s.hi)
                    self.assertGreater(s.step, 0)
                    self.assertLessEqual(s.step, (s.hi - s.lo) / 4)
                    self.assertTrue(s.why and s.watch)
                if (s.module, s.param) not in dtbridge.ACTION_PATHS and s.reachable:
                    self.assertTrue(s.candidate_paths, "an unpinned reachable row needs candidates")
                self.assertEqual(s.display_scale, 100.0 if s.unit == "%" else 1.0)

    def test_initial_statuses_match_the_spec_table(self) -> None:
        live = {f"{s.module}.{s.param}" for s in vocab.PARAMS if vocab.status(s) == "live"}
        self.assertEqual(live, {
            "exposure.exposure", "exposure.black", "sigmoid.contrast",
            "temperature.temperature", "colorbalancergb.contrast",
        })
        absolute = {f"{s.module}.{s.param}" for s in vocab.PARAMS if vocab.status(s) == "absolute_only"}
        self.assertEqual(absolute, {
            "sigmoid.skew", "temperature.tint", "colorbalancergb.global_saturation",
        })
        self.assertEqual(vocab.status(vocab.spec("colorzones", "curves")), "unreachable")

    def test_status_follows_a_new_path_pin_then_a_slot_pin(self) -> None:
        s = vocab.spec("colorbalancergb", "global_vibrance")
        self.assertEqual(vocab.status(s), "candidate")
        with mock.patch.dict(dtbridge.ACTION_PATHS,
                             {("colorbalancergb", "global_vibrance"): "iop/colorbalancergb/global vibrance"}):
            self.assertEqual(vocab.status(s), "absolute_only")
            layout = dict(xmp.LAYOUTS[("colorbalancergb", 5)])
            layout["named_slots"] = {**layout["named_slots"], 7: "global_vibrance"}
            with mock.patch.dict(xmp.LAYOUTS, {("colorbalancergb", 5): layout}):
                self.assertEqual(vocab.status(s), "live")

    def test_clamp_and_wrap(self) -> None:
        exp = vocab.spec("exposure", "exposure")
        self.assertEqual(vocab.clamp(exp, 9.0), (4.0, True))
        self.assertEqual(vocab.clamp(exp, 1.0), (1.0, False))
        hue = vocab.spec("colorbalancergb", "shadows_hue")
        self.assertEqual(vocab.clamp(hue, 370.0), (10.0, False))
        self.assertEqual(vocab.clamp(hue, -20.0), (340.0, False))

    def test_display_units(self) -> None:
        sat = vocab.spec("colorbalancergb", "global_saturation")
        self.assertAlmostEqual(vocab.to_display(sat, 0.05), 5.0)

    def test_closest_live_prefers_the_same_concept(self) -> None:
        alt = vocab.closest_live(vocab.spec("filmicrgb", "contrast"))
        self.assertEqual((alt.module, alt.param), ("sigmoid", "contrast"))


class VocabListVerbTest(unittest.TestCase):
    def test_list_counts_and_filter(self) -> None:
        result = vocab.list_vocab.__wrapped__(job="job-test")
        self.assertEqual(sum(result.counts.values()), len(vocab.PARAMS))
        only = vocab.list_vocab.__wrapped__("sigmoid", job="job-test")
        self.assertEqual({r["module"] for r in only.rows}, {"sigmoid"})

    def test_unknown_module_is_usage(self) -> None:
        with self.assertRaises(UsageError):
            vocab.list_vocab.__wrapped__("nosuchmodule", job="job-test")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --extra dev python -m pytest tests/test_vocab.py -q`
Expected: FAIL (`ImportError: cannot import name 'vocab'`).

- [ ] **Step 3: Implement `scouter/core/vocab.py`**

```python
"""The conversational vocabulary (phase 3 design §4).

One row per module parameter Scouter can move by words: unit, Scouter's own
conservative clamp, one conversational notch, where the current value is read
from, and the explanation text. `status` is DERIVED from the pins
(`dtbridge.ACTION_PATHS`, `xmp.LAYOUTS`), never stored: when a host probe pins
a path or an XMP slot, the row upgrades itself and the golden shows it.

Units are darktable's INTERNAL units (P3-R1): what `dt.gui.action(..., "set")`
takes. `%` rows carry `display_scale = 100` (the GUI shows 0.05 as 5 %).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from scouter import metrics
from scouter.core import dtbridge, xmp
from scouter.errors import UsageError
from scouter.paths import current_job
from scouter.registry import ArgSpec, Verb, register
from scouter.types import VocabResult

STATUSES = ("live", "absolute_only", "candidate", "unreachable")
CONCEPT_IDS = (
    "exposure", "tone_mapper", "white_balance", "color_balance", "chroma_saturation",
    "grading", "black_level", "color_zones", "presets",
)


@dataclass(frozen=True)
class ParamSpec:
    module: str
    param: str
    ui: str
    unit: str
    display_scale: float
    lo: float
    hi: float
    step: float
    value_source: str
    neutral: float | None
    wraps: bool
    concept: str
    why: str
    watch: str
    candidate_paths: tuple[str, ...] = ()
    reachable: bool = True
    note: str = ""


def _cb(param: str, ui: str, unit: str, lo: float, hi: float, step: float, concept: str,
        why: str, watch: str, candidates: tuple[str, ...] = (), *, wraps: bool = False,
        neutral: float | None = 0.0) -> ParamSpec:
    """A colour balance rgb row (`%` rows get display_scale 100)."""
    return ParamSpec(
        module="colorbalancergb", param=param, ui=f"color balance rgb › {ui}", unit=unit,
        display_scale=100.0 if unit == "%" else 1.0, lo=lo, hi=hi, step=step,
        value_source="xmp", neutral=None if wraps else neutral, wraps=wraps,
        concept=concept, why=why, watch=watch, candidate_paths=candidates,
    )


def _zone(zone: str, section: str, label: str) -> tuple[ParamSpec, ParamSpec, ParamSpec]:
    base = f"iop/colorbalancergb/{section}"
    flat = f"iop/colorbalancergb/{label}"
    return (
        _cb(f"{zone}_luminance", f"4 ways › {section} › luminance", "%", -0.25, 0.25, 0.02,
            "color_balance", f"lifts or lowers only the {label}",
            "noise in the darkest areas" if zone == "shadows" else
            ("clipping in the brightest areas" if zone == "highlights" else "skin"),
            (f"{base}/luminance", f"{flat} luminance")),
        _cb(f"{zone}_hue", f"4 ways › {section} › hue", "°", 0.0, 360.0, 10.0,
            "grading", f"picks the tint colour for the {label}",
            "neutral greys in that range", (f"{base}/hue", f"{flat} hue"), wraps=True),
        _cb(f"{zone}_chroma", f"4 ways › {section} › chroma", "%", 0.0, 0.3, 0.02,
            "grading", f"sets how strong the {label} tint is",
            "greys in that range turning coloured", (f"{base}/chroma", f"{flat} chroma")),
    )


PARAMS: tuple[ParamSpec, ...] = (
    ParamSpec("exposure", "exposure", "exposure › exposure", "EV", 1.0, -3.0, 4.0, 0.3,
              "xmp", None, False, "exposure",
              "brightens or darkens the whole frame evenly, before any tone mapping",
              "the highlights for clipping and the shadows for noise"),
    ParamSpec("exposure", "black", "exposure › black level correction", "ratio", 1.0,
              -0.1, 0.1, 0.005, "xmp", None, False, "black_level",
              "moves the point that renders as pure black",
              "the darkest areas: a few thousandths is a visible change"),
    ParamSpec("sigmoid", "contrast", "sigmoid › contrast", "ratio", 1.0, 0.5, 3.0, 0.1,
              "xmp", None, False, "tone_mapper",
              "steepens or flattens the middle of the tone curve",
              "shadow and highlight detail at the two ends"),
    ParamSpec("sigmoid", "skew", "sigmoid › skew", "ratio", 1.0, -1.0, 1.0, 0.1,
              "xmp", None, False, "tone_mapper",
              "shifts where the tone curve spends its contrast, toward shadows or highlights",
              "whether the midtones still sit where you want them"),
    ParamSpec("temperature", "temperature", "white balance › temperature", "K", 1.0,
              2000.0, 12000.0, 300.0, "wb_table", None, False, "white_balance",
              "tells darktable the light was bluer or redder, so the picture turns warmer or cooler",
              "neutral greys and skin"),
    ParamSpec("temperature", "tint", "white balance › tint", "ratio", 1.0, 0.5, 2.0, 0.05,
              "none", None, False, "white_balance",
              "trades green against magenta in the light", "skin and foliage"),
    _cb("contrast", "master › contrast", "%", -0.5, 0.5, 0.05, "color_balance",
        "adds or removes contrast around middle grey, after the tone mapper",
        "that it does not fight the sigmoid contrast"),
    _cb("global_saturation", "master › global saturation", "%", -0.5, 0.5, 0.05,
        "chroma_saturation",
        "changes colourfulness and darkens strong colours slightly, which reads natural",
        "skin and skies for over-cooked colour"),
    _cb("global_vibrance", "master › global vibrance", "%", -0.5, 0.5, 0.05,
        "chroma_saturation", "boosts the least colourful pixels most",
        "already-saturated areas, which should barely move",
        ("iop/colorbalancergb/global vibrance",)),
    _cb("global_chroma", "master › global chroma", "%", -0.5, 0.5, 0.05,
        "chroma_saturation", "changes colourfulness at constant brightness",
        "bright saturated colours going neon", ("iop/colorbalancergb/global chroma",)),
    *_zone("shadows", "shadows lift", "shadows"),
    *_zone("midtones", "power", "midtones"),
    *_zone("highlights", "highlights gain", "highlights"),
    ParamSpec("filmicrgb", "white_relative_exposure", "filmic rgb › scene › white relative exposure",
              "EV", 1.0, 2.0, 10.0, 0.25, "xmp", None, False, "tone_mapper",
              "sets which scene brightness maps to pure white", "clipped highlights",
              ("iop/filmicrgb/white relative exposure",)),
    ParamSpec("filmicrgb", "black_relative_exposure", "filmic rgb › scene › black relative exposure",
              "EV", 1.0, -14.0, -3.0, 0.25, "xmp", None, False, "tone_mapper",
              "sets which scene brightness maps to pure black", "blocked shadows",
              ("iop/filmicrgb/black relative exposure",)),
    ParamSpec("filmicrgb", "contrast", "filmic rgb › look › contrast", "ratio", 1.0, 0.5, 3.0,
              0.1, "xmp", None, False, "tone_mapper",
              "steepens or flattens the middle of the tone curve",
              "shadow and highlight detail at the two ends", ("iop/filmicrgb/contrast",)),
    ParamSpec("colorzones", "strength", "color zones › mix", "%", 100.0, 0.0, 2.0, 0.1,
              "xmp", None, False, "color_zones",
              "scales every colour-zone adjustment at once", "the zones you set in darktable",
              ("iop/colorzones/mix",)),
    ParamSpec("colorzones", "curves", "color zones › (zone curves)", "—", 1.0, 0.0, 0.0, 0.0,
              "none", None, False, "color_zones", "", "", (), False,
              "zones are curve nodes, not sliders: no dt.gui.action sets a zone value; "
              "use color balance rgb's per-zone sliders (4 ways) instead"),
)

_BY_KEY = {(s.module, s.param): s for s in PARAMS}


def spec(module: str, param: str) -> ParamSpec | None:
    return _BY_KEY.get((module, param))


def xmp_named(module: str, param: str) -> bool:
    """Is `param` a named field of any pinned XMP layout for `module`?"""
    for (operation, _version), layout in xmp.LAYOUTS.items():
        if operation != module:
            continue
        names = {f[0] for f in layout.get("fields", ())}
        names |= set(layout.get("named_slots", {}).values())
        if param in names:
            return True
    return False


def status(s: ParamSpec) -> str:
    if not s.reachable:
        return "unreachable"
    if (s.module, s.param) not in dtbridge.ACTION_PATHS:
        return "candidate"
    if s.value_source == "wb_table":
        return "live"
    if s.value_source == "xmp" and xmp_named(s.module, s.param):
        return "live"
    return "absolute_only"


def clamp(s: ParamSpec, value: float) -> tuple[float, bool]:
    """Scouter's safe range (P3-R15). Hue rows wrap modulo 360 instead."""
    if s.wraps:
        return value % 360.0, False
    if value < s.lo:
        return s.lo, True
    if value > s.hi:
        return s.hi, True
    return value, False


def to_display(s: ParamSpec, value: float) -> float:
    return value * s.display_scale


def closest_live(s: ParamSpec) -> ParamSpec | None:
    """The first `live` row with the same concept, else the same module, else None."""
    for pool in (
        [p for p in PARAMS if p.concept == s.concept],
        [p for p in PARAMS if p.module == s.module],
    ):
        for p in pool:
            if p is not s and status(p) == "live":
                return p
    return None


def table() -> list[dict]:
    rows = []
    for s in PARAMS:
        row = asdict(s)
        row["candidate_paths"] = list(s.candidate_paths)
        row["status"] = status(s)
        row["pinned_path"] = dtbridge.ACTION_PATHS.get((s.module, s.param))
        rows.append(row)
    return rows


@metrics.instrumented("vocab.list")
def list_vocab(module: str | None = None, *, job: str | None = None) -> VocabResult:
    """Every word Scouter can move, with its status today. No bridge calls."""
    job = job or current_job()
    rows = table()
    if module is not None:
        known = sorted({r["module"] for r in rows})
        if module not in known:
            raise UsageError(
                f"no vocabulary rows for module {module!r}",
                detail={"module": module, "known": known},
            )
        rows = [r for r in rows if r["module"] == module]
    counts = {name: sum(1 for r in rows if r["status"] == name) for name in STATUSES}
    return VocabResult(rows=rows, counts=counts)


register(
    Verb(
        id="vocab.list",
        group="vocab",
        fn=list_vocab,
        args=(ArgSpec("module", "str", default=None, help="only this module's rows"),),
        writes=False,
        gated=False,
        summary="The conversational vocabulary with each word's status today.",
    )
)
```

Add `"scouter.core.vocab",` to `_CORE_MODULES` in `scouter/registry.py`, before `"scouter.metrics"`.

Add to `TOOL_SPECS` in `scouter/mcp/schemas.py`:

```python
    {
        "name": "vocab_list",
        "description": "The conversational vocabulary: every module parameter Scouter can move "
        "by words, with unit, safe range, step, and status (live, absolute_only, candidate, "
        "unreachable). Read once per session.",
        "inputSchema": _schema(
            {"module": {"type": "string", "description": "only this module's rows, e.g. colorbalancergb"}},
            [],
        ),
    },
```

Contract files: `SMOKE_ARGS["vocab.list"] = {}`, `EXPECTED_VERBS` += `"vocab.list"`, regenerate the MCP golden.

- [ ] **Step 4: Create the golden, review it, re-run**

Run: `uv run --extra dev python -m pytest tests/test_vocab.py -q` (the first run creates `tests/fixtures/vocab_golden.json` and fails). Open the file and check the 24 rows against spec §4.3's table (units, clamps, steps, statuses). Then re-run it and the full suite.
Expected: `9 passed`, then the full suite `0 failed`.

- [ ] **Step 5: Commit**

```bash
git add scouter/core/vocab.py scouter/registry.py scouter/mcp/schemas.py tests/test_vocab.py tests/fixtures/vocab_golden.json tests/scouter_smoke_args.py tests/test_layer_parity.py tests/fixtures/scouter_mcp_golden.json
git commit -m "phase 3 task 2: vocabulary table with derived status, vocab.list (MCP golden: +vocab_list)"
```

**Validation contract (done when):**
- `uv run --extra dev python -m pytest tests/test_vocab.py tests/test_layer_parity.py tests/test_scouter_mcp_schemas.py tests/test_metrics.py -q` → no `failed`.
- `uv run --extra dev python -m scouter vocab list | python3 -c "import json,sys; d=json.load(sys.stdin)['data']; print(len(d['rows']), d['counts'])"` → `24 {'live': 5, 'absolute_only': 3, 'candidate': 15, 'unreachable': 1}`.
- `uv run --extra dev python -m scouter vocab list --module nosuch; echo "exit=$?"` → the last line is `exit=2`.
- `grep -rn "probe_unpinned=True" scouter/` → no output.
- `git diff main -- scouter/core/dtbridge.py` → empty output (the whitelist is untouched).

---

### Task 3: Phrases — the deterministic resolver and `vocab.resolve`

**Files:**
- Create: `scouter/core/phrases.py`
- Modify: `scouter/registry.py` (`_CORE_MODULES` += `"scouter.core.phrases"`), `scouter/mcp/schemas.py` (`vocab_resolve`, plus the `PHRASE`/`AMOUNT` fragments) + contract files
- Test: `tests/test_phrases.py`, `tests/fixtures/phrases_golden.json`

**Interfaces:**
- Consumes: `vocab.spec`, `vocab.status`, `dtbridge.ACTION_PATHS`, `types.ResolveResult`.
- Produces: `Move(module, param, kind, notches, value)`, `Phrase(key, synonyms, moves)`, `Resolution(phrase, normalised, key, intensity, explicit, moves)`, `PHRASES`, `normalise(text) -> str`, `resolve(text, amount=None) -> Resolution`, `resolution_dict(res) -> dict`, `move_view(move) -> dict` (adds `delta`, `unit`, `status`, `pinned_path`; `@tone` shows sigmoid's with `routes`), verb `vocab.resolve` → `resolve_verb(phrase, *, amount=None, job=None) -> ResolveResult`. `TONE = "@tone"`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_phrases.py
"""Phase 3 task 3: phrase -> moves, deterministic, pinned by a golden."""

from __future__ import annotations

import difflib
import json
import unittest
from pathlib import Path

from scouter.core import phrases, vocab
from scouter.errors import UsageError

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "phrases_golden.json"

#: 40 fixed inputs: canonical forms, intensity words, explicit amounts, noise.
INPUTS = [
    "brighter", "darker", "more contrast", "less contrast", "warmer", "cooler",
    "more magenta", "more green", "lift the shadows", "deeper shadows",
    "brighter midtones", "darker midtones", "brighter highlights", "tame the highlights",
    "deeper blacks", "faded blacks", "more saturation", "less saturation",
    "more vibrance", "less vibrance", "more chroma", "less chroma",
    "warm the highlights", "cool the shadows", "teal and orange",
    "stronger colour grade", "weaker color grade",
    "a touch warmer", "slightly less contrast", "much cooler", "way brighter",
    "a little bit darker", "a lot more saturation", "Make it warmer, please!",
    "cooler by 300k", "brighter by 0.5 EV", "more saturation by 10%",
    "Lift The Shadows", "punchier", "desaturate",
]


class PhraseGoldenTest(unittest.TestCase):
    maxDiff = None

    def test_every_input_resolves_as_pinned(self) -> None:
        actual = json.dumps(
            {text: phrases.resolution_dict(phrases.resolve(text)) for text in INPUTS},
            indent=2, sort_keys=True,
        ) + "\n"
        if not GOLDEN.exists():
            GOLDEN.write_text(actual)
            self.fail(f"golden created at {GOLDEN}; review and commit it")
        expected = GOLDEN.read_text()
        if expected != actual:
            diff = "\n".join(difflib.unified_diff(expected.splitlines(), actual.splitlines(),
                                                  "golden", "actual", lineterm=""))
            self.fail(f"phrase resolution changed; regenerate deliberately.\n{diff}")


class PhraseRulesTest(unittest.TestCase):
    def test_intensity_scales_notches(self) -> None:
        self.assertEqual(phrases.resolve("warmer").moves[0].notches, 1.0)
        self.assertEqual(phrases.resolve("a touch warmer").moves[0].notches, 0.5)
        self.assertEqual(phrases.resolve("much warmer").moves[0].notches, 2.0)
        self.assertEqual(phrases.resolve("cooler").moves[0].notches, -1.0)

    def test_amount_replaces_intensity_and_is_bounded(self) -> None:
        self.assertEqual(phrases.resolve("warmer", amount=3).moves[0].notches, 3.0)
        for bad in (0, -1, 6, float("nan")):
            with self.subTest(amount=bad), self.assertRaises(UsageError):
                phrases.resolve("warmer", amount=bad)

    def test_explicit_by_amount_converts_display_units(self) -> None:
        mv = phrases.resolve("more saturation by 10%").moves[0]
        self.assertIsNone(mv.notches)
        self.assertAlmostEqual(mv.value, 0.10)
        self.assertAlmostEqual(phrases.resolve("cooler by 300k").moves[0].value, -300.0)

    def test_negative_by_amount_is_refused(self) -> None:
        with self.assertRaises(UsageError):
            phrases.resolve("warmer by -300k")

    def test_by_amount_with_the_wrong_unit_is_refused(self) -> None:
        with self.assertRaises(UsageError):
            phrases.resolve("warmer by 0.5 ev")

    def test_by_amount_on_a_multi_move_phrase_is_refused(self) -> None:
        with self.assertRaises(UsageError):
            phrases.resolve("teal and orange by 10%")

    def test_amount_and_by_together_is_refused(self) -> None:
        with self.assertRaises(UsageError):
            phrases.resolve("warmer by 300k", amount=2)

    def test_unknown_phrase_suggests_neighbours(self) -> None:
        with self.assertRaises(UsageError) as cm:
            phrases.resolve("warmmer")
        self.assertIn("warmer", cm.exception.detail["suggestions"])

    def test_contrast_phrases_route_through_tone(self) -> None:
        self.assertEqual(phrases.resolve("less contrast").moves[0].module, phrases.TONE)

    def test_every_phrase_targets_a_vocabulary_row(self) -> None:
        for phrase in phrases.PHRASES:
            for mv in phrase.moves:
                module = "sigmoid" if mv.module == phrases.TONE else mv.module
                with self.subTest(key=phrase.key, move=f"{module}.{mv.param}"):
                    self.assertIsNotNone(vocab.spec(module, mv.param))

    def test_synonyms_are_unique_across_phrases(self) -> None:
        seen: dict[str, str] = {}
        for phrase in phrases.PHRASES:
            for syn in phrase.synonyms:
                self.assertNotIn(syn, seen, f"{syn!r} in {phrase.key} and {seen.get(syn)}")
                seen[syn] = phrase.key


class ResolveVerbTest(unittest.TestCase):
    def test_view_reports_delta_unit_and_status(self) -> None:
        r = phrases.resolve_verb.__wrapped__("warmer", job="job-test")
        mv = r.moves[0]
        self.assertEqual((mv["module"], mv["param"], mv["unit"]), ("temperature", "temperature", "K"))
        self.assertAlmostEqual(mv["delta"], 300.0)
        self.assertEqual(mv["status"], "live")
        tone = phrases.resolve_verb.__wrapped__("more contrast", job="job-test").moves[0]
        self.assertEqual(tone["routes"], ["sigmoid", "filmicrgb"])
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --extra dev python -m pytest tests/test_phrases.py -q`
Expected: FAIL (`ImportError`).

- [ ] **Step 3: Implement `scouter/core/phrases.py`**

```python
"""Phrase -> moves (phase 3 design §5). Pure: no darktable, no model.

Grammar, in order: normalise; take one explicit `by N[unit]`; take one
intensity word; drop filler; exact synonym lookup. An unknown phrase is a
`usage` refusal carrying the nearest known phrases.
"""

from __future__ import annotations

import difflib
import math
import re
from dataclasses import asdict, dataclass

from scouter import metrics
from scouter.core import dtbridge, vocab
from scouter.errors import UsageError
from scouter.paths import current_job
from scouter.registry import ArgSpec, Verb, register
from scouter.types import ResolveResult

TONE = "@tone"
TONE_ROUTES = ["sigmoid", "filmicrgb"]


@dataclass(frozen=True)
class Move:
    module: str
    param: str
    kind: str  # "adjust" | "set"
    notches: float | None = None
    value: float | None = None


@dataclass(frozen=True)
class Phrase:
    key: str
    synonyms: tuple[str, ...]
    moves: tuple[Move, ...]


@dataclass(frozen=True)
class Resolution:
    phrase: str
    normalised: str
    key: str
    intensity: float
    explicit: bool
    moves: tuple[Move, ...]


def _adj(module: str, param: str, notches: float) -> Move:
    return Move(module, param, "adjust", notches=notches)


def _set(module: str, param: str, value: float) -> Move:
    return Move(module, param, "set", value=value)


CB = "colorbalancergb"
PHRASES: tuple[Phrase, ...] = (
    Phrase("brighter", ("brighter", "brighten", "lighter", "more exposure", "brighten up"),
           (_adj("exposure", "exposure", 1),)),
    Phrase("darker", ("darker", "darken", "less exposure", "lower exposure"),
           (_adj("exposure", "exposure", -1),)),
    Phrase("more_contrast", ("more contrast", "punchier", "add contrast", "increase contrast",
                             "more punch"), (_adj(TONE, "contrast", 1),)),
    Phrase("less_contrast", ("less contrast", "flatter", "reduce contrast", "lower contrast",
                             "softer contrast"), (_adj(TONE, "contrast", -1),)),
    Phrase("warmer", ("warmer", "warm up", "more warmth", "warm"),
           (_adj("temperature", "temperature", 1),)),
    Phrase("cooler", ("cooler", "colder", "cool down", "less warm", "cool"),
           (_adj("temperature", "temperature", -1),)),
    Phrase("more_magenta", ("more magenta", "less green"), (_adj("temperature", "tint", -1),)),
    Phrase("more_green", ("more green", "less magenta"), (_adj("temperature", "tint", 1),)),
    Phrase("lift_shadows", ("lift shadows", "lift the shadows", "open shadows", "open the shadows",
                            "open up the shadows", "brighter shadows"),
           (_adj(CB, "shadows_luminance", 1),)),
    Phrase("deepen_shadows", ("deeper shadows", "darker shadows", "crush the shadows",
                              "crush shadows"), (_adj(CB, "shadows_luminance", -1),)),
    Phrase("brighter_midtones", ("brighter midtones", "lift the midtones"),
           (_adj(CB, "midtones_luminance", 1),)),
    Phrase("darker_midtones", ("darker midtones", "lower the midtones"),
           (_adj(CB, "midtones_luminance", -1),)),
    Phrase("brighter_highlights", ("brighter highlights", "lift the highlights"),
           (_adj(CB, "highlights_luminance", 1),)),
    Phrase("tame_highlights", ("tame the highlights", "darker highlights",
                               "pull down the highlights", "recover highlights"),
           (_adj(CB, "highlights_luminance", -1),)),
    Phrase("deeper_blacks", ("deeper blacks", "richer blacks"), (_adj("exposure", "black", 1),)),
    Phrase("faded_blacks", ("faded blacks", "lift the blacks", "matte blacks"),
           (_adj("exposure", "black", -1),)),
    Phrase("more_saturation", ("more saturation", "more saturated", "more colour",
                               "more colourful", "saturate"),
           (_adj(CB, "global_saturation", 1),)),
    Phrase("less_saturation", ("less saturation", "less saturated", "desaturate", "muted",
                               "less colour"), (_adj(CB, "global_saturation", -1),)),
    Phrase("more_vibrance", ("more vibrance", "more vibrant"), (_adj(CB, "global_vibrance", 1),)),
    Phrase("less_vibrance", ("less vibrance", "less vibrant"), (_adj(CB, "global_vibrance", -1),)),
    Phrase("more_chroma", ("more chroma",), (_adj(CB, "global_chroma", 1),)),
    Phrase("less_chroma", ("less chroma",), (_adj(CB, "global_chroma", -1),)),
    Phrase("warm_highlights", ("warm the highlights", "warmer highlights"),
           (_set(CB, "highlights_hue", 60.0), _adj(CB, "highlights_chroma", 1))),
    Phrase("cool_shadows", ("cool the shadows", "cooler shadows", "blue shadows"),
           (_set(CB, "shadows_hue", 220.0), _adj(CB, "shadows_chroma", 1))),
    Phrase("teal_orange", ("teal and orange", "orange and teal", "cinematic grade"),
           (_set(CB, "shadows_hue", 200.0), _adj(CB, "shadows_chroma", 1),
            _set(CB, "highlights_hue", 55.0), _adj(CB, "highlights_chroma", 1))),
    Phrase("stronger_grade", ("stronger colour grade", "stronger grade", "more grade",
                              "more colour grade"),
           (_adj(CB, "shadows_chroma", 1), _adj(CB, "midtones_chroma", 1),
            _adj(CB, "highlights_chroma", 1))),
    Phrase("weaker_grade", ("weaker colour grade", "weaker grade", "less grade",
                            "less colour grade"),
           (_adj(CB, "shadows_chroma", -1), _adj(CB, "midtones_chroma", -1),
            _adj(CB, "highlights_chroma", -1))),
)

_INDEX: dict[str, Phrase] = {syn: p for p in PHRASES for syn in p.synonyms}

#: Longest first, so "a little bit" wins over "a little" and "a bit".
INTENSITY: tuple[tuple[str, float], ...] = (
    ("a little bit", 0.5), ("a tiny bit", 0.5), ("a touch", 0.5), ("a little", 0.5),
    ("a bit", 0.5), ("slightly", 0.5),
    ("a lot", 2.0), ("strongly", 2.0), ("much", 2.0), ("lots", 2.0), ("way", 2.0),
)
FILLER = ("make the image", "make the photo", "make it", "can you", "the image", "the photo",
          "the picture", "please")
_DROP_TOKENS = {"it", "a", "bit"}
_BY_RE = re.compile(r"\bby\s+([+-]?\d+(?:\.\d+)?)\s*(ev|k|%|°|deg|degrees)?(?=\s|$)")
_UNIT_ALIASES = {"ev": "EV", "k": "K", "%": "%", "°": "°", "deg": "°", "degrees": "°"}


def normalise(text: str) -> str:
    text = text.lower().replace("color", "colour")
    text = re.sub(r"[^a-z0-9.+%° -]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _tone_spec() -> vocab.ParamSpec:
    return vocab.spec("sigmoid", "contrast")  # type: ignore[return-value]


def _row(move: Move) -> vocab.ParamSpec:
    if move.module == TONE:
        return _tone_spec()
    return vocab.spec(move.module, move.param)  # type: ignore[return-value]


def resolve(text: str, amount: float | None = None) -> Resolution:
    if not isinstance(text, str) or not text.strip():
        raise UsageError("a phrase is required, e.g. 'warmer' or 'less contrast'")
    norm = normalise(text)
    core = norm
    explicit: tuple[float, str | None] | None = None
    m = _BY_RE.search(core)
    if m:
        raw_number = m.group(1)
        if raw_number.startswith("-"):
            raise UsageError(
                "give a positive amount: the direction words carry the sign "
                "(say 'cooler by 300k', not 'warmer by -300k')",
                detail={"phrase": text},
            )
        explicit = (float(raw_number), m.group(2))
        core = (core[: m.start()] + " " + core[m.end():]).strip()
    factor = 1.0
    for word, f in INTENSITY:
        pattern = re.compile(rf"\b{re.escape(word)}\b")
        if pattern.search(core):
            core = pattern.sub(" ", core, count=1)
            factor = f
            break
    for filler in FILLER:
        core = re.sub(rf"\b{re.escape(filler)}\b", " ", core)
    core = " ".join(t for t in core.split() if t not in _DROP_TOKENS)
    phrase = _INDEX.get(core)
    if phrase is None:
        suggestions = difflib.get_close_matches(core, list(_INDEX), n=3, cutoff=0.6)
        raise UsageError(
            f"no phrase matches {text!r}"
            + (f"; did you mean {', '.join(repr(s) for s in suggestions)}?" if suggestions else ""),
            fix="call `scouter vocab list` (MCP vocab_list) for every phrase and word",
            detail={"phrase": text, "normalised": core, "suggestions": suggestions},
        )
    if amount is not None:
        if explicit is not None:
            raise UsageError("pass either `amount` or 'by N' in the phrase, not both",
                             detail={"phrase": text, "amount": amount})
        if not (isinstance(amount, (int, float)) and math.isfinite(amount) and 0 < amount <= 5):
            raise UsageError("amount must be a number in (0, 5]", detail={"amount": amount})
        factor = float(amount)
    if explicit is not None:
        adjusts = [mv for mv in phrase.moves if mv.kind == "adjust"]
        if len(phrase.moves) != 1 or len(adjusts) != 1:
            raise UsageError(
                f"'by N' works only for a one-move phrase; {phrase.key!r} makes "
                f"{len(phrase.moves)} moves — use `amount` as a multiplier instead",
                detail={"phrase": text, "key": phrase.key},
            )
        mv = adjusts[0]
        row = _row(mv)
        number, unit = explicit
        if unit is not None and _UNIT_ALIASES[unit] != row.unit:
            raise UsageError(
                f"{row.ui} is measured in {row.unit}, not {_UNIT_ALIASES[unit]}",
                detail={"phrase": text, "unit": unit, "expected": row.unit},
            )
        sign = 1.0 if (mv.notches or 0) > 0 else -1.0
        moves = (Move(mv.module, mv.param, "adjust", None, sign * number / row.display_scale),)
        return Resolution(text, norm, phrase.key, 1.0, True, moves)
    moves = tuple(
        Move(mv.module, mv.param, "adjust", (mv.notches or 0.0) * factor, None)
        if mv.kind == "adjust" else mv
        for mv in phrase.moves
    )
    return Resolution(text, norm, phrase.key, factor, False, moves)


def resolution_dict(res: Resolution) -> dict:
    return {"key": res.key, "intensity": res.intensity, "explicit": res.explicit,
            "moves": [asdict(mv) for mv in res.moves]}


def move_view(mv: Move) -> dict:
    row = _row(mv)
    view = asdict(mv)
    if mv.kind == "adjust":
        view["delta"] = mv.value if mv.notches is None else mv.notches * row.step
    view["unit"] = row.unit
    view["status"] = vocab.status(row)
    view["pinned_path"] = dtbridge.ACTION_PATHS.get((row.module, row.param))
    if mv.module == TONE:
        view["routes"] = list(TONE_ROUTES)
    return view


@metrics.instrumented("vocab.resolve")
def resolve_verb(phrase: str, *, amount: float | None = None,
                 job: str | None = None) -> ResolveResult:
    """What a phrase would do, with each move's status. No darktable calls."""
    job = job or current_job()
    res = resolve(phrase, amount)
    return ResolveResult(phrase=res.phrase, normalised=res.normalised, key=res.key,
                         intensity=res.intensity, explicit=res.explicit,
                         moves=[move_view(mv) for mv in res.moves])


register(
    Verb(
        id="vocab.resolve",
        group="vocab",
        fn=resolve_verb,
        args=(
            ArgSpec("phrase", "str", required=True, positional=True,
                    help="plain words, e.g. 'a touch warmer'"),
            ArgSpec("amount", "float", default=None,
                    help="multiplier on the phrase's step (0-5]; replaces 'slightly'/'much'"),
        ),
        writes=False,
        gated=False,
        summary="Show what a phrase would change, without touching darktable.",
    )
)
```

In `scouter/mcp/schemas.py`, add the fragments (verbatim from spec §14.2):

```python
PHRASE = {
    "type": "string",
    "minLength": 1,
    "maxLength": 200,
    "description": "what to change, in plain words, e.g. 'warmer', 'a touch less contrast', "
    "'lift the shadows', 'cooler by 300k'; call vocab_list for every phrase",
}
AMOUNT = {
    "type": "number",
    "exclusiveMinimum": 0,
    "maximum": 5,
    "description": "multiplier on the phrase's default step (1 = one notch); replaces "
    "words like 'slightly'",
}
```

and the tool:

```python
    {
        "name": "vocab_resolve",
        "description": "Show what a phrase would do (module, parameter, delta) and whether each "
        "move is reachable, without touching darktable.",
        "inputSchema": _schema({"phrase": PHRASE, "amount": AMOUNT}, ["phrase"]),
    },
```

Contract files: `SMOKE_ARGS["vocab.resolve"] = {"phrase": "warmer"}`, `EXPECTED_VERBS` += `"vocab.resolve"`, regenerate the MCP golden. `_CORE_MODULES` += `"scouter.core.phrases"`.

- [ ] **Step 4: Create the golden, review it against spec §5.3, re-run**

Run: `uv run --extra dev python -m pytest tests/test_phrases.py -q` twice (the first run creates the golden). Check in the golden that `"cooler by 300k"` has `value: -300.0` and that `"a lot more saturation"` has `notches: 2.0`.
Expected: `13 passed`, then the full suite `0 failed`.

- [ ] **Step 5: Commit**

```bash
git add scouter/core/phrases.py scouter/registry.py scouter/mcp/schemas.py tests/test_phrases.py tests/fixtures/phrases_golden.json tests/scouter_smoke_args.py tests/test_layer_parity.py tests/fixtures/scouter_mcp_golden.json
git commit -m "phase 3 task 3: deterministic phrase resolver + vocab.resolve (MCP golden: +vocab_resolve)"
```

**Validation contract (done when):**
- `uv run --extra dev python -m pytest tests/test_phrases.py -q` → `13 passed`.
- `uv run --extra dev python -m scouter vocab resolve "a touch warmer" | python3 -c "import json,sys; m=json.load(sys.stdin)['data']['moves'][0]; print(m['module'], m['param'], m['delta'], m['status'])"` → `temperature temperature 150.0 live`.
- `uv run --extra dev python -m scouter vocab resolve "warmmer"; echo "exit=$?"` → the JSON contains `"suggestions"` including `"warmer"`, and the last line is `exit=2`.
- `uv run --extra dev python -m pytest tests -q` → no `failed`.

---

### Task 4: The educative layer (`scouter/core/explain.py`)

**Files:**
- Create: `scouter/core/explain.py`
- Test: `tests/test_explain.py`, `tests/fixtures/explain_golden.json`

**Interfaces:**
- Consumes: `vocab.ParamSpec`, `vocab.PARAMS`, `vocab.status`, `vocab.to_display`, `vocab.CONCEPT_IDS`, `config.load().educative`, `paths.job_state_read`, `paths.job_state_update`.
- Produces: `LEVELS`, `CONCEPTS: dict[str, tuple[str, str]]`, `MoveNote(spec, previous, target, kind="adjust")`, `resolve_level(override) -> str`, `with_unit(s, number_text) -> str`, `brief_line(note) -> str`, `concept_line(concept_id) -> str`, `lines(notes, *, level, job) -> list[str]`, `preset_lines(action, name, modules, items, *, level, job) -> list[str]`. There is **no verb**: this is a phrasing layer only (P3-R6).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_explain.py
"""Phase 3 task 4: educative lines (row 346), pinned by a golden."""

from __future__ import annotations

import difflib
import json
import unittest
from pathlib import Path

from scouter import paths
from scouter.core import explain, vocab
from scouter.errors import UsageError
from tests.scouter_fakes import TempCache

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "explain_golden.json"


def _note(module: str, param: str, prev: float, target: float, kind: str = "adjust"):
    return explain.MoveNote(vocab.spec(module, param), prev, target, kind)


class ExplainGoldenTest(unittest.TestCase):
    maxDiff = None

    def test_brief_lines_and_concepts_match_the_golden(self) -> None:
        data = {
            "brief": {
                f"{s.module}.{s.param}": explain.brief_line(
                    explain.MoveNote(s, s.lo + s.step, s.lo + 2 * s.step))
                for s in vocab.PARAMS if vocab.status(s) != "unreachable"
            },
            "concepts": {cid: explain.concept_line(cid) for cid in vocab.CONCEPT_IDS},
        }
        actual = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        if not GOLDEN.exists():
            GOLDEN.write_text(actual, encoding="utf-8")
            self.fail(f"golden created at {GOLDEN}; review and commit it")
        expected = GOLDEN.read_text(encoding="utf-8")
        if expected != actual:
            diff = "\n".join(difflib.unified_diff(expected.splitlines(), actual.splitlines(),
                                                  "golden", "actual", lineterm=""))
            self.fail(f"explanation text changed; regenerate deliberately.\n{diff}")


class ExplainLevelsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = TempCache()
        self.cache.start()

    def tearDown(self) -> None:
        self.cache.stop()

    def test_brief_sentence_shape(self) -> None:
        line = explain.brief_line(_note("exposure", "exposure", 1.55, 1.85))
        self.assertEqual(
            line,
            "exposure › exposure up +0.3 EV (1.55 EV → 1.85 EV): brightens or darkens the whole "
            "frame evenly, before any tone mapping; watch the highlights for clipping and the "
            "shadows for noise.",
        )

    def test_percent_rows_show_display_units(self) -> None:
        line = explain.brief_line(_note("colorbalancergb", "contrast", 0.0, 0.05))
        self.assertIn("up +5% (0% → 5%)", line)

    def test_off_is_empty(self) -> None:
        self.assertEqual(explain.lines([_note("exposure", "exposure", 1, 1.3)],
                                       level="off", job="job-test"), [])

    def test_full_explains_a_concept_once_per_session(self) -> None:
        notes = [_note("exposure", "exposure", 1, 1.3)]
        first = explain.lines(notes, level="full", job="job-test")
        self.assertEqual(len(first), 2)
        self.assertTrue(first[0].startswith("[Scene-referred exposure]"))
        second = explain.lines(notes, level="full", job="job-test")
        self.assertEqual(len(second), 1, "the concept must not repeat in the same session")
        self.assertEqual(paths.job_state_read("job-test")["explained_concepts"], ["exposure"])
        fresh = explain.lines(notes, level="full", job="job-other")
        self.assertEqual(len(fresh), 2, "a new session explains it again")

    def test_brief_does_not_mark_concepts(self) -> None:
        explain.lines([_note("exposure", "exposure", 1, 1.3)], level="brief", job="job-test")
        self.assertNotIn("explained_concepts", paths.job_state_read("job-test"))

    def test_two_moves_sharing_a_concept_explain_it_once(self) -> None:
        notes = [_note("colorbalancergb", "shadows_hue", 0, 220, "set"),
                 _note("colorbalancergb", "shadows_chroma", 0, 0.02)]
        out = explain.lines(notes, level="full", job="job-test")
        self.assertEqual(sum(1 for line in out if line.startswith("[")), 1)
        self.assertEqual(len(out), 3)

    def test_resolve_level(self) -> None:
        self.assertEqual(explain.resolve_level("full"), "full")
        self.assertEqual(explain.resolve_level(None), "brief")
        with self.assertRaises(UsageError):
            explain.resolve_level("loud")

    def test_preset_lines(self) -> None:
        out = explain.preset_lines("save", "harbour-warm", ["temperature", "sigmoid"], 2,
                                   level="brief", job="job-test")
        self.assertEqual(out, ["saved 2 module(s) (temperature, sigmoid) as style 'harbour-warm': "
                               "apply it from darktable's styles panel or with preset apply."])
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --extra dev python -m pytest tests/test_explain.py -q`
Expected: FAIL (`ImportError`).

- [ ] **Step 3: Implement `scouter/core/explain.py`**

```python
"""Educative mode (row 346; phase 3 design §7): a reply-phrasing layer ONLY.

It reads ParamSpec text, a move's numbers and the session's explained set,
and returns strings. It sends nothing to darktable and changes no value. The
session is the job (`--job` / $SCOUTER_JOB / the MCP server's pinned job);
the explained set lives in the job state file under "explained_concepts".
"""

from __future__ import annotations

from dataclasses import dataclass

from scouter import paths
from scouter.core import vocab
from scouter.errors import ScouterError, UsageError

LEVELS = ("off", "brief", "full")

CONCEPTS: dict[str, tuple[str, str]] = {
    "exposure": ("Scene-referred exposure",
                 "Exposure scales all the light in the scene by the same factor, measured in EV "
                 "(one EV doubles or halves it). In darktable's scene-referred workflow you set it "
                 "first, so the subject sits at a sensible brightness, and let the tone mapper "
                 "handle the extremes."),
    "tone_mapper": ("Tone mapping",
                    "Sigmoid (or filmic rgb) squeezes the scene's wide range of light into what a "
                    "screen can show. Its contrast slider sets how steep that curve is in the "
                    "middle: more contrast separates midtones but pushes shadows and highlights "
                    "toward the ends."),
    "white_balance": ("White balance",
                      "White balance tells darktable what colour the light was. Raising the "
                      "temperature slider says the light was bluer, so darktable compensates and "
                      "the picture turns warmer; tint trades green against magenta. Get it right "
                      "before any creative colour."),
    "color_balance": ("Color balance rgb",
                      "Color balance rgb is darktable's colour-grading module. Master sliders act "
                      "on the whole image; the 4-ways tab grades shadows (lift), midtones (power) "
                      "and highlights (gain) separately, each with luminance, hue and chroma."),
    "chroma_saturation": ("Chroma, saturation, vibrance",
                          "Chroma is colourfulness independent of brightness; saturation also "
                          "darkens as colour increases, which reads more natural on skin; "
                          "vibrance boosts the least colourful pixels most. Small numbers go a "
                          "long way: 5 % is a visible move."),
    "grading": ("Colour grading",
                "A grade gives a look by tinting shadows and highlights differently, classically "
                "cool shadows and warm highlights. Hue picks the tint, chroma picks how much; "
                "keep chroma low and judge on a neutral grey or skin."),
    "black_level": ("Black level",
                    "Black level correction moves the point that renders as pure black. Deeper "
                    "blacks add density; lifting them gives a faded, matte look. It is a strong "
                    "control: a few thousandths are visible."),
    "color_zones": ("Color zones",
                    "Color zones adjusts hue, chroma and lightness by the colour a pixel already "
                    "has (for example, only the blues). Scouter can reach only its global mix; "
                    "the zone curves need darktable's own editor."),
    "presets": ("Styles as presets",
                "A darktable style is a saved list of module settings. Applying one appends those "
                "settings to an image's history, so it acts like a filter you can stack; it never "
                "replaces what is there, and each application is visible in the history panel."),
}


@dataclass(frozen=True)
class MoveNote:
    spec: vocab.ParamSpec
    previous: float | None
    target: float | None
    kind: str = "adjust"  # "adjust" | "set"


def resolve_level(override: str | None) -> str:
    if override is not None:
        if override not in LEVELS:
            raise UsageError(f"educative must be one of {', '.join(LEVELS)}, got {override!r}",
                             detail={"educative": override})
        return override
    try:
        from scouter import config

        return config.load().educative
    except ScouterError:
        return "brief"  # a broken config must not break editing (P3-R8)


def with_unit(s: vocab.ParamSpec, number_text: str) -> str:
    if s.unit in ("ratio", "—"):
        return number_text
    if s.unit in ("%", "°"):
        return f"{number_text}{s.unit}"
    return f"{number_text} {s.unit}"


def _value(s: vocab.ParamSpec, v: float | None) -> str:
    return "unknown" if v is None else f"{vocab.to_display(s, v):.4g}"


def brief_line(note: MoveNote) -> str:
    s = note.spec
    prev, target = _value(s, note.previous), _value(s, note.target)
    if note.kind == "set" or note.previous is None or note.target is None:
        head = f"{s.ui} set to {with_unit(s, target)} (was {with_unit(s, prev)})"
    else:
        delta = vocab.to_display(s, note.target - note.previous)
        direction = "up" if delta >= 0 else "down"
        head = (f"{s.ui} {direction} {with_unit(s, f'{delta:+.3g}')} "
                f"({with_unit(s, prev)} → {with_unit(s, target)})")
    return f"{head}: {s.why}; watch {s.watch}."


def concept_line(concept_id: str) -> str:
    title, text = CONCEPTS[concept_id]
    return f"[{title}] {text}"


def _new_concepts(concepts: list[str], job: str) -> list[str]:
    seen = set(paths.job_state_read(job).get("explained_concepts", []))
    out: list[str] = []
    for c in concepts:
        if c not in seen and c not in out:
            out.append(c)
    return out


def _mark(job: str, concepts: list[str]) -> None:
    if not concepts:
        return

    def _mutate(state: dict) -> None:
        state["explained_concepts"] = sorted(set(state.get("explained_concepts", [])) | set(concepts))

    paths.job_state_update(job, _mutate)


def lines(notes: list[MoveNote], *, level: str, job: str) -> list[str]:
    if level not in LEVELS:
        raise UsageError(f"educative must be one of {', '.join(LEVELS)}", detail={"educative": level})
    if level == "off" or not notes:
        return []
    if level == "brief":
        return [brief_line(n) for n in notes]
    new = _new_concepts([n.spec.concept for n in notes], job)
    out: list[str] = []
    emitted: set[str] = set()
    for n in notes:
        c = n.spec.concept
        if c in new and c not in emitted:
            out.append(concept_line(c))
            emitted.add(c)
        out.append(brief_line(n))
    _mark(job, new)
    return out


def preset_lines(action: str, name: str, modules: list[str], items: int, *,
                 level: str, job: str) -> list[str]:
    if level == "off":
        return []
    if action == "save":
        line = (f"saved {len(modules)} module(s) ({', '.join(modules)}) as style '{name}': "
                "apply it from darktable's styles panel or with preset apply.")
    else:
        line = (f"applied style '{name}' ({items} history item(s) appended): compare with the "
                "history panel; darktable's own history is the way back.")
    out: list[str] = []
    if level == "full":
        new = _new_concepts(["presets"], job)
        if new:
            out.append(concept_line("presets"))
            _mark(job, new)
    out.append(line)
    return out
```

Also add a one-line test that every `ParamSpec.concept` is a `CONCEPTS` key, in `ExplainLevelsTest`:

```python
    def test_every_row_concept_has_text(self) -> None:
        self.assertEqual(set(explain.CONCEPTS), set(vocab.CONCEPT_IDS))
```

- [ ] **Step 4: Create the golden, review it, re-run**

Run: `uv run --extra dev python -m pytest tests/test_explain.py -q` twice. Read the golden for tone: one sentence per row, units correct (`°` rows say `set to` only when kind is set, and the golden uses adjust).
Expected: `10 passed`, then the full suite `0 failed`.

- [ ] **Step 5: Commit**

```bash
git add scouter/core/explain.py tests/test_explain.py tests/fixtures/explain_golden.json
git commit -m "phase 3 task 4: educative phrasing layer (off/brief/full, session-scoped concepts)"
```

**Validation contract (done when):**
- `uv run --extra dev python -m pytest tests/test_explain.py -q` → `10 passed`.
- `grep -n "dtbridge\|darkroom" scouter/core/explain.py` → no output (the layer makes no darktable calls).
- `uv run --extra dev python -c "from scouter.core import explain; print(sorted(explain.CONCEPTS))"` → `['black_level', 'chroma_saturation', 'color_balance', 'color_zones', 'exposure', 'grading', 'presets', 'tone_mapper', 'white_balance']`.

---

### Task 5: Relative moves — `darkroom.get` and `darkroom.adjust`

**Files:**
- Create: `scouter/core/moves.py`
- Modify: `scouter/registry.py` (`_CORE_MODULES` += `"scouter.core.moves"`), `scouter/mcp/schemas.py` (`darkroom_get`, `darkroom_adjust`, fragment `EDUCATIVE`) + contract files
- Test: `tests/test_moves.py`

**Interfaces:**
- Consumes: `vocab.*`, `explain.lines`, `explain.MoveNote`, `explain.resolve_level`, `darkroom._current_image`, `darkroom._require_job_image`, `darkroom._commit`, `darkroom._raw_and_before_state`, `darkroom._read_sidecar_parsed`, `darkroom._module_enabled`, `darkroom.set_param.__wrapped__`, `darkroom.MODULE_ARG`/`PARAM_ARG`/`INSTANCE_ARG`/`DRY_RUN_ARG`/`COMMIT_ARG`, `dtbridge.require_darkroom`, `history.watermark_fresh`, `xmp.latest_params`, `wb.kelvin`, `wb.load_table`, `errors.ValueUnknown`/`NotPinned`, `types.ValueResult`/`AdjustResult`.
- Produces: `moves.require_row(module, param) -> ParamSpec`, `moves.gate(s, *, relative: bool) -> None`, `moves.read_current(resolved, s, instance, job) -> tuple[float | None, str, str | None]`, `moves._camera_for(path) -> str | None` (test seam), `moves.route_tone(raw_path) -> str`, verbs `darkroom.get` → `get(module, param, *, instance=0, job=None) -> ValueResult` and `darkroom.adjust` → `adjust(module, param, delta, *, instance=0, commit=True, trust_sidecar=False, educative=None, dry_run=False, job=None) -> AdjustResult`. Also `moves._adjust_core(...)`, the undecorated body `say` reuses in task 6 (same signature as `adjust`, but `resolved` is passed in and pre-commit is skipped).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_moves.py
"""Phase 3 task 5: relative moves read, clamp, set -- with a call-counting FakeBridge."""

from __future__ import annotations

import os
import shutil
import unittest
from pathlib import Path
from unittest import mock

from scouter.core import dtbridge, moves
from scouter.errors import NotPinned, UsageError, ValueUnknown
from scouter.types import CommitResult
from tests.scouter_fakes import FakeBridge, TempCache

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class _MovesFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = TempCache()
        root = self.cache.start()
        self.raw = root / "P6281348.ORF"
        self.raw.write_bytes(b"not really a raw")
        shutil.copy(FIXTURES / "P6281348.ORF.xmp", str(self.raw) + ".xmp")
        # The measured EM10iii table, under a temp config dir (never ~/.config).
        cfg_dir = root / "config"
        (cfg_dir / "wb").mkdir(parents=True)
        shutil.copy(FIXTURES / "wb-em10iii.json", cfg_dir / "wb" / "em10iii.json")
        env = mock.patch.dict(os.environ, {"SCOUTER_CONFIG": str(cfg_dir / "config.json")})
        env.start()
        self.addCleanup(env.stop)
        cam = mock.patch("scouter.core.moves._camera_for", return_value="em10iii")
        cam.start()
        self.addCleanup(cam.stop)
        self.bridge = FakeBridge({
            "darkroom.current": {"image_id": 12, "path": str(self.raw), "view": "darkroom",
                                 "source": "action_images"},
            "image.lookup": {"found": True, "image_id": 12, "path": str(self.raw)},
            "darkroom.action": lambda p: {"nan": False, "status": p.get("value"),
                                          "view": "darkroom", **p},
        })
        dtbridge.set_bridge(self.bridge)
        self.commits: list[tuple] = []

        def fake_commit(job, image, *, strategy="auto", attempts=None, expect_change=True):
            self.commits.append((attempts, expect_change))
            return CommitResult(committed=True, changed=expect_change, warnings=[])

        patcher = mock.patch("scouter.core.darkroom._commit", side_effect=fake_commit)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        dtbridge.set_bridge(None)
        self.cache.stop()

    def sets(self) -> list[dict]:
        return [p for m, p in self.bridge.calls if m == "darkroom.action"]


class AdjustTest(_MovesFixture):
    def test_adjust_reads_then_sets_the_sum(self) -> None:
        r = moves.adjust.__wrapped__("exposure", "exposure", 0.3, commit=False, job="job-test")
        self.assertAlmostEqual(r.previous, 1.5500002, places=5)
        self.assertAlmostEqual(r.target, 1.8500002, places=5)
        self.assertEqual(r.source, "xmp")
        self.assertEqual(len(self.sets()), 1)
        self.assertAlmostEqual(self.sets()[0]["value"], 1.8500002, places=5)
        self.assertEqual(r.summary, "exposure › exposure: +0.3 EV (1.55 EV → 1.85 EV)")

    def test_pre_commit_is_one_expect_no_change_bounce(self) -> None:
        moves.adjust.__wrapped__("exposure", "exposure", 0.3, commit=False, job="job-test")
        self.assertEqual(self.commits[0], (1, False))

    def test_trust_sidecar_skips_the_pre_commit(self) -> None:
        moves.adjust.__wrapped__("exposure", "exposure", 0.3, commit=False,
                                 trust_sidecar=True, job="job-test")
        self.assertEqual(self.commits, [])

    def test_clamp_is_reported(self) -> None:
        r = moves.adjust.__wrapped__("exposure", "exposure", 10.0, commit=False, job="job-test")
        self.assertTrue(r.clamped)
        self.assertEqual(r.target, 4.0)
        self.assertIn("[clamped at 4 EV]", r.summary)

    def test_adjust_at_the_limit_sends_nothing(self) -> None:
        moves.adjust.__wrapped__("exposure", "exposure", 10.0, commit=False, job="job-test")
        before = len(self.sets())
        r = moves.adjust.__wrapped__("exposure", "exposure", 0.3, commit=False, job="job-test")
        self.assertTrue(r.noop)
        self.assertEqual(len(self.sets()), before, "a move at the limit must send nothing")

    def test_kelvin_comes_from_the_measured_table(self) -> None:
        r = moves.adjust.__wrapped__("temperature", "temperature", 300.0, commit=False,
                                     job="job-test")
        self.assertEqual(r.source, "wb_table")
        self.assertAlmostEqual(r.previous, 5399.05, places=1)
        self.assertAlmostEqual(self.sets()[0]["value"], 5699.05, places=1)
        self.assertTrue(any("tint 1.0" in w for w in r.warnings))

    def test_kelvin_without_a_table_is_value_unknown(self) -> None:
        with mock.patch("scouter.core.moves._camera_for", return_value="a7"):
            with self.assertRaises(ValueUnknown) as cm:
                moves.adjust.__wrapped__("temperature", "temperature", 300.0, job="job-test")
        self.assertIn("wb calibrate", cm.exception.detail["reason"])
        self.assertEqual(self.sets(), [])

    def test_absent_module_uses_the_declared_neutral(self) -> None:
        r = moves.adjust.__wrapped__("colorbalancergb", "contrast", 0.05, commit=False,
                                     job="job-test")
        self.assertEqual((r.previous, r.source), (0.0, "neutral"))
        self.assertIn("+5% (0% → 5%)", r.summary)

    def test_absolute_only_refuses_value_unknown_before_any_write(self) -> None:
        with self.assertRaises(ValueUnknown) as cm:
            moves.adjust.__wrapped__("sigmoid", "skew", 0.1, job="job-test")
        self.assertIn("darkroom set sigmoid skew", cm.exception.fix)
        self.assertEqual(self.bridge.calls, [])

    def test_candidate_refuses_not_pinned_before_any_bridge_call(self) -> None:
        with self.assertRaises(NotPinned):
            moves.adjust.__wrapped__("colorbalancergb", "shadows_luminance", 0.02, job="job-test")
        self.assertEqual(self.bridge.calls, [])

    def test_hue_wraps_modulo_360(self) -> None:
        hue = ("colorbalancergb", "shadows_hue")
        with mock.patch.dict(dtbridge.ACTION_PATHS, {hue: "iop/colorbalancergb/shadows lift/hue"}), \
             mock.patch("scouter.core.vocab.xmp_named", return_value=True), \
             mock.patch("scouter.core.moves.read_current", return_value=(350.0, "xmp", None)):
            r = moves.adjust.__wrapped__(*hue, 20.0, commit=False, job="job-test")
        self.assertAlmostEqual(r.target, 10.0)
        self.assertFalse(r.clamped)

    def test_zero_or_nonfinite_delta_is_usage(self) -> None:
        for bad in (0.0, float("nan"), float("inf")):
            with self.subTest(delta=bad), self.assertRaises(UsageError):
                moves.adjust.__wrapped__("exposure", "exposure", bad, job="job-test")

    def test_not_in_vocabulary_is_usage_naming_darkroom_set(self) -> None:
        with self.assertRaises(UsageError) as cm:
            moves.adjust.__wrapped__("lens", "tca", 0.1, job="job-test")
        self.assertIn("darkroom set", cm.exception.fix)

    def test_dry_run_makes_no_bridge_calls(self) -> None:
        r = moves.adjust.__wrapped__("exposure", "exposure", 0.3, dry_run=True, job="job-test")
        self.assertEqual(r.outcome, "dry_run")
        self.assertEqual(self.bridge.calls, [])
        self.assertEqual(r.plan[0]["path"], "iop/exposure/exposure")

    def test_brief_explain_is_attached(self) -> None:
        r = moves.adjust.__wrapped__("exposure", "exposure", 0.3, commit=False,
                                     educative="brief", job="job-test")
        self.assertEqual(len(r.explain), 1)
        self.assertIn("watch the highlights", r.explain[0])
        off = moves.adjust.__wrapped__("exposure", "exposure", 0.3, commit=False,
                                       educative="off", job="job-test")
        self.assertEqual(off.explain, [])

    def test_ruling_30_guard_applies(self) -> None:
        from scouter import paths
        paths.job_state_update("job-test", lambda s: s.__setitem__("opened_image_id", 99))
        from scouter.errors import NotInDarkroom
        with self.assertRaises(NotInDarkroom):
            moves.adjust.__wrapped__("exposure", "exposure", 0.3, job="job-test")
        self.assertEqual(self.sets(), [])


class GetTest(_MovesFixture):
    def test_get_reads_the_value_and_never_commits(self) -> None:
        r = moves.get.__wrapped__("sigmoid", "contrast", job="job-test")
        self.assertAlmostEqual(r.value, 1.4, places=5)
        self.assertEqual((r.source, r.status), ("xmp", "live"))
        self.assertEqual(self.commits, [])
        self.assertFalse(r.fresh)
        self.assertTrue(any("not known-fresh" in w for w in r.warnings))

    def test_get_reports_null_with_a_reason_instead_of_refusing(self) -> None:
        r = moves.get.__wrapped__("sigmoid", "skew", job="job-test")
        self.assertIsNone(r.value)
        self.assertEqual(r.status, "absolute_only")


class CliNegativeDeltaTest(unittest.TestCase):
    def test_negative_positional_delta_parses(self) -> None:
        from scouter.cli.main import build_parser
        ns = build_parser().parse_args(["darkroom", "adjust", "sigmoid", "contrast", "-0.1", "--dry-run"])
        self.assertEqual(ns.delta, -0.1)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --extra dev python -m pytest tests/test_moves.py -q`
Expected: FAIL (`ImportError: cannot import name 'moves'`).

- [ ] **Step 3: Implement `scouter/core/moves.py` (get + adjust)**

```python
"""Relative moves (phase 3 design §6): read the current value, add, clamp, set.

The engine is unchanged: every write goes through `darkroom.set_param.__wrapped__`
(one metrics event per top-level verb). Values come from the journal, the XMP,
the measured WB table or a declared neutral -- never from a slider position
(P3-R2).
"""

from __future__ import annotations

import math
import time

from scouter import metrics
from scouter.core import darkroom, dtbridge, explain, history, vocab, wb, xmp
from scouter.errors import NotPinned, ScouterError, UsageError, ValueUnknown
from scouter.paths import current_job
from scouter.registry import ArgSpec, Verb, register
from scouter.types import AdjustResult, ResolvedImage, ValueResult

_TEMP_COEFFS = ("coeff_r", "coeff_g", "coeff_b", "coeff_g2")
_TINT_WARNING = ("Kelvin read from the r/b coefficient ratio against a table measured at "
                 "tint 1.0; at another tint it is approximate")


def require_row(module: str, param: str) -> vocab.ParamSpec:
    s = vocab.spec(module, param)
    if s is None:
        raise UsageError(
            f"{module}.{param} is not in the conversational vocabulary",
            fix="`scouter darkroom set` reaches any pinned pair; `scouter vocab list` shows the words",
            detail={"module": module, "param": param},
        )
    return s


def gate(s: vocab.ParamSpec, *, relative: bool) -> None:
    """Refuse by status BEFORE any bridge call (design §4.2)."""
    st = vocab.status(s)
    if st in ("candidate", "unreachable"):
        alt = vocab.closest_live(s)
        hint = s.note or (f"closest live word: {alt.module}.{alt.param} ({alt.ui})" if alt else
                          "no live alternative yet")
        raise NotPinned(
            f"{s.module}.{s.param} has no pinned darktable action path (status {st}); {hint}",
            detail={"module": s.module, "param": s.param, "status": st,
                    "candidate_paths": list(s.candidate_paths)},
        )
    if relative and st == "absolute_only":
        raise ValueUnknown(
            f"{s.module}.{s.param}'s current value is not readable from the XMP yet",
            fix=(f"set it absolutely: `scouter darkroom set {s.module} {s.param} <value>`; "
                 "relative moves need its XMP slot pinned by a host probe"),
            detail={"module": s.module, "param": s.param, "status": st,
                    "reason": "no named XMP field for this parameter"},
        )


def _camera_for(path: str) -> str | None:
    """Camera slug from EXIF, or None. A test seam: unit tests patch it."""
    from scouter import config
    from scouter.core import exif

    try:
        return exif.camera_slug(exif.read_tags(path), config.load())
    except (ScouterError, OSError):
        return None


def _table_range(camera: str) -> tuple[float, float] | None:
    try:
        table = wb.load_table(camera)
    except ValueError:
        return None
    if not table:
        return None
    ks = [float(p["kelvin"]) for p in table["samples"]]
    return min(ks), max(ks)


def read_current(resolved: ResolvedImage, s: vocab.ParamSpec, instance: int,
                 job: str) -> tuple[float | None, str, str | None]:
    """(value, source, reason). Source order: journal, xmp, wb_table, neutral."""
    if s.value_source == "wb_table":
        parsed = darkroom._read_sidecar_parsed(resolved.path)
        if parsed is None:
            return None, "wb_table", "no sidecar yet: commit history first"
        params = xmp.latest_params(parsed, "temperature", instance)
        if not params:
            return None, "wb_table", "no active temperature entry in the XMP"
        try:
            coeffs = [float(params[k]) for k in _TEMP_COEFFS]
        except (KeyError, TypeError, ValueError):
            return None, "wb_table", "temperature's coefficients did not decode"
        camera = _camera_for(resolved.path)
        if camera is None:
            return None, "wb_table", "camera model unreadable from EXIF; run scouter wb calibrate"
        result = wb.kelvin(coeffs, camera)
        return result.get("kelvin"), "wb_table", result.get("reason")
    raw, before, pending = darkroom._raw_and_before_state(
        job, resolved.image_id, resolved.path, s.module, s.param, instance)
    if pending and before.get("value") is not None:
        return float(before["value"]), "journal", None
    if raw.get("value") is not None:
        return float(raw["value"]), "xmp", None
    if raw.get("enabled") is None and s.neutral is not None:
        return s.neutral, "neutral", None
    return None, "xmp", f"{s.module}.{s.param} did not decode from the XMP"


def route_tone(raw_path: str) -> str:
    """`@tone` -> filmicrgb only when filmic is enabled+active and sigmoid is not (§5.4)."""
    parsed = darkroom._read_sidecar_parsed(raw_path)
    if parsed is None:
        return "sigmoid"
    enabled = {e.operation for e in xmp.active_entries(parsed) if e.enabled}
    return "filmicrgb" if "filmicrgb" in enabled and "sigmoid" not in enabled else "sigmoid"


def _fmt(s: vocab.ParamSpec, v: float | None) -> str:
    return "?" if v is None else explain.with_unit(s, f"{vocab.to_display(s, v):.4g}")


def _summary(s: vocab.ParamSpec, delta: float, prev: float, shown: float | None,
             clamped: bool, target: float) -> str:
    d = explain.with_unit(s, f"{vocab.to_display(s, delta):+.3g}")
    line = f"{s.ui}: {d} ({_fmt(s, prev)} → {_fmt(s, shown if shown is not None else target)})"
    if clamped:
        line += f" [clamped at {_fmt(s, target)}]"
    return line


def _adjust_core(module: str, param: str, delta: float, *, resolved: ResolvedImage,
                 instance: int, commit: bool, level: str, job: str) -> AdjustResult:
    """Steps 5-11 of §6.2. The caller has gated, resolved and (maybe) pre-committed."""
    s = vocab.spec(module, param)
    assert s is not None
    t0 = time.monotonic()
    previous, source, reason = read_current(resolved, s, instance, job)
    if previous is None:
        raise ValueUnknown(
            f"cannot read {module}.{param}'s current value",
            detail={"module": module, "param": param, "source_tried": source,
                    "reason": reason or "unknown"},
        )
    warnings: list[str] = []
    target, clamped = vocab.clamp(s, previous + delta)
    if s.value_source == "wb_table":
        warnings.append(_TINT_WARNING)
        camera = _camera_for(resolved.path)
        rng = _table_range(camera) if camera else None
        if rng and not (rng[0] <= target <= rng[1]):
            target, clamped = min(max(target, rng[0]), rng[1]), True
    if clamped and abs(target - previous) < 1e-6:
        return AdjustResult(
            image_id=resolved.image_id, path=resolved.path, module=module, param=param,
            instance=instance, unit=s.unit, delta=delta, previous=previous, target=target,
            applied=previous, clamped=True, source=source, committed=False, noop=True,
            summary=f"{s.ui}: already at the limit ({_fmt(s, previous)}); nothing sent",
            warnings=warnings,
        )
    set_fn = getattr(darkroom.set_param, "__wrapped__", darkroom.set_param)
    t1 = time.monotonic()
    result = set_fn(module, param, target, instance=instance, commit=commit, job=job)
    applied = result.applied
    if s.value_source == "wb_table":
        applied, _, _ = read_current(resolved, s, instance, job) if commit else (None, "", None)
    warnings.extend(result.warnings)
    explain_lines = explain.lines([explain.MoveNote(s, previous, target)], level=level, job=job)
    return AdjustResult(
        image_id=resolved.image_id, path=resolved.path, module=module, param=param,
        instance=instance, unit=s.unit, delta=delta, previous=previous, target=target,
        applied=applied, clamped=clamped, source=source, committed=result.committed,
        noop=result.noop, summary=_summary(s, delta, previous, applied, clamped, target),
        explain=explain_lines, warnings=warnings,
        plan=[{"action": "darkroom.adjust", "ms_read": int((t1 - t0) * 1000),
               "ms_set": int((time.monotonic() - t1) * 1000)}],
    )


def _precommit(job: str, image_id: int) -> str | None:
    """One expect-no-change bounce (P3-R3). A timeout is a warning, not a failure."""
    try:
        darkroom._commit(job, image_id, strategy="auto", attempts=1, expect_change=False)
    except ScouterError as e:
        return f"pre-commit did not confirm ({e.code}); the value read may predate darktable's live state"
    return None


@metrics.instrumented("darkroom.get")
def get(module: str, param: str, *, instance: int = 0, job: str | None = None) -> ValueResult:
    """A parameter's current VALUE. Never commits, never moves the view."""
    job = job or current_job()
    s = require_row(module, param)
    resolved = darkroom._current_image()
    value, source, reason = read_current(resolved, s, instance, job)
    fresh, why = history.watermark_fresh(job, resolved.image_id, resolved.xmp_path)
    warnings: list[str] = []
    if not fresh:
        warnings.append(f"sidecar not known-fresh ({why}): darktable may hold newer, uncommitted "
                        "changes; `darkroom adjust` commits before it reads")
    if value is None:
        warnings.append(reason or f"{module}.{param} is not readable")
    return ValueResult(
        image_id=resolved.image_id, path=resolved.path, module=module, param=param,
        instance=instance, value=value,
        display_value=None if value is None else vocab.to_display(s, value),
        unit=s.unit, source=source, enabled=darkroom._module_enabled(resolved.path, module, instance),
        fresh=bool(fresh), status=vocab.status(s), warnings=warnings,
    )


@metrics.instrumented("darkroom.adjust")
def adjust(module: str, param: str, delta: float, *, instance: int = 0, commit: bool = True,
           trust_sidecar: bool = False, educative: str | None = None, dry_run: bool = False,
           job: str | None = None) -> AdjustResult:
    """Move one parameter by `delta` (internal units), relative to its current value."""
    job = job or current_job()
    s = require_row(module, param)
    delta = darkroom._require_finite("delta", delta)
    if delta == 0:
        raise UsageError("delta must be non-zero", detail={"argument": "delta"})
    level = explain.resolve_level(educative)
    gate(s, relative=True)
    if dry_run:
        return AdjustResult(
            outcome="dry_run", module=module, param=param, instance=instance, unit=s.unit,
            delta=delta,
            plan=[{"action": "darkroom.adjust", "path": dtbridge.ACTION_PATHS[(module, param)],
                   "delta": delta, "clamp": [s.lo, s.hi], "wraps": s.wraps}],
            reason="dry run: no darktable calls were made; the current value was not read",
        )
    resolved = darkroom._current_image()
    darkroom._require_job_image(job, resolved.image_id)
    dtbridge.require_darkroom()
    warning = None if trust_sidecar else _precommit(job, resolved.image_id)
    result = _adjust_core(module, param, delta, resolved=resolved, instance=instance,
                          commit=commit, level=level, job=job)
    if warning:
        result.warnings.insert(0, warning)
    return result


EDUCATIVE_ARG = ArgSpec("educative", "str", default=None, choices=("off", "brief", "full"),
                        help="explanation level for this call (default: config's educative)")
TRUST_ARG = ArgSpec("trust_sidecar", "bool", default=False,
                    help="skip the pre-read commit bounce (scripts that own the darkroom)")

for verb in (
    Verb(
        id="darkroom.get",
        group="darkroom",
        fn=get,
        args=(darkroom.MODULE_ARG, darkroom.PARAM_ARG, darkroom.INSTANCE_ARG),
        writes=False,
        gated=False,
        summary="Read one parameter's current value from the XMP (never moves the view).",
    ),
    Verb(
        id="darkroom.adjust",
        group="darkroom",
        fn=adjust,
        args=(
            darkroom.MODULE_ARG,
            darkroom.PARAM_ARG,
            ArgSpec("delta", "float", required=True, positional=True,
                    help="relative change in internal units (EV, K, or a fraction: 0.05 = 5%%)"),
            darkroom.INSTANCE_ARG,
            darkroom.COMMIT_ARG,
            TRUST_ARG,
            EDUCATIVE_ARG,
            darkroom.DRY_RUN_ARG,
        ),
        writes=True,
        gated=False,
        summary="Change one parameter by a relative amount, clamped to a safe range.",
    ),
):
    register(verb)
```

`xmp.active_entries(parsed)` exists (phase 2 R1). `darkroom._require_finite` exists. If the `status` in `darkroom.status`'s warning is edited (spec §11: "its warning names `darkroom get`"), that is a string change inside `status` only: replace `read values with \`history read\` (design 7.3)` with `read values with \`darkroom get\` or \`history read\``. Update any test asserting the old text.

MCP schemas: add the `EDUCATIVE` fragment (spec §14.2) and:

```python
    {
        "name": "darkroom_get",
        "description": "Read one parameter's current VALUE from the image's XMP sidecar (or Kelvin "
        "via the camera's measured table). Never moves the view; `fresh: false` means darktable "
        "may hold newer, uncommitted changes.",
        "inputSchema": _schema(
            {"module": {"type": "string", "description": "darktable module, e.g. sigmoid"},
             "param": {"type": "string", "description": "parameter, e.g. contrast"},
             "instance": INSTANCE},
            ["module", "param"],
        ),
    },
    {
        "name": "darkroom_adjust",
        "description": "Change one module parameter by a relative delta in internal units (EV for "
        "exposure, K for temperature, fractions for color balance rgb: 0.05 means 5 %). Reads the "
        "current value first (one view bounce to flush unsaved slider moves unless trust_sidecar), "
        "clamps to Scouter's safe range, sets it, and returns previous/target/applied plus a "
        "one-line summary.",
        "inputSchema": _schema(
            {"module": {"type": "string", "description": "darktable module, e.g. exposure"},
             "param": {"type": "string", "description": "parameter, e.g. exposure"},
             "delta": {"type": "number", "description": "relative change, internal units, non-zero"},
             "instance": INSTANCE,
             "commit": {"type": "boolean", "default": True,
                        "description": "commit history to the XMP after the write"},
             "trust_sidecar": {"type": "boolean", "default": False,
                               "description": "skip the pre-read commit bounce"},
             "educative": EDUCATIVE,
             "dry_run": DRY_RUN},
            ["module", "param", "delta"],
        ),
    },
```

Contract files: `SMOKE_ARGS["darkroom.get"] = {"module": "exposure", "param": "exposure"}`, `SMOKE_ARGS["darkroom.adjust"] = {"module": "exposure", "param": "exposure", "delta": 0.3, "dry_run": True}`, `EXPECTED_VERBS` += both, regenerate the MCP golden. `_CORE_MODULES` += `"scouter.core.moves"` (after `"scouter.core.darkroom"`).

- [ ] **Step 4: Run to verify pass**

Run: `uv run --extra dev python -m pytest tests/test_moves.py -q && uv run --extra dev python -m pytest tests -q`
Expected: `19 passed`, then the full suite `0 failed`.

- [ ] **Step 5: Commit**

```bash
git add scouter/core/moves.py scouter/core/darkroom.py scouter/registry.py scouter/mcp/schemas.py tests/test_moves.py tests/scouter_smoke_args.py tests/test_layer_parity.py tests/fixtures/scouter_mcp_golden.json
git commit -m "phase 3 task 5: darkroom.get + darkroom.adjust (read, clamp, set; MCP golden: +darkroom_get, +darkroom_adjust)"
```

**Validation contract (done when):**
- `uv run --extra dev python -m pytest tests/test_moves.py -q` → `19 passed`.
- `uv run --extra dev python -m scouter darkroom adjust sigmoid contrast -0.1 --dry-run; echo "exit=$?"` → JSON with `"outcome": "dry_run"` and `"delta": -0.1`, and the last line is `exit=5`.
- `uv run --extra dev python -m scouter darkroom adjust colorbalancergb shadows_luminance 0.02; echo "exit=$?"` → JSON `"outcome": "error:not_pinned"`, last line `exit=21` (refused before any bridge call, so darktable need not run).
- `uv run --extra dev python -m scouter darkroom adjust sigmoid skew 0.1; echo "exit=$?"` → `error:value_unknown`, last line `exit=20`.
- `git diff main --stat -- scouter/core/darkroom.py` → at most 2 lines changed (only `status`'s warning string); `set_param` and `nudge` are untouched.

---

### Task 6: `darkroom.say` and undo by move group

**Files:**
- Modify: `scouter/core/moves.py` (add `say`)
- Modify: `scouter/core/darkroom.py` (`undo_last` → `_undo_one` + group loop; `single` arg)
- Modify: `scouter/mcp/schemas.py` (`darkroom_say`, `darkroom_undo_last.single`) + contract files
- Test: `tests/test_say.py`

**Interfaces:**
- Consumes: `phrases.resolve`, `phrases.TONE`, `moves._adjust_core`, `moves.gate`, `moves.route_tone`, `moves._precommit`, `journal.GROUP`, `journal.next_seq`, `darkroom.set_param.__wrapped__`, `explain.lines`, `types.SayResult`.
- Produces: verb `darkroom.say` → `say(phrase, *, amount=None, commit=True, trust_sidecar=False, educative=None, dry_run=False, job=None) -> SayResult`; `darkroom.undo_last(*, single=False, dry_run=False, job=None) -> UndoResult` (with `undone_group`); `darkroom._undo_one(*, dry_run, job) -> UndoResult` (the old body, undecorated).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_say.py
"""Phase 3 task 6: phrases as grouped moves, undone together."""

from __future__ import annotations

from unittest import mock

from scouter.core import darkroom, dtbridge, journal, moves
from scouter.errors import NotPinned, UsageError, ValueUnknown
from tests.test_moves import _MovesFixture


class SayTest(_MovesFixture):
    def test_one_move_phrase_lands_in_a_group(self) -> None:
        r = moves.say.__wrapped__("a touch warmer", commit=False, job="job-test")
        self.assertEqual(r.key, "warmer")
        self.assertEqual(len(r.moves), 1)
        self.assertAlmostEqual(r.moves[0]["delta"], 150.0)
        self.assertTrue(r.group.startswith("say-"))
        self.assertEqual(journal.read_all("job-test")[-1].group, r.group)
        self.assertIsNone(journal.GROUP.get(), "the contextvar must be reset")

    def test_contrast_routes_to_sigmoid_on_the_fixture(self) -> None:
        r = moves.say.__wrapped__("less contrast", commit=False, job="job-test")
        self.assertEqual(r.moves[0]["module"], "sigmoid")
        self.assertAlmostEqual(r.moves[0]["target"], 1.3, places=5)

    def test_contrast_routes_to_filmic_when_filmic_is_the_tone_mapper(self) -> None:
        with mock.patch("scouter.core.moves.route_tone", return_value="filmicrgb"):
            with self.assertRaises(NotPinned) as cm:
                moves.say.__wrapped__("less contrast", job="job-test")
        self.assertEqual(cm.exception.detail["refused"][0]["module"], "filmicrgb")

    def test_a_phrase_with_any_unreachable_move_is_refused_whole(self) -> None:
        with self.assertRaises(NotPinned) as cm:
            moves.say.__wrapped__("teal and orange", job="job-test")
        self.assertEqual(len(cm.exception.detail["refused"]), 4)
        self.assertEqual(self.sets(), [], "nothing may land before the pre-validation")

    def test_absolute_only_phrase_is_value_unknown(self) -> None:
        with self.assertRaises(ValueUnknown):
            moves.say.__wrapped__("more saturation", job="job-test")

    def test_unknown_phrase_is_usage_with_suggestions(self) -> None:
        with self.assertRaises(UsageError) as cm:
            moves.say.__wrapped__("warmmer", job="job-test")
        self.assertIn("warmer", cm.exception.detail["suggestions"])

    def test_one_pre_commit_for_the_whole_phrase(self) -> None:
        moves.say.__wrapped__("brighter", commit=False, job="job-test")
        self.assertEqual(self.commits, [(1, False)])

    def test_dry_run_resolves_without_darktable(self) -> None:
        r = moves.say.__wrapped__("warmer", dry_run=True, job="job-test")
        self.assertEqual(r.outcome, "dry_run")
        self.assertEqual(self.bridge.calls, [])

    def test_partial_when_a_later_move_fails(self) -> None:
        with mock.patch.dict(dtbridge.ACTION_PATHS, {
            ("colorbalancergb", "shadows_chroma"): "iop/colorbalancergb/shadows lift/chroma",
            ("colorbalancergb", "midtones_chroma"): "iop/colorbalancergb/power/chroma",
            ("colorbalancergb", "highlights_chroma"): "iop/colorbalancergb/highlights gain/chroma",
        }), mock.patch("scouter.core.vocab.xmp_named", return_value=True):
            calls = {"n": 0}

            def flaky(p):
                calls["n"] += 1
                if calls["n"] == 2:
                    return {"nan": True, "view": "darkroom", **p}
                return {"nan": False, "status": p.get("value"), "view": "darkroom", **p}

            self.bridge.responses["darkroom.action"] = flaky
            r = moves.say.__wrapped__("stronger grade", commit=False, job="job-test")
        self.assertEqual(r.outcome, "partial")
        self.assertEqual(len(r.moves), 1)
        self.assertEqual(r.refused[0]["code"], "not_in_darkroom")


class GroupUndoTest(_MovesFixture):
    def _two_moves(self) -> str:
        """Land two exposure writes in one group by hand (no multi-move live phrase exists yet)."""
        token = journal.GROUP.set("say-test")
        try:
            darkroom.set_param.__wrapped__("exposure", "exposure", 1.8, commit=False, job="job-test")
            darkroom.set_param.__wrapped__("sigmoid", "contrast", 1.6, commit=False, job="job-test")
        finally:
            journal.GROUP.reset(token)
        return "say-test"

    def test_undo_reverts_the_whole_group_newest_first(self) -> None:
        self._two_moves()
        r = darkroom.undo_last.__wrapped__(job="job-test")
        self.assertEqual([u["undone"]["module"] for u in r.undone_group], ["sigmoid", "exposure"])
        self.assertTrue(all(e.undone for e in journal.read_all("job-test")))

    def test_single_undoes_only_the_newest(self) -> None:
        self._two_moves()
        r = darkroom.undo_last.__wrapped__(single=True, job="job-test")
        self.assertEqual(r.undone["module"], "sigmoid")
        self.assertEqual(r.undone_group, [])
        self.assertEqual([e.undone for e in journal.read_all("job-test")], [False, True])

    def test_ungrouped_writes_behave_as_before(self) -> None:
        darkroom.set_param.__wrapped__("exposure", "exposure", 1.8, commit=False, job="job-test")
        darkroom.set_param.__wrapped__("sigmoid", "contrast", 1.6, commit=False, job="job-test")
        r = darkroom.undo_last.__wrapped__(job="job-test")
        self.assertEqual(r.undone["module"], "sigmoid")
        self.assertEqual([e.undone for e in journal.read_all("job-test")], [False, True])

    def test_group_dry_run_lists_every_member_and_changes_nothing(self) -> None:
        self._two_moves()
        r = darkroom.undo_last.__wrapped__(dry_run=True, job="job-test")
        self.assertEqual(r.outcome, "dry_run")
        self.assertEqual(len(r.plan), 2)
        self.assertFalse(any(e.undone for e in journal.read_all("job-test")))
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --extra dev python -m pytest tests/test_say.py -q`
Expected: FAIL (`AttributeError: module 'scouter.core.moves' has no attribute 'say'`).

- [ ] **Step 3a: Implement `say` in `scouter/core/moves.py`**

```python
from scouter.core import journal, phrases  # add to the imports
from scouter.types import SayResult        # add to the imports


def _refusal(mv_module: str, param: str, kind: str) -> dict | None:
    s = vocab.spec(mv_module, param)
    if s is None:
        return {"module": mv_module, "param": param, "code": "usage", "status": "missing"}
    try:
        gate(s, relative=(kind == "adjust"))
    except ScouterError as e:
        return {"module": mv_module, "param": param, "code": e.code, "status": vocab.status(s),
                "message": e.message}
    return None


@metrics.instrumented("darkroom.say")
def say(phrase: str, *, amount: float | None = None, commit: bool = True,
        trust_sidecar: bool = False, educative: str | None = None, dry_run: bool = False,
        job: str | None = None) -> SayResult:
    """A plain-words edit: every move of the phrase, as one undoable group (§6.3)."""
    job = job or current_job()
    res = phrases.resolve(phrase, amount)
    level = explain.resolve_level(educative)
    if dry_run:
        return SayResult(
            outcome="dry_run", phrase=phrase, key=res.key, intensity=res.intensity,
            moves=[phrases.move_view(mv) for mv in res.moves],
            reason="dry run: no darktable calls; @tone was not routed against the image",
        )
    resolved = darkroom._current_image()
    darkroom._require_job_image(job, resolved.image_id)
    dtbridge.require_darkroom()
    tone = route_tone(resolved.path)
    concrete = [(tone if mv.module == phrases.TONE else mv.module, mv) for mv in res.moves]
    refused = [r for r in (_refusal(m, mv.param, mv.kind) for m, mv in concrete) if r]
    if refused:
        first = refused[0]
        cls = {"not_pinned": NotPinned, "value_unknown": ValueUnknown}.get(first["code"], UsageError)
        raise cls(
            f"'{phrase}' cannot run: {len(refused)} of {len(concrete)} move(s) are not reachable "
            f"({', '.join(r['module'] + '.' + r['param'] for r in refused)})",
            detail={"phrase": phrase, "key": res.key, "refused": refused},
        )
    group = f"say-{journal.next_seq(job)}"
    warning = None if trust_sidecar else _precommit(job, resolved.image_id)
    landed: list[dict] = []
    notes: list[explain.MoveNote] = []
    failures: list[dict] = []
    token = journal.GROUP.set(group)
    try:
        for module, mv in concrete:
            s = vocab.spec(module, mv.param)
            try:
                if mv.kind == "adjust":
                    delta = mv.value if mv.notches is None else mv.notches * s.step
                    r = _adjust_core(module, mv.param, delta, resolved=resolved, instance=0,
                                     commit=commit, level="off", job=job)
                    notes.append(explain.MoveNote(s, r.previous, r.target))
                    landed.append(r.to_dict())
                else:
                    target, _ = vocab.clamp(s, float(mv.value))
                    set_fn = getattr(darkroom.set_param, "__wrapped__", darkroom.set_param)
                    r = set_fn(module, mv.param, target, commit=commit, job=job)
                    notes.append(explain.MoveNote(s, r.previous, target, "set"))
                    landed.append({**r.to_dict(), "target": target,
                                   "summary": f"{s.ui}: set to {explain.with_unit(s, f'{vocab.to_display(s, target):.4g}')}"})
            except ScouterError as e:
                if not landed:
                    raise
                failures.append({"module": module, "param": mv.param, "code": e.code,
                                 "message": e.message})
                break
    finally:
        journal.GROUP.reset(token)
    return SayResult(
        outcome="partial" if failures else "ok",
        image_id=resolved.image_id, path=resolved.path, phrase=phrase, key=res.key,
        intensity=res.intensity, group=group, moves=landed, refused=failures,
        summary="; ".join(m.get("summary", "") for m in landed),
        explain=explain.lines(notes, level=level, job=job),
        warnings=[w for w in [warning] if w],
    )
```

Register `darkroom.say` next to `darkroom.adjust`:

```python
register(
    Verb(
        id="darkroom.say",
        group="darkroom",
        fn=say,
        args=(
            ArgSpec("phrase", "str", required=True, positional=True,
                    help="plain words, e.g. 'a touch warmer' (quote it)"),
            ArgSpec("amount", "float", default=None,
                    help="multiplier on the phrase's step (0-5]"),
            darkroom.COMMIT_ARG,
            TRUST_ARG,
            EDUCATIVE_ARG,
            darkroom.DRY_RUN_ARG,
        ),
        writes=True,
        gated=False,
        summary="Edit by a plain-words phrase; its moves undo together.",
    )
)
```

**Note on `outcome="partial"`:** `registry.call` maps any outcome other than `ok`/`dry_run` to `ok=False`, and `EXIT_CODES["partial"] == 9` exists (phase 2), so no change is needed there.

- [ ] **Step 3b: Refactor `undo_last` in `scouter/core/darkroom.py`**

Rename the current `undo_last` function to `_undo_one`, **remove its `@metrics.instrumented` decorator**, and keep its body byte-identical. Then add, directly after it:

```python
@metrics.instrumented("darkroom.undo_last")
def undo_last(*, single: bool = False, dry_run: bool = False, job: str | None = None) -> UndoResult:
    """Revert the newest Scouter MOVE in this job (phase 3 §6.4).

    A `darkroom.say` phrase is one move of several writes sharing a journal
    `group`; every other write is a move of one, so phase 1/2 behaviour is
    unchanged. `single=True` reverts only the newest write.
    """
    job = job or current_job()
    newest = journal.latest_open(job)
    if single or newest is None or newest.group is None:
        return _undo_one(dry_run=dry_run, job=job)
    group = newest.group
    members = [e for e in reversed(journal.read_all(job))
               if e.group == group and not e.undone
               and not (e.outcome or "").startswith("error")]
    if dry_run:
        return UndoResult(
            outcome="dry_run", image_id=newest.image_id, path=newest.path, seq=newest.seq,
            plan=[{"action": "darkroom.undo_last", "seq": e.seq, "module": e.module,
                   "param": e.param, "restore_to": e.before.get("value")} for e in members],
            reason=f"would undo {len(members)} write(s) of move {group}, newest first",
        )
    results: list[dict] = []
    try:
        while True:
            entry = journal.latest_open(job)
            if entry is None or entry.group != group:
                break
            results.append(_undo_one(dry_run=False, job=job).to_dict())
    except ScouterError as e:
        if not results:
            raise
        return UndoResult(
            outcome="partial", image_id=newest.image_id, path=newest.path, seq=newest.seq,
            undone=results[0].get("undone"), undone_group=results, committed=False,
            reason=f"undid {len(results)} of move {group}, then failed: {e.code}: {e.message}",
        )
    return UndoResult(
        image_id=newest.image_id, path=newest.path, seq=newest.seq,
        undone=results[0].get("undone") if results else None, undone_group=results,
        committed=all(r.get("committed") for r in results),
        warnings=[w for r in results for w in r.get("warnings", [])],
    )
```

Update the `darkroom.undo_last` Verb's args to `(ArgSpec("single", "bool", default=False, help="undo only the newest write, not the whole move it was part of"), DRY_RUN_ARG)`. Update the `darkroom_undo_last` MCP schema's properties to add `"single": {"type": "boolean", "default": False, "description": "undo only the newest write, not the whole phrase it was part of"}`, and change its description to begin "Revert the newest Scouter move (a darkroom_say phrase is one move)". Add the `darkroom_say` tool (description verbatim from spec §14.2, properties `phrase: PHRASE, amount: AMOUNT, commit, trust_sidecar, educative: EDUCATIVE, dry_run: DRY_RUN`, required `["phrase"]`). Contract files: `SMOKE_ARGS["darkroom.say"] = {"phrase": "brighter", "dry_run": True}`, `EXPECTED_VERBS` += `"darkroom.say"`, regenerate the MCP golden. Check `grep -rn "undo_last" tests/` for calls that pass positional args. There are none today, so the existing `tests/test_darkroom_core.py` undo tests must pass unchanged.

- [ ] **Step 4: Run to verify pass**

Run: `uv run --extra dev python -m pytest tests/test_say.py tests/test_darkroom_core.py -q && uv run --extra dev python -m pytest tests -q`
Expected: `test_say.py` 13 passed, `test_darkroom_core.py` unchanged count, full suite `0 failed`. (In step 2, also run `uv run --extra dev python -m pytest tests/test_darkroom_core.py -q | tail -1` and note the count.)

- [ ] **Step 5: Commit**

```bash
git add scouter/core/moves.py scouter/core/darkroom.py scouter/mcp/schemas.py tests/test_say.py tests/scouter_smoke_args.py tests/test_layer_parity.py tests/fixtures/scouter_mcp_golden.json
git commit -m "phase 3 task 6: darkroom.say (grouped moves) + undo by move group (MCP golden: +darkroom_say, undo_last.single)"
```

**Validation contract (done when):**
- `uv run --extra dev python -m pytest tests/test_say.py -q` → `13 passed`.
- `uv run --extra dev python -m pytest tests/test_darkroom_core.py -q` → the same `N passed` it printed before this task (recorded in step 2), and `0 failed`.
- `uv run --extra dev python -m scouter darkroom say "teal and orange"; echo "exit=$?"` → `error:not_pinned`, `exit=21`.
- `uv run --extra dev python -m scouter darkroom say "a touch warmer" --dry-run | python3 -c "import json,sys; print(json.load(sys.stdin)['data']['moves'][0]['delta'])"` → `150.0`.
- `uv run --extra dev python -m scouter darkroom undo-last --help | grep -c -- --single` → `1`.

---

### Task 7: Presets — `preset.save` and `preset.list`

**Files:**
- Create: `scouter/core/presets.py`
- Modify: `scouter/registry.py` (`_CORE_MODULES` += `"scouter.core.presets"`), `scouter/mcp/schemas.py` (`preset_save`, `preset_list`, fragment `PRESET_NAME`) + contract files
- Test: `tests/test_presets.py`

**Interfaces:**
- Consumes: `style.extract.__wrapped__`, `style._existing_styles`, `config.load`, `Config.root()`/`library_root`/`style.never_include`, `darkroom._current_image`/`_require_job_image`/`_commit`, `dtbridge.require_darkroom`, `journal.read_all`, `paths.validate_style_name`, `explain.preset_lines`/`resolve_level`, `types.PresetResult`/`PresetListResult`.
- Produces: `presets.memory_dir(cfg) -> Path | None`, `presets.session_modules(job, image_id) -> list[str]`, `presets._read_index(dir) -> dict`, `presets._write_index(dir, data) -> None`, verbs `preset.save` → `save(name, *, modules=None, description="", educative=None, confirm=False, dry_run=False, job=None) -> PresetResult` and `preset.list` → `list_presets(*, job=None) -> PresetListResult`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_presets.py
"""Phase 3 task 7: save the session's edits as a named darktable style; list presets."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest import mock

from scouter.core import darkroom, journal, presets
from scouter.errors import UsageError
from scouter.types import StyleResult
from tests.test_moves import _MovesFixture


class _PresetFixture(_MovesFixture):
    def setUp(self) -> None:
        super().setUp()
        self.library = self.cache.root / "library"
        self.library.mkdir()
        cfg = Path(os.environ["SCOUTER_CONFIG"])
        cfg.write_text(json.dumps({"library_root": str(self.library)}))
        self.bridge.responses["list_styles"] = {"styles": [], "count": 0}
        self.extract_calls: list[dict] = []
        dtstyle = self.cache.root / "styles" / "harbour-warm.dtstyle"
        dtstyle.parent.mkdir(parents=True, exist_ok=True)
        dtstyle.write_text("<darktable_style/>")

        def fake_extract(image, name, modules, **kw):
            self.extract_calls.append({"image": image, "name": name, "modules": list(modules), **kw})
            return StyleResult(name=name, created=True, dtstyle_path=str(dtstyle),
                               operations_included=list(modules), modules_honoured=True)

        p = mock.patch("scouter.core.style.extract.__wrapped__", side_effect=fake_extract)
        p.start()
        self.addCleanup(p.stop)

    def _edit(self) -> None:
        darkroom.set_param.__wrapped__("exposure", "exposure", 1.8, commit=False, job="job-test")
        darkroom.set_param.__wrapped__("sigmoid", "contrast", 1.6, commit=False, job="job-test")


class PresetSaveTest(_PresetFixture):
    def test_modules_default_to_this_sessions_edits(self) -> None:
        self._edit()
        r = presets.save.__wrapped__("harbour-warm", job="job-test")
        self.assertEqual(self.extract_calls[0]["modules"], ["exposure", "sigmoid"])
        self.assertEqual(r.modules, ["exposure", "sigmoid"])
        self.assertEqual(self.commits[0], (1, False), "flush before extracting")

    def test_session_modules_skip_undone_and_failed_writes(self) -> None:
        self._edit()
        darkroom.undo_last.__wrapped__(single=True, job="job-test")
        self.assertEqual(presets.session_modules("job-test", 12), ["exposure"])

    def test_nothing_edited_is_usage(self) -> None:
        with self.assertRaises(UsageError):
            presets.save.__wrapped__("harbour-warm", job="job-test")

    def test_never_include_is_dropped_from_the_default(self) -> None:
        with mock.patch.object(journal, "read_all", return_value=[
            journal.Entry(seq=1, ts="t", verb="darkroom.set", image_id=12, path="p",
                          module="crop", param="cx", instance=0, after={"value": 1.0}),
            journal.Entry(seq=2, ts="t", verb="darkroom.set", image_id=12, path="p",
                          module="exposure", param="exposure", instance=0, after={"value": 1.0}),
        ]):
            self.assertEqual(presets.session_modules("job-test", 12), ["exposure"])

    def test_memory_copy_and_index(self) -> None:
        self._edit()
        r = presets.save.__wrapped__("harbour-warm", job="job-test")
        mem = self.library / "scouter" / "memory" / "presets"
        self.assertEqual(Path(r.memory_path), mem / "harbour-warm.dtstyle")
        index = json.loads((mem / "presets.json").read_text())
        self.assertEqual(index["schema"], 1)
        self.assertEqual(index["presets"][0]["name"], "harbour-warm")
        self.assertEqual(index["presets"][0]["modules"], ["exposure", "sigmoid"])

    def test_resaving_keeps_the_old_file(self) -> None:
        self._edit()
        presets.save.__wrapped__("harbour-warm", job="job-test")
        presets.save.__wrapped__("harbour-warm", confirm=True, job="job-test")
        mem = self.library / "scouter" / "memory" / "presets"
        self.assertEqual(len(list(mem.glob("harbour-warm.*.dtstyle"))), 1)
        self.assertEqual(len(json.loads((mem / "presets.json").read_text())["presets"]), 1)

    def test_no_library_root_warns_and_still_saves(self) -> None:
        Path(os.environ["SCOUTER_CONFIG"]).write_text("{}")
        self._edit()
        r = presets.save.__wrapped__("harbour-warm", job="job-test")
        self.assertIsNone(r.memory_path)
        self.assertTrue(any("library_root" in w for w in r.warnings))

    def test_bad_name_is_usage_before_anything(self) -> None:
        with self.assertRaises(UsageError):
            presets.save.__wrapped__("../etc/x", job="job-test")
        self.assertEqual(self.bridge.calls, [])

    def test_dry_run_makes_no_bridge_calls(self) -> None:
        r = presets.save.__wrapped__("harbour-warm", dry_run=True, job="job-test")
        self.assertEqual(r.outcome, "dry_run")
        self.assertEqual(self.bridge.calls, [])

    def test_explain_line(self) -> None:
        self._edit()
        r = presets.save.__wrapped__("harbour-warm", educative="brief", job="job-test")
        self.assertIn("as style 'harbour-warm'", r.explain[0])


class PresetListTest(_PresetFixture):
    def test_list_merges_darktable_and_the_index(self) -> None:
        self._edit()
        presets.save.__wrapped__("harbour-warm", job="job-test")
        self.bridge.responses["list_styles"] = {"styles": [{"name": "someone-elses"}], "count": 1}
        r = presets.list_presets.__wrapped__(job="job-test")
        self.assertEqual(r.missing_in_darktable, ["harbour-warm"])
        self.assertEqual(r.darktable_only, ["someone-elses"])
        row = next(p for p in r.presets if p["name"] == "harbour-warm")
        self.assertEqual((row["scouter"], row["in_darktable"]), (True, False))
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --extra dev python -m pytest tests/test_presets.py -q`
Expected: FAIL (`ImportError`).

- [ ] **Step 3: Implement `scouter/core/presets.py` (save + list)**

```python
"""Named presets (phase 3 design §8). A preset IS a darktable style.

Scouter adds a default module list (this session's edits), a durable copy in
`<library_root>/scouter/memory/presets/` (the directory phase 2 reserved,
inside the backup set) and apply-to-the-open-image through a bounce (task 8).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from scouter import __version__, config, metrics, paths
from scouter.core import darkroom, dtbridge, explain, journal, style
from scouter.errors import ScouterError, UsageError
from scouter.paths import current_job
from scouter.registry import ArgSpec, Verb, register
from scouter.types import PresetListResult, PresetResult

INDEX = "presets.json"


def _now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def memory_dir(cfg: config.Config) -> Path | None:
    if not cfg.library_root:
        return None
    return Path(cfg.library_root).expanduser() / "scouter" / "memory" / "presets"


def session_modules(job: str, image_id: int) -> list[str]:
    """Modules this job actually changed on this image, first-touched order (P3-R9)."""
    never = set(config.load().style.never_include)
    out: list[str] = []
    for e in journal.read_all(job):
        if e.image_id != image_id or e.undone or not e.module:
            continue
        if (e.outcome or "").startswith("error") or e.verb in ("module.reset", "preset.apply"):
            continue
        if e.after is None:
            continue
        if e.module in never or e.module in out:
            continue
        out.append(e.module)
    return out


def _read_index(directory: Path) -> dict:
    path = directory / INDEX
    if not path.exists():
        return {"schema": 1, "presets": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != 1:
        raise UsageError(f"{path} is not a schema-1 preset index", detail={"path": str(path)})
    return data


def _write_index(directory: Path, data: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".presets-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, directory / INDEX)


def _source_path(cfg: config.Config, path: str) -> str:
    if cfg.library_root:
        try:
            return str(Path(path).resolve().relative_to(Path(cfg.library_root).expanduser().resolve()))
        except ValueError:
            pass
    return path


@metrics.instrumented("preset.save")
def save(name: str, *, modules: list[str] | None = None, description: str = "",
         educative: str | None = None, confirm: bool = False, dry_run: bool = False,
         job: str | None = None) -> PresetResult:
    """Save the darkroom image's session edits as a named darktable style."""
    job = job or current_job()
    paths.validate_style_name(name)
    level = explain.resolve_level(educative)
    if dry_run:
        return PresetResult(
            outcome="dry_run", name=name, action="save", modules=list(modules or []),
            plan=[{"action": "preset.save", "name": name,
                   "modules": list(modules) if modules else "this session's edits"}],
            reason="dry run: no darktable calls; the darkroom image was not resolved",
        )
    resolved = darkroom._current_image()
    darkroom._require_job_image(job, resolved.image_id)
    dtbridge.require_darkroom()
    warnings: list[str] = []
    try:
        darkroom._commit(job, resolved.image_id, strategy="auto", attempts=1, expect_change=False)
    except ScouterError as e:
        warnings.append(f"flush before saving did not confirm ({e.code}); the style reflects the "
                        "last committed sidecar")
    chosen = list(modules) if modules else session_modules(job, resolved.image_id)
    if not chosen:
        raise UsageError(
            "nothing was edited on this image in this session; pass --modules",
            detail={"image_id": resolved.image_id, "job": job},
        )
    extract = getattr(style.extract, "__wrapped__", style.extract)
    sr = extract(resolved.image_id, name, chosen,
                 description=description or f"Scouter preset from {Path(resolved.path).name}",
                 confirm=confirm, job=job)
    if sr.outcome == "dry_run":
        return PresetResult(outcome="dry_run", name=name, action="save", modules=chosen,
                            plan=sr.plan, reason=sr.reason or "a style of that name exists; "
                            "re-run with confirm=true / --yes to replace it")
    warnings.extend(sr.warnings)
    cfg = config.load()
    mem = memory_dir(cfg)
    memory_path: str | None = None
    replaced = False
    if mem is None:
        warnings.append("saved in darktable and in the styles cache only; set library_root to "
                        "keep presets in the backed-up library memory")
    elif sr.dtstyle_path:
        mem.mkdir(parents=True, exist_ok=True)
        dest = mem / f"{name}.dtstyle"
        if dest.exists():
            dest.rename(mem / f"{name}.{_now_z().replace(':', '')}.dtstyle")
            replaced = True
        shutil.copyfile(sr.dtstyle_path, dest)
        memory_path = str(dest)
        index = _read_index(mem)
        index["presets"] = [p for p in index["presets"] if p.get("name") != name]
        index["presets"].append({
            "name": name, "created_at": _now_z(),
            "source_path": _source_path(cfg, resolved.path), "image_id": resolved.image_id,
            "modules": list(sr.operations_included or chosen), "dtstyle": dest.name,
            "sha256": hashlib.sha256(dest.read_bytes()).hexdigest(),
            "description": description, "scouter_version": __version__,
        })
        _write_index(mem, index)
    included = list(sr.operations_included or chosen)
    return PresetResult(
        image_id=resolved.image_id, path=resolved.path, name=name, action="save",
        modules=included, dtstyle_path=sr.dtstyle_path, memory_path=memory_path,
        replaced=replaced or sr.created is False, warnings=warnings,
        explain=explain.preset_lines("save", name, included, len(included), level=level, job=job),
    )


@metrics.instrumented("preset.list")
def list_presets(*, job: str | None = None) -> PresetListResult:
    """darktable's styles merged with Scouter's preset index (one bridge call)."""
    job = job or current_job()
    in_dt = set(style._existing_styles())
    mem = memory_dir(config.load())
    index = _read_index(mem) if mem else {"presets": []}
    ours = {p["name"]: p for p in index["presets"]}
    rows = []
    for name in sorted(set(ours) | in_dt):
        p = ours.get(name, {})
        rows.append({"name": name, "scouter": name in ours, "in_darktable": name in in_dt,
                     "modules": p.get("modules", []), "created_at": p.get("created_at")})
    missing = sorted(n for n in ours if n not in in_dt)
    warnings = [f"{n} is in Scouter's index but not in darktable: re-import "
                f"{mem / (n + '.dtstyle')} in darktable's styles module, or save it again"
                for n in missing] if mem else []
    return PresetListResult(presets=rows, darktable_only=sorted(in_dt - set(ours)),
                            missing_in_darktable=missing, warnings=warnings)


PRESET_NAME_ARG = ArgSpec("name", "str", required=True, positional=True,
                          help="preset (darktable style) name")
EDUCATIVE_ARG = ArgSpec("educative", "str", default=None, choices=("off", "brief", "full"),
                        help="explanation level for this call (default: config's educative)")

for verb in (
    Verb(
        id="preset.save",
        group="preset",
        fn=save,
        args=(
            PRESET_NAME_ARG,
            ArgSpec("modules", "list[str]", default=None,
                    help="modules to keep (default: those changed in this session)"),
            ArgSpec("description", "str", default="", help="style description"),
            EDUCATIVE_ARG,
            ArgSpec("confirm", "bool", default=False,
                    help="required only to replace an existing preset of this name"),
            ArgSpec("dry_run", "bool", default=False, help="plan only, send nothing"),
        ),
        writes=True,
        gated=True,
        summary="Save this session's edits on the darkroom image as a named preset (style).",
    ),
    Verb(
        id="preset.list",
        group="preset",
        fn=list_presets,
        args=(),
        writes=False,
        gated=False,
        summary="List presets: darktable's styles merged with Scouter's index.",
    ),
):
    register(verb)
```

Confirm `style.extract`'s existing-name behaviour returns `outcome="dry_run"` without `confirm` (it is gated: "`confirm` is required only when a style of that name already exists"). If it raises instead, catch that same class and return `dry_run`. The test `test_resaving_keeps_the_old_file` fakes extract, so write one more test that uses the **real** `style.extract` against the fixture XMP. Do not patch `style.extract` in that test; add `"style.import_file": {"imported": True, "name": "real-one", "replaced": False}` and `"list_styles"` responses to the FakeBridge. Assert `r.dtstyle_path` ends with `real-one.dtstyle` and that the file contains `<operation>exposure</operation>`.

MCP: add the `PRESET_NAME` fragment (spec §14.2) and the tools:

```python
    {
        "name": "preset_save",
        "description": "Save the darkroom image's edits from this session as a named darktable "
        "style (a preset/filter). Modules default to the ones changed in this session; pass "
        "modules to choose. Needs confirm=true only to replace an existing preset of that name.",
        "inputSchema": _schema(
            {"name": PRESET_NAME,
             "modules": {"type": "array", "items": {"type": "string"},
                         "description": "modules to keep (default: this session's edits)"},
             "description": {"type": "string", "default": "", "description": "style description"},
             "educative": EDUCATIVE,
             "confirm": {"type": "boolean", "default": False,
                         "description": "required only to replace an existing preset of this name"},
             "dry_run": DRY_RUN},
            ["name"],
        ),
    },
    {
        "name": "preset_list",
        "description": "List presets: darktable's styles merged with Scouter's saved-preset "
        "index, flagging any deleted in darktable.",
        "inputSchema": _schema({}, []),
    },
```

Contract files: `SMOKE_ARGS["preset.save"] = {"name": "smoke", "dry_run": True}`, `SMOKE_ARGS["preset.list"] = {}`, `EXPECTED_VERBS` += both, regenerate the golden.

- [ ] **Step 4: Run to verify pass**

Run: `uv run --extra dev python -m pytest tests/test_presets.py -q && uv run --extra dev python -m pytest tests -q`
Expected: `12 passed`, then the full suite `0 failed`. The real-extract test lives in its own class, `PresetRealExtractTest(_MovesFixture)`, so `_PresetFixture`'s extract patch never applies to it.

- [ ] **Step 5: Commit**

```bash
git add scouter/core/presets.py scouter/registry.py scouter/mcp/schemas.py tests/test_presets.py tests/scouter_smoke_args.py tests/test_layer_parity.py tests/fixtures/scouter_mcp_golden.json
git commit -m "phase 3 task 7: preset.save (session modules, library memory) + preset.list (MCP golden: +preset_save, +preset_list)"
```

**Validation contract (done when):**
- `uv run --extra dev python -m pytest tests/test_presets.py -q` → `12 passed`.
- `uv run --extra dev python -m scouter preset save harbour-warm --dry-run; echo "exit=$?"` → `"outcome": "dry_run"`, `exit=5`.
- `uv run --extra dev python -m scouter preset save ../x; echo "exit=$?"` → `error:usage`, `exit=2`.
- `grep -n "library.db" scouter/core/presets.py` → no output.

---

### Task 8: Presets — `preset.apply` (bounce), `preset.apply_job`, the Lua method, the undo refusal

**Files:**
- Modify: `scouter/core/presets.py` (add `apply`, `apply_job`)
- Modify: `scouter/core/darkroom.py` (`_undo_one`: refuse a `preset.apply` entry)
- Modify: `darktable_mcp/lua/darktable_mcp.lua` (`commit_view_bounce(between)`, `methods["darkroom.apply_style"]`)
- Modify: `darktable_mcp/bridge/client.py` (`DEFAULT_TIMEOUTS["darkroom.apply_style"] = 30.0`)
- Modify: `tests/lua/test_dispatcher.lua`, `scouter/mcp/schemas.py` (`preset_apply`, `preset_apply_job`) + contract files
- Test: `tests/test_presets_apply.py`

**Interfaces:**
- Consumes: `style.apply.__wrapped__`, `style.apply_set.__wrapped__`, `style._existing_styles`, `darkroom._current_image`, `darkroom._read_sidecar_parsed`, `darkroom._commit`, `darkroom._UnsupportedUndo`, `journal.begin/complete/fail`, `explain.preset_lines`.
- Produces: verbs `preset.apply` → `apply(name, image=None, *, educative=None, dry_run=False, job=None) -> PresetResult`; `preset.apply_job` → `apply_job(name, *, job_slug, exclude=None, force=False, confirm=False, dry_run=False, job=None) -> StyleSetResult`. Bridge method `darkroom.apply_style {image_id, name} -> {applied, name, image_id, view, left_ms, returned_ms}`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_presets_apply.py
"""Phase 3 task 8: apply a preset to the open image (bounce) or another image; to a job."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from scouter.core import darkroom, journal, presets
from scouter.errors import CommitTimeout, UsageError
from scouter.types import StyleResult, StyleSetResult
from tests.test_moves import _MovesFixture


class PresetApplyTest(_MovesFixture):
    def setUp(self) -> None:
        super().setUp()
        self.bridge.responses["list_styles"] = {"styles": [{"name": "harbour-warm"}], "count": 1}
        self.history_end = iter([14, 16])
        he = mock.patch("scouter.core.presets._history_end",
                        side_effect=lambda path: next(self.history_end))
        he.start()
        self.addCleanup(he.stop)
        self.bridge.responses["darkroom.apply_style"] = lambda p: {
            "applied": True, "name": p["name"], "image_id": p["image_id"], "view": "darkroom",
            "left_ms": 150, "returned_ms": 50}

    def test_open_image_goes_through_the_bounce_method(self) -> None:
        r = presets.apply.__wrapped__("harbour-warm", job="job-test")
        self.assertIn("darkroom.apply_style", [m for m, _ in self.bridge.calls])
        self.assertNotIn("style.apply", [m for m, _ in self.bridge.calls])
        self.assertEqual((r.applied_to, r.history_end_before, r.history_end_after),
                         ("darkroom", 14, 16))
        self.assertEqual(self.commits[-1], (None, True))
        entry = journal.read_all("job-test")[-1]
        self.assertEqual((entry.verb, entry.after["history_end"]), ("preset.apply", 16))

    def test_history_that_did_not_advance_is_commit_timeout(self) -> None:
        self.history_end = iter([14, 14])
        with self.assertRaises(CommitTimeout) as cm:
            presets.apply.__wrapped__("harbour-warm", job="job-test")
        self.assertEqual((cm.exception.detail["before"], cm.exception.detail["after"]), (14, 14))

    def test_another_image_uses_the_library_path(self) -> None:
        with mock.patch("scouter.core.style.apply.__wrapped__",
                        return_value=StyleResult(name="harbour-warm", applied=1, image_id=7)) as ap:
            r = presets.apply.__wrapped__("harbour-warm", "7", job="job-test")
        ap.assert_called_once()
        self.assertEqual(r.applied_to, "library")
        self.assertNotIn("darkroom.apply_style", [m for m, _ in self.bridge.calls])

    def test_unknown_style_is_usage_with_suggestions(self) -> None:
        with self.assertRaises(UsageError) as cm:
            presets.apply.__wrapped__("harbour-wram", job="job-test")
        self.assertIn("harbour-warm", cm.exception.detail["suggestions"])

    def test_undo_of_a_preset_application_is_refused_and_marked(self) -> None:
        darkroom.set_param.__wrapped__("exposure", "exposure", 1.8, commit=False, job="job-test")
        presets.apply.__wrapped__("harbour-warm", job="job-test")
        with self.assertRaises(darkroom._UnsupportedUndo) as cm:
            darkroom.undo_last.__wrapped__(job="job-test")
        self.assertIn("2 history item(s)", str(cm.exception))
        r = darkroom.undo_last.__wrapped__(job="job-test")
        self.assertEqual(r.undone["module"], "exposure", "the older write stays reachable")

    def test_dry_run_sends_nothing(self) -> None:
        r = presets.apply.__wrapped__("harbour-warm", dry_run=True, job="job-test")
        self.assertEqual(r.outcome, "dry_run")
        self.assertEqual(self.bridge.calls, [])


class PresetApplyJobTest(_MovesFixture):
    def test_delegates_to_apply_set_unchanged(self) -> None:
        sentinel = StyleSetResult(outcome="dry_run")
        with mock.patch("scouter.core.style.apply_set.__wrapped__", return_value=sentinel) as a:
            r = presets.apply_job.__wrapped__("harbour-warm", job_slug="harbour-wedding",
                                              exclude=["1"], job="job-test")
        self.assertIs(r, sentinel)
        _, kwargs = a.call_args
        self.assertEqual((kwargs["job_slug"], kwargs["exclude"], kwargs["confirm"]),
                         ("harbour-wedding", ["1"], False))
```

Add to `tests/lua/test_dispatcher.lua`, following that file's existing stub pattern for `style.apply` and the view bounce:

```lua
-- phase 3 task 8: darkroom.apply_style applies INSIDE the bounce (lighttable leg)
do
  local order = {}
  local style = {name = "harbour-warm"}
  local image = {id = 12, apply_style = function(self, s) table.insert(order, "apply:" .. s.name .. "@" .. stub_view()) end}
  stub_styles({style})
  stub_darkroom_image(image)
  local r = call("darkroom.apply_style", {image_id = 12, name = "harbour-warm"})
  assert(r.applied == true, "applied")
  assert(order[1] == "apply:harbour-warm@lighttable", "applied in the lighttable, got " .. tostring(order[1]))
  assert(r.view == "darkroom", "returned to the darkroom")
  local ok, err = pcall(call, "darkroom.apply_style", {image_id = 99, name = "harbour-warm"})
  assert(not ok and tostring(err):match("wrong image loaded:"), "refuses another image")
end
```

If `stub_view`, `stub_styles`, `stub_darkroom_image` or `call` are named differently in the harness, use its existing equivalents. Read `tests/lua/test_dispatcher.lua` first and **do not** add a second stubbing mechanism.

- [ ] **Step 2: Run to verify failure**

Run: `uv run --extra dev python -m pytest tests/test_presets_apply.py -q; lua tests/lua/test_dispatcher.lua`
Expected: FAIL (`AttributeError: ... 'apply'`), and the Lua assertion fails on the unknown method.

- [ ] **Step 3a: Lua**

In `commit_view_bounce`, add the parameter `between` and call it once after the lighttable dwell (after `dt.control.sleep(VIEW_BOUNCE_DWELL_MS)` and before leg 2):

```lua
local function commit_view_bounce(between)
  -- ... existing leg 1 and dwell unchanged ...
  if between then
    local ok_between, between_err = pcall(between)
    if not ok_between then
      -- still return to the darkroom below; report the failure after
      between_error = tostring(between_err)
    end
  end
  -- ... existing leg 2 unchanged; add `between_error = between_error` to the returned table ...
```

Declare `local between_error = nil` at the top of the function. Existing callers pass nothing, so the bounce is unchanged for them. Then:

```lua
methods["darkroom.apply_style"] = function(p)
  p = p or {}
  if type(p.name) ~= "string" or p.name == "" then error("darkroom.apply_style: name required") end
  local want = tonumber(p.image_id)
  if not want then error("darkroom.apply_style: image_id required") end
  local style = nil
  for _, s in ipairs(dt.styles) do
    if s.name == p.name then style = s; break end
  end
  if not style then error(string.format("darkroom.apply_style: style %q not found", p.name)) end
  local image = darkroom_image()
  if not image or not ids_match(image.id, want) then
    error(string.format("wrong image loaded: darkroom shows %s, requested %d",
                        tostring(image and image.id), want))
  end
  local applied = false
  local started = os.clock()
  local result = commit_view_bounce(function()
    image:apply_style(style)
    applied = true
  end)
  if result.between_error then
    error("darkroom.apply_style: apply failed in the lighttable: " .. result.between_error)
  end
  if not result.ok then
    error("darkroom.apply_style: " .. tostring(result.error))
  end
  return {applied = applied, name = p.name, image_id = want, view = current_view_name(),
          left_ms = result.left_ms, returned_ms = result.returned_ms,
          wall_ms = math.floor((os.clock() - started) * 1000)}
end
```

`darkroom_image()` and `ids_match` already exist (they are used by `commit_view_bounce` and `darkroom.open`). If `commit_view_bounce` does not already return `left_ms`/`returned_ms`, return what it has (`waited` values from `wait_for_view`) under those names. `darktable_mcp/bridge/client.py`: add `"darkroom.apply_style": 30.0,` to `DEFAULT_TIMEOUTS`.

- [ ] **Step 3b: Python**

Append to `scouter/core/presets.py`:

```python
import difflib  # add to imports
from scouter.errors import CommitTimeout  # add to imports
from scouter.types import StyleSetResult  # add to imports


def _history_end(path: str) -> int | None:
    parsed = darkroom._read_sidecar_parsed(path)
    return None if parsed is None else int(parsed.history_end)


def _require_style(name: str) -> None:
    existing = style._existing_styles()
    if name not in existing:
        suggestions = difflib.get_close_matches(name, existing, n=3, cutoff=0.6)
        raise UsageError(f"no darktable style named {name!r}",
                         fix="`scouter preset list` shows every preset",
                         detail={"name": name, "suggestions": suggestions})


@metrics.instrumented("preset.apply")
def apply(name: str, image: str | int | None = None, *, educative: str | None = None,
          dry_run: bool = False, job: str | None = None) -> PresetResult:
    """Apply a preset to the open image (through a bounce) or to another image."""
    job = job or current_job()
    paths.validate_style_name(name)
    level = explain.resolve_level(educative)
    if dry_run:
        return PresetResult(outcome="dry_run", name=name, action="apply",
                            plan=[{"action": "preset.apply", "name": name, "image": image}],
                            reason="dry run: no darktable calls were made")
    _require_style(name)
    current = darkroom._current_image()
    target_is_open = image is None or str(image) in (str(current.image_id), current.path)
    if not target_is_open:
        applied = getattr(style.apply, "__wrapped__", style.apply)(image, name, job=job)
        return PresetResult(image_id=applied.image_id, path=applied.path, name=name,
                            action="apply", applied_to="library", warnings=applied.warnings,
                            explain=explain.preset_lines("apply", name, [], applied.applied,
                                                         level=level, job=job))
    darkroom._require_job_image(job, current.image_id)
    dtbridge.require_darkroom()
    before = _history_end(current.path)
    entry = journal.begin(job, verb="preset.apply", image_id=current.image_id, path=current.path,
                          module="", param="", instance=0, before={"history_end": before})
    try:
        dtbridge.call("darkroom.apply_style", {"image_id": current.image_id, "name": name})
        darkroom._commit(job, current.image_id, strategy="auto", expect_change=True)
    except ScouterError as e:
        journal.fail(job, entry.seq, outcome=f"error:{e.code}")
        raise
    after = _history_end(current.path)
    if before is None or after is None or after <= before:
        journal.fail(job, entry.seq, outcome="error:commit_timeout")
        raise CommitTimeout(
            f"style {name!r} was sent but the sidecar's history did not advance",
            detail={"before": before, "after": after, "name": name},
        )
    journal.complete(job, entry.seq, after={"history_end": after, "style": name})
    return PresetResult(
        image_id=current.image_id, path=current.path, name=name, action="apply",
        applied_to="darkroom", history_end_before=before, history_end_after=after,
        explain=explain.preset_lines("apply", name, [], after - before, level=level, job=job),
    )


@metrics.instrumented("preset.apply_job")
def apply_job(name: str, *, job_slug: str, exclude: list[str] | None = None, force: bool = False,
              confirm: bool = False, dry_run: bool = False,
              job: str | None = None) -> StyleSetResult:
    """A thin, always-gated delegate to style.apply_set (design §8.5)."""
    job = job or current_job()
    fn = getattr(style.apply_set, "__wrapped__", style.apply_set)
    return fn(name, job_slug=job_slug, exclude=exclude, force=force, confirm=confirm,
              dry_run=dry_run, job=job)
```

Register both, in the same `for verb in (...)` style as task 7:

```python
    Verb(
        id="preset.apply",
        group="preset",
        fn=apply,
        args=(PRESET_NAME_ARG,
              ArgSpec("image", "image", positional=True,
                      help="path or image id; default: the image open in the darkroom"),
              EDUCATIVE_ARG,
              ArgSpec("dry_run", "bool", default=False, help="plan only, send nothing")),
        writes=True,
        gated=False,
        summary="Apply a preset to the open image (live, via a bounce) or to another image.",
    ),
    Verb(
        id="preset.apply_job",
        group="preset",
        fn=apply_job,
        args=(PRESET_NAME_ARG,
              ArgSpec("job_slug", "str", required=True, help="job whose frames get the preset"),
              ArgSpec("exclude", "list[str]", default=None, help="paths or ids to leave out"),
              ArgSpec("force", "bool", default=False,
                      help="apply even where Scouter's ledger says it is already applied"),
              ArgSpec("confirm", "bool", default=False, help="required: a set write is gated"),
              ArgSpec("dry_run", "bool", default=False, help="plan only")),
        writes=True,
        gated=True,
        summary="Apply a preset to every frame of a job (batched, gated).",
    ),
```

In `scouter/core/darkroom.py`'s `_undo_one`, next to the existing `module.reset` refusal branch, add a branch shaped the same way:

```python
    if entry.verb == "preset.apply":
        before_end = entry.before.get("history_end")
        after_end = (entry.after or {}).get("history_end")
        items = (after_end - before_end) if isinstance(before_end, int) and isinstance(after_end, int) else "some"
        reason = (
            f"cannot undo preset '{(entry.after or {}).get('style', '?')}': a style application "
            f"appended {items} history item(s), and darktable's Lua API cannot remove history "
            "items. In darktable, click the history item just below the style's first item and "
            "compress history, or press Ctrl-Z"
        )
        if dry_run:
            return UndoResult(outcome="dry_run", image_id=entry.image_id, path=entry.path,
                              seq=entry.seq, reason=f"{reason} (this entry would be skipped, not undone)")
        journal.mark_undone(job, entry.seq, note="skipped: a style application is not undoable")
        raise _UnsupportedUndo(reason, detail={"seq": entry.seq, "style": (entry.after or {}).get("style")})
```

MCP tools `preset_apply` (properties `name: PRESET_NAME, image: IMAGE, educative: EDUCATIVE, dry_run: DRY_RUN`, required `["name"]`) and `preset_apply_job` (properties `name, job_slug` = phase 2's `JOB_SLUG` fragment, `exclude` array of strings, `force`, `confirm`, `dry_run`, required `["name", "job_slug"]`). Descriptions verbatim from spec §14.2. Contract files: `SMOKE_ARGS["preset.apply"] = {"name": "smoke", "dry_run": True}`, `SMOKE_ARGS["preset.apply_job"] = {"name": "smoke", "job_slug": "smoke-job"}`, `EXPECTED_VERBS` += both, regenerate the golden.

- [ ] **Step 4: Run to verify pass**

Run: `uv run --extra dev python -m pytest tests/test_presets_apply.py tests/test_lua_dispatcher.py tests/test_bridge.py -q && lua tests/lua/test_dispatcher.lua && uv run --extra dev python -m pytest tests -q`
Expected: `test_presets_apply.py` 7 passed, Lua harness exits 0, full suite `0 failed`. `tests/test_lua_dispatcher.py`'s timeout-versus-`STALE_AGE_SECONDS` invariant holds (30 < 600).

- [ ] **Step 5: Commit**

```bash
git add scouter/core/presets.py scouter/core/darkroom.py darktable_mcp/lua/darktable_mcp.lua darktable_mcp/bridge/client.py tests/lua/test_dispatcher.lua tests/test_presets_apply.py scouter/mcp/schemas.py tests/scouter_smoke_args.py tests/test_layer_parity.py tests/fixtures/scouter_mcp_golden.json
git commit -m "phase 3 task 8: preset.apply via darkroom.apply_style bounce, preset.apply_job, undo refusal (MCP golden: +preset_apply, +preset_apply_job)"
```

**Validation contract (done when):**
- `uv run --extra dev python -m pytest tests/test_presets_apply.py -q` → `7 passed`.
- `lua tests/lua/test_dispatcher.lua; echo "exit=$?"` → last line `exit=0`.
- `grep -c 'methods\["darkroom.apply_style"\]' darktable_mcp/lua/darktable_mcp.lua` → `1`.
- `uv run --extra dev python -m scouter preset apply-job harbour-warm --job-slug nosuch; echo "exit=$?"` → a non-zero exit, and no `Traceback` in the output.
- `uv run --extra dev python -c "from scouter import registry; print(len(registry.load_verbs()))"` → `44`.

---

### Task 9: Cheap closures and docs — MCP session job, WB table version, host restore scope, README

**Files:**
- Modify: `scouter/mcp/server.py` (`_pin_session_job`, called from `main`)
- Modify: `scouter/core/wb.py` (`load_table` refuses `version != 1`)
- Create: `tests/host_support.py` (moved `_PluginInstall` → `PluginInstall`, `_quit` → `quit_darktable`, plus `launch(image) -> Popen` and `wait_ready(budget_s) -> str`)
- Modify: `tests/test_host_phase2.py` (import from `tests.host_support`, no behaviour change)
- Modify: `scouter/__init__.py` (`__version__ = "0.3.0"`, `PHASE = 3`), `README.md`
- Test: `tests/test_phase3_closures.py`

**Interfaces:**
- Consumes: `paths.validate_job`, `wb.load_table`.
- Produces: `scouter.mcp.server._pin_session_job(environ: MutableMapping[str, str]) -> str`; `tests.host_support.PluginInstall` (`install()`, `restore()`, `touched_paths() -> list[Path]`), `quit_darktable(proc)`, `launch(image: Path) -> subprocess.Popen`, `wait_ready(budget_s: float = 120.0) -> str`, and constants `DARKTABLE`, `SCRATCH_DIR`, `HERO`, `INSTALLED_PLUGIN`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_phase3_closures.py
"""Phase 3 task 9: the cheap closures from phase 2 (design §11)."""

from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scouter import PHASE, __version__, paths
from scouter.core import wb
from scouter.mcp import server


class SessionJobTest(unittest.TestCase):
    def test_session_job_is_pinned_once_and_respects_an_explicit_one(self) -> None:
        env: dict[str, str] = {}
        job = server._pin_session_job(env)
        self.assertRegex(job, r"^mcp-\d{8}-\d{6}-\d+$")
        self.assertEqual(paths.validate_job(job), job)
        self.assertEqual(env["SCOUTER_JOB"], job)
        self.assertEqual(server._pin_session_job(env), job, "a second call keeps the same job")
        named = {"SCOUTER_JOB": "pratik-teaching"}
        self.assertEqual(server._pin_session_job(named), "pratik-teaching")

    def test_an_invalid_explicit_job_is_refused(self) -> None:
        from scouter.errors import UsageError
        with self.assertRaises(UsageError):
            server._pin_session_job({"SCOUTER_JOB": "../escape"})


class WbTableVersionTest(unittest.TestCase):
    def test_version_2_is_refused_with_the_file_named(self) -> None:
        fixture = json.loads((Path(__file__).parent / "fixtures" / "wb-em10iii.json").read_text())
        fixture["version"] = 2
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "wb").mkdir()
            (Path(tmp) / "wb" / "em10iii.json").write_text(json.dumps(fixture))
            with mock.patch.dict(os.environ, {"SCOUTER_CONFIG": str(Path(tmp) / "config.json")}):
                with self.assertRaises(ValueError) as cm:
                    wb.load_table("em10iii")
                self.assertIn("version 2", str(cm.exception))
                self.assertIsNone(wb.kelvin([2.0, 1.0, 1.7, 1.0], "em10iii")["kelvin"])


class HostSupportScopeTest(unittest.TestCase):
    def test_plugin_install_touches_only_the_plugin_and_luarc(self) -> None:
        from tests import host_support
        touched = {p.name for p in host_support.PluginInstall().touched_paths()}
        self.assertEqual(touched, {"darktable_mcp.lua", "luarc"})

    def test_no_host_file_snapshots_the_whole_darktable_config(self) -> None:
        for path in Path(__file__).parent.glob("test_host*.py"):
            text = path.read_text()
            with self.subTest(file=path.name):
                self.assertIsNone(re.search(r"copytree\([^)]*darktable", text),
                                  "ruling R22 / P3-R18: never snapshot ~/.config/darktable wholesale")


class VersionTest(unittest.TestCase):
    def test_phase_and_version(self) -> None:
        self.assertEqual((PHASE, __version__), (3, "0.3.0"))
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --extra dev python -m pytest tests/test_phase3_closures.py -q`
Expected: FAIL (`AttributeError: module 'scouter.mcp.server' has no attribute '_pin_session_job'`).

- [ ] **Step 3: Implement**

`scouter/mcp/server.py`:

```python
import os                                   # add
from collections.abc import MutableMapping  # add
from datetime import datetime, timezone     # add


def _pin_session_job(environ: MutableMapping[str, str]) -> str:
    """One MCP server process = one Scouter session (phase 3 §11.1).

    Without this, `paths.current_job()` derives `job-<UTC second>-<pid>` per
    call, so two tool calls a second apart write different journals and
    `darkroom_undo_last` finds nothing. An explicit SCOUTER_JOB wins.
    """
    existing = environ.get("SCOUTER_JOB")
    if existing:
        return paths.validate_job(existing)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    job = paths.validate_job(f"mcp-{stamp}-{os.getpid()}")
    environ["SCOUTER_JOB"] = job
    return job
```

In `main()`, after `logging.basicConfig(...)`:

```python
    job = _pin_session_job(os.environ)
    logger.info("scouter-mcp session job: %s", job)
```

`scouter/core/wb.py`, `load_table`, at the top of the `elif isinstance(data, dict) and isinstance(data.get("samples"), list) ...` branch:

```python
        version = data.get("version", 1)
        if version != 1:
            raise ValueError(
                f"{path}: calibration table version {version} is not supported "
                "(this Scouter reads version 1); re-run `scouter wb calibrate`"
            )
```

`tests/host_support.py`: **move** (do not copy) `_PluginInstall` (renamed `PluginInstall`), `_quit` (renamed `quit_darktable`) and the path constants they need (`DARKTABLE`, `SCRATCH_DIR`, `HERO`, `INSTALLED_PLUGIN`, `PLUGIN_SRC`) from `tests/test_host_phase2.py`. Keep their bodies byte-identical. Then add:

```python
# A METHOD on PluginInstall (indent it into the class). Build the luarc path with
# the same expression install() uses for `self.luarc_path`, so the two cannot drift.
    def touched_paths(self) -> list[Path]:
        """Everything install()/restore() may write: the plugin file and luarc. Nothing else (P3-R18)."""
        return [INSTALLED_PLUGIN, INSTALLED_PLUGIN.parent.parent / "luarc"]


def launch(image: Path) -> subprocess.Popen:
    """Start darktable on `image`, so it comes up in the darkroom (phase 2 host fact)."""
    return subprocess.Popen([str(DARKTABLE), str(image)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_ready(budget_s: float = 120.0) -> str:
    """Poll `view.current` until the bridge answers; return the view. Raises on budget."""
    from scouter.core import dtbridge

    deadline = time.monotonic() + budget_s
    last: object = "never answered"
    while time.monotonic() < deadline:
        try:
            return dtbridge.current_view()
        except Exception as e:  # noqa: BLE001 -- any failure means "not yet"
            last = e
            time.sleep(0.5)
    raise TimeoutError(f"darktable bridge not ready after {budget_s}s: {last}")
```

In `tests/test_host_phase2.py`, replace the moved definitions with `from tests.host_support import PluginInstall as _PluginInstall, quit_darktable as _quit, DARKTABLE, SCRATCH_DIR, HERO, INSTALLED_PLUGIN, PLUGIN_SRC` (keep the old names so the rest of the file does not change). `host_support.py` must not import `pytest` or trigger any darktable side effect at import time.

`scouter/__init__.py`: `__version__ = "0.3.0"`, `PHASE = 3`. Fix any test that pins `PHASE == 2` or `"0.2.0"` (for example `grep -rn '"0.2.0"\|PHASE' tests/`). Only update assertions whose purpose is the current version.

`README.md`:
1. In §"Honest limits" (phase 1), replace the `style extract --modules` bullet with: "**`style extract --no-filter` keeps the whole history.** Since phase 2 the default `--modules` path filters for real (`modules_honoured: true`); only `--no-filter` captures every active operation (minus `never_include`), and says so."
2. In the `darkroom status` bullet, add: "Use `darkroom get` (phase 3) for a parameter's value."
3. Add a section `## Scouter phase 3 (conversation: say it, see it, save it)`, placed after the phase 2 exit criteria. It has five parts. (a) A four-line hero session:

```sh
export SCOUTER_JOB=teach-harbour
scouter --pretty darkroom say "a touch warmer"
scouter --pretty darkroom say "less contrast" --educative full
scouter darkroom undo-last
scouter preset save harbour-warm && scouter preset apply-job harbour-warm --job-slug harbour-wedding --yes
```

(b) The educative config key and levels. (c) The two new exit codes (`20 value_unknown`, `21 not_pinned`). (d) "`scouter vocab list` shows which words work today". (e) §"Honest limits, phase 3", copied from spec §12 verbatim. Append `· 20 value_unknown · 21 not_pinned` to the exit-code line.

- [ ] **Step 4: Run to verify pass**

Run: `uv run --extra dev python -m pytest tests/test_phase3_closures.py -q && uv run --extra dev python -m pytest tests -q`
Expected: `6 passed`, then the full suite `0 failed`. The host classes still skip without `SCOUTER_HOST_TESTS=1`.

- [ ] **Step 5: Commit**

```bash
git add scouter/mcp/server.py scouter/core/wb.py tests/host_support.py tests/test_host_phase2.py scouter/__init__.py README.md tests/test_phase3_closures.py
git commit -m "phase 3 task 9: MCP session job, wb table version check, host restore scope (R22), README phase 3"
```

**Validation contract (done when):**
- `uv run --extra dev python -m pytest tests/test_phase3_closures.py -q` → `6 passed`.
- `uv run --extra dev python -m pytest tests/test_host_phase2.py -q` → `skipped` only, no `error` (the imports resolve).
- `grep -n "copytree" tests/test_host_*.py tests/host_support.py` → no output.
- `grep -c "Honest limits, phase 3" README.md` → `1`; `grep -c "21 not_pinned\|\`21\` not_pinned" README.md` → at least `1`.
- `uv run --extra dev python -c "import scouter; print(scouter.PHASE, scouter.__version__)"` → `3 0.3.0`.

---

### Task 10: Host run — probe candidate paths, pin slots, check signs, preset round trip, timing

> **STOP before step 3: `WAITING FOR GO`.** This task launches the real darktable on Pratik's Mac. The controller confirms that Pratik has closed darktable before any host command runs. Tasks 1-9 are merge-ready without this task (P3-R17). If GO never comes, the phase merges with the current pins, and the rows stay `candidate`/`absolute_only` with honest refusals.

**Files:**
- Create: `tests/test_host_phase3.py`
- Modify (after the run, by hand, from the printed results): `scouter/core/dtbridge.py` (`ACTION_PATHS`), `scouter/core/xmp.py` (`LAYOUTS` named slots / new layouts), `tests/fixtures/<module>-v<n>.xmp` (new captures, by hand), `scouter/core/phrases.py` (only if a sign check fails), `tests/fixtures/vocab_golden.json`, `tests/fixtures/phrases_golden.json`, `tests/fixtures/explain_golden.json` (regenerated, reviewed), `docs/measurements.md`

**Interfaces:**
- Consumes: `tests.host_support.{PluginInstall, launch, wait_ready, quit_darktable, SCRATCH_DIR, HERO}`, `dtbridge.call`, `vocab.PARAMS`, `vocab.status`, `registry.call`, `scout.stats`, `style.delete` bridge verb (phase 2 R14: `scouter-` prefix + `confirm`).
- Produces: pinned `ACTION_PATHS` entries and `LAYOUTS` slots, with every pin citing the run in a comment. `docs/measurements.md` gains the section "Phase 3 host run (2026-09-24+)".

- [ ] **Step 1: Write the host test file (it skips without the env var)**

```python
# tests/test_host_phase3.py
"""Phase 3 task 10: the host probe (design §10). Gated: SCOUTER_HOST_TESTS=1 + darktable.

Never snapshots ~/.config/darktable (P3-R18): PluginInstall touches the plugin
file and luarc only. Every class quits darktable in tearDown/finally.
"""

from __future__ import annotations

import os
import shutil
import statistics
import time
import unittest
from pathlib import Path

from tests import host_support as hs

RUN = os.environ.get("SCOUTER_HOST_TESTS") == "1" and hs.DARKTABLE.exists()
REASON = "host-only: set SCOUTER_HOST_TESTS=1 with darktable installed"
SCRATCH = hs.SCRATCH_DIR / "phase3-probe.ORF"
RESULTS = Path(__file__).resolve().parent.parent / "docs" / "measurements-generated.md"


def _record(line: str) -> None:
    print(line)
    with RESULTS.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


@unittest.skipUnless(RUN, REASON)
class ProbeCandidatePathsHostTest(unittest.TestCase):
    """§10.1: ONE darktable launch per candidate path (an unknown path wedges the worker)."""

    def test_probe_every_candidate(self) -> None:
        from scouter.core import dtbridge, vocab

        hs.SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
        if not SCRATCH.exists():
            shutil.copy(hs.HERO, SCRATCH)
        install = hs.PluginInstall()
        install.install()
        _record("\n## Phase 3 candidate-path probe\n\n| module.param | path | verdict | ms |\n|---|---|---|---|")
        try:
            for s in vocab.PARAMS:
                if vocab.status(s) != "candidate":
                    continue
                for path in s.candidate_paths:
                    proc = hs.launch(SCRATCH)
                    verdict, ms = "error", 0
                    try:
                        hs.wait_ready()
                        t0 = time.monotonic()
                        try:
                            r = dtbridge.call("darkroom.status", {"path": path, "instance": 0},
                                              timeout=10.0)
                            verdict = "absent" if r.get("nan") else "pinned"
                        except Exception:  # noqa: BLE001
                            verdict = "wedges"
                        ms = int((time.monotonic() - t0) * 1000)
                        if verdict == "pinned":
                            try:
                                dtbridge.call("view.current", {}, timeout=5.0)
                            except Exception:  # noqa: BLE001
                                verdict = "wedges"
                    finally:
                        hs.quit_darktable(proc)
                    _record(f"| {s.module}.{s.param} | `{path}` | {verdict} | {ms} |")
                    if verdict == "pinned":
                        break
        finally:
            install.restore()


@unittest.skipUnless(RUN, REASON)
class Phase3LiveHostTest(unittest.TestCase):
    """§10.2 slots, §10.3 signs, preset round trip and timing, in ONE launch."""

    @classmethod
    def setUpClass(cls) -> None:
        hs.SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
        if not SCRATCH.exists():
            shutil.copy(hs.HERO, SCRATCH)
        cls.install = hs.PluginInstall()
        cls.install.install()
        cls.proc = hs.launch(SCRATCH)
        try:
            hs.wait_ready()
        except BaseException:
            hs.quit_darktable(cls.proc)
            cls.install.restore()
            raise
        os.environ["SCOUTER_JOB"] = f"phase3-host-{os.getpid()}"

    @classmethod
    def tearDownClass(cls) -> None:
        from scouter import registry
        try:
            registry.call("history.commit", "cli", image=str(SCRATCH))
        finally:
            hs.quit_darktable(cls.proc)
            cls.install.restore()

    def test_1_slot_pinning_by_sentinel(self) -> None:
        from scouter import registry
        from scouter.core import history, vocab

        for s in vocab.PARAMS:
            if vocab.status(s) != "absolute_only" or s.value_source != "xmp":
                continue
            sentinel = 37.0 if s.wraps else 0.173
            registry.call("darkroom.enable", "cli", module=s.module)
            r = registry.call("darkroom.set", "cli", module=s.module, param=s.param, value=sentinel)
            entry = max((e for e in history.read(image=str(SCRATCH)).entries
                         if e.operation == s.module), key=lambda e: e.num)
            floats = (entry.decoded or {}).get("floats", [])
            slots = [i for i, v in enumerate(floats) if abs(v - sentinel) < 1e-4]
            _record(f"slot {s.module}.{s.param} v{entry.modversion}: set ok={r.ok} "
                    f"slots={slots} of {len(floats)} (size {len(entry.params_hex) // 2})")

    def test_2_signs(self) -> None:
        from scouter import registry

        def stats() -> dict:
            return registry.call("scout.stats", "cli", image=str(SCRATCH), px=512).data

        base = stats()
        registry.call("darkroom.adjust", "cli", module="exposure", param="black", delta=0.01)
        deeper = stats()
        _record(f"sign exposure.black +0.01: luminance_mean {base.get('luminance_mean')} -> "
                f"{deeper.get('luminance_mean')} (expect down)")
        registry.call("darkroom.undo_last", "cli")

    def test_3_preset_round_trip(self) -> None:
        from scouter import registry
        from scouter.core import dtbridge

        name = f"scouter-probe-{os.getpid()}"
        self.assertTrue(registry.call("darkroom.say", "cli", phrase="warmer").ok)
        saved = registry.call("preset.save", "cli", name=name)
        self.assertTrue(saved.ok, saved.data)
        listed = registry.call("preset.list", "cli")
        self.assertIn(name, [p["name"] for p in listed.data["presets"]])
        registry.call("darkroom.say", "cli", phrase="cooler")
        applied = registry.call("preset.apply", "cli", name=name)
        _record(f"preset.apply open image: ok={applied.ok} data={applied.data}")
        self.assertTrue(applied.ok)
        self.assertGreater(applied.data["history_end_after"], applied.data["history_end_before"])
        dtbridge.call("style.delete", {"name": name, "prefix": "scouter-", "confirm": True})

    def test_4_conversation_timing(self) -> None:
        from scouter import registry

        times = []
        for phrase in ("brighter", "darker", "brighter", "darker", "less contrast"):
            t0 = time.monotonic()
            r = registry.call("darkroom.say", "cli", phrase=phrase)
            times.append(time.monotonic() - t0)
            self.assertTrue(r.ok, r.data)
        _record(f"darkroom.say wall times (s): {[round(t, 2) for t in times]}, "
                f"median {statistics.median(times):.2f}")
        self.assertLess(statistics.median(times), 3.5)
```

Adapt `registry.call` argument names to the real verb signatures if one differs. `history.commit`'s image argument is `image`, and `scout.stats` takes `image` and `px`. Do not change the verbs to fit the test.

- [ ] **Step 2: Verify it skips without the env var**

Run: `uv run --extra dev python -m pytest tests/test_host_phase3.py -q -rs`
Expected: `2 skipped`, with the reason `host-only: set SCOUTER_HOST_TESTS=1 with darktable installed`.
Commit the file: `git add tests/test_host_phase3.py && git commit -m "phase 3 task 10: host probe file (skips without SCOUTER_HOST_TESTS)"`.

- [ ] **Step 3: WAITING FOR GO, then the run**

After the controller's GO (and only then; Pratik has closed darktable):

```bash
pgrep -x darktable && echo "ABORT: darktable is running" || true
SCOUTER_HOST_TESTS=1 uv run --extra dev python -m pytest tests/test_host_phase3.py::ProbeCandidatePathsHostTest -q -s
SCOUTER_HOST_TESTS=1 uv run --extra dev python -m pytest tests/test_host_phase3.py::Phase3LiveHostTest -q -s
pgrep -x darktable && echo "LEAK: darktable still running" || echo "darktable gone"
```

Expected: the last line is `darktable gone`. The candidate table and the slot, sign, preset and timing lines are in `docs/measurements-generated.md`.

- [ ] **Step 4: Pin from the results, by hand**

1. For each `pinned` row, add `("<module>", "<param>"): "<path>",` to `dtbridge.ACTION_PATHS`, under a comment `# phase 3 task 10 host probe, <date>: answered non-nan, worker alive after`. Never add a row whose verdict is `absent`, `wedges` or `error`.
2. For each slot line with exactly one slot, add `i: "<param>"` to `named_slots` of `xmp.LAYOUTS[(module, v)]` (create the layout with `size` and `float_count` from the line if the `(module, v)` is new). Capture the sidecar to `tests/fixtures/<module>-v<v>.xmp` by hand, and add a unit test in `tests/test_xmp.py` asserting the named value decodes from that fixture.
3. The phase 3 unit tests that assert **initial** statuses (`tests/test_vocab.py::test_initial_statuses_match_the_spec_table`, `tests/test_moves.py::test_candidate_refuses_not_pinned_before_any_bridge_call`, `tests/test_say.py::test_a_phrase_with_any_unreachable_move_is_refused_whole`, `test_absolute_only_*`) will fail for rows that got pinned. Update each one to assert the **new** status, and where a test needs an unpinned row, switch it to a row still unpinned (keep at least one `candidate` example; `colorzones.curves` is always `unreachable`).
4. If a sign line contradicts its expectation, flip the notches of the affected phrases in `phrases.py` and say so in the commit.
5. Regenerate the three phase 3 goldens with the commands printed by their tests, and review the diffs: only status/pinned_path changes, plus any sign flips.
6. Add a "Phase 3 host run" section to `docs/measurements.md` by hand: the candidate table, pinned slots, signs, preset round trip, and timing median against the 3.5 s target.

- [ ] **Step 5: Run and commit**

Run: `uv run --extra dev python -m pytest tests -q && lua tests/lua/test_dispatcher.lua`
Expected: `0 failed`, Lua exit 0.

```bash
git add scouter/core/dtbridge.py scouter/core/xmp.py scouter/core/phrases.py tests/ docs/measurements.md
git commit -m "phase 3 task 10: host probe pins (<n> paths, <m> slots), signs, preset round trip, timing"
```

**Validation contract (done when):**
- `uv run --extra dev python -m pytest tests/test_host_phase3.py -q -rs` (without the env var) → `2 skipped`.
- After the run: `pgrep -x darktable || echo gone` → `gone`.
- `git diff main -- scouter/core/dtbridge.py | grep '^+' | grep -c 'iop/'` equals the number of `pinned` rows in the probe table in `docs/measurements.md`.
- `uv run --extra dev python -m scouter vocab list | python3 -c "import json,sys; print(json.load(sys.stdin)['data']['counts'])"` → `live` ≥ 5, and every newly `live` row appears in the measurements table.
- `uv run --extra dev python -m pytest tests -q` → no `failed`.
- `ls ~/.config/darktable/data.db` has an mtime unchanged by the run's restore step (only the plugin and luarc are restored; P3-R18).

---

## Self-review

**1. Spec coverage.**

| Spec section | Task |
|---|---|
| §1 C1 vocabulary table + golden | 2 |
| §1 C2 phrases + golden | 3 |
| §1 C3 relative moves, value_unknown / not_pinned | 1 (errors), 5 |
| §1 C4 say + group undo | 1 (journal group), 6 |
| §1 C5 educative off/brief/full, session set | 1 (config key), 4 (layer), 5/6/7/8 (attached) |
| §1 C6 presets save/list/apply/apply_job, Lua | 7, 8 |
| §1 C7 parity + MCP golden | 2-8 (contract files each) |
| §1 C8 one event per verb | 2-8 (`@metrics.instrumented`, `SMOKE_ARGS`) |
| §1 C9 host probe, slots, signs, preset, timing | 10 |
| §4.2 status derivation | 2 |
| §5.4 @tone routing | 5 (`route_tone`), 6 (used) |
| §6.1 darkroom.get never commits | 5 (`test_get_reads_the_value_and_never_commits`) |
| §6.2 pre-commit default, trust_sidecar, clamp no-op | 5 |
| §6.4 undo by group, `single` | 6 |
| §7 educative config default brief, override, broken config → brief | 1, 4 |
| §8.1 session modules, memory dir, index, never overwrite | 7 |
| §8.3 bounce apply, history_end verification | 8 |
| §8.4 undo refusal for preset.apply | 8 |
| §9 Lua `darkroom.apply_style`, timeout 30 s | 8 |
| §10.4 / P3-R18 restore scope | 9 (helper + test), 10 (used) |
| §11 closures: MCP session job, wb version, README stale bullet, darkroom get | 9 (+5 for status warning text) |
| §12 honest limits in README | 9 |
| §15 exit codes 20/21 | 1 |
| §16 PHASE 3, payload counters | 9 (PHASE), 5/6/7/8 (fields) |

No gaps.

**2. Placeholder scan.** No "TBD", "TODO" or "similar to task N". Every code step carries code. Two steps defer to existing code by name, and both say exactly what to reuse. Task 8's Lua harness helpers are named, with "use the harness's existing equivalents" and a "do not add a second stubbing mechanism" guard. Task 10 step 4 is a hand-pinning procedure driven by the run's output, which cannot be pre-written. It lists exact file edits and their conditions.

**3. Type consistency.** `ParamSpec` fields (task 2) are used by name in tasks 3-6. `Move(module, param, kind, notches, value)` (task 3) is consumed in task 6's `say`. `MoveNote(spec, previous, target, kind)` (task 4) is consumed in tasks 5-6. `explain.with_unit` is defined in task 4 and used in tasks 5-6. `_adjust_core(module, param, delta, *, resolved, instance, commit, level, job)` is defined in task 5 and called with those keywords in task 6. `journal.GROUP` (task 1) is used in task 6. `undo_last(*, single, dry_run, job)` (task 6) is used in tasks 7 (`single=True`) and 8. `PresetResult`/`PresetListResult` field names (task 1) match tasks 7-8. `presets._history_end(path)` is defined and patched in task 8. `host_support.PluginInstall.touched_paths` is defined in task 9 and tested there. The verb count: 35 + `vocab.list`, `vocab.resolve`, `darkroom.get`, `darkroom.adjust`, `darkroom.say`, `preset.save`, `preset.list`, `preset.apply`, `preset.apply_job` = 44 (task 8 validation).

**4. Review Focus.** Five lines, each with a named test in its owning task: negative `by N` (task 3), clamp-limit no-op (task 5), hue wrap (task 5), MCP session job (task 9), preset modules after undo (task 7).
