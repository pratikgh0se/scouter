# Scouter Phase 3: Conversation (vocabulary, relative moves, named presets, educative mode)

**Date:** 2026-09-24
**Phase:** 3. Scope set by decision row 390 (Pratik, 2026-09-24 15:45), which also carries row 346 (educative mode).
**Phase 2 design:** `docs/superpowers/specs/2026-09-14-scouter-phase2-design.md` (built, merged to `main` at `33ebaef`, tree = `6a720e9`, 1164 passed / 18 skipped on 2026-09-24).
**Phase 2 ledger:** `.superpowers/sdd/2026-09-14-scouter-phase2/progress.md`. Rulings R1-R22 are binding where they touch the same code. Phase 1 rulings 1-35 still hold (restated in phase 2 §3.1).
**Learning note:** `~/second-brain/Knowledge/Scouter - What I need to learn to edit with it 2026-09-15.md` (moved there from `Inbox/` during the 2026-09-21 triage). It describes the workflow this phase makes real: open one picture live, edit by conversation, undo when wrong, "save this as <name>", reuse it as a filter.
**Plan:** `docs/superpowers/plans/2026-09-24-scouter-phase3.md`

---

## 1. Goal and exit criteria

Phase 1 made one frame editable by absolute values. Phase 2 made a job deliverable. Phase 3 lets Pratik edit **one picture by conversation**: "warmer", "lift the shadows", "less contrast", "stronger colour grade", watching darktable change as each move lands. He can undo a move, save the result as a named preset, and apply that preset to another image or a whole job. Scouter explains each move at the level he chooses.

Every piece is a deterministic core function. The phrase table, the step sizes, the clamps and the explanation text are fixed data, pinned by golden files. No model runs inside Scouter. Claude, the MCP client, picks the phrase or the numbers. Scouter maps them to darktable the same way every time.

| # | Criterion | How it is proven |
|---|---|---|
| C1 | Every vocabulary row names a module, a parameter, a unit, a clamp range, a conversational step, a value source and a derived status (`live`, `absolute_only`, `candidate`, `unreachable`). The table is pinned. | `tests/test_vocab.py` + golden `tests/fixtures/vocab_golden.json` |
| C2 | Every phrase in the phrase table resolves to the same moves every time. An unknown phrase is a `usage` refusal that suggests the nearest known phrases. | `tests/test_phrases.py` + golden `tests/fixtures/phrases_golden.json` |
| C3 | `darkroom.adjust` reads the current value, adds the delta, clamps, sets it, and reports `previous`, `target`, `applied`, `clamped`, `source` and a one-line `summary`. It refuses a param whose value it cannot read (`value_unknown`, exit 20) and a param with no pinned action path (`not_pinned`, exit 21). | `tests/test_moves.py` (FakeBridge that counts calls + a temp copy of the fixture XMP) |
| C4 | `darkroom.say "<phrase>"` runs every move of the phrase as one group. `darkroom.undo_last` reverts the whole group by default. | `tests/test_say.py` |
| C5 | `educative: off \| brief \| full` (config key, default `brief`, per-call override). `off` adds no lines. `brief` adds one sentence per move. `full` adds a concept paragraph the first time that concept comes up in the session, and never again in that session. | `tests/test_explain.py` + golden `tests/fixtures/explain_golden.json` |
| C6 | `preset.save NAME` saves the modules edited in this session on the darkroom image as a `.dtstyle` and imports it into darktable. `preset.list` shows the presets. `preset.apply NAME` applies one to the open image (through a view bounce) or to a named image. `preset.apply_job` applies one to a whole job. | `tests/test_presets.py`, `tests/test_presets_apply.py`, `tests/lua/test_dispatcher.lua` |
| C7 | Three layers, one vocabulary: every new verb is in the registry, the CLI and the MCP tool table, with the same argument names. MCP schemas are pinned. | existing `tests/test_layer_parity.py`, `tests/test_scouter_mcp_schemas.py` (golden updated by each task that adds a verb) |
| C8 | Every new verb emits exactly one metrics event. | existing `tests/test_metrics.py::test_every_registered_verb_emits_one_event` |
| C9 | On the real darktable: candidate action paths are probed one per launch, and each one that answers is pinned. Unknown XMP slots are pinned by the sentinel method. Sign conventions are checked against rendered stats. A preset round-trips through the bounce. A conversational `darkroom.say` finishes in under 3.5 s. | `tests/test_host_phase3.py` (gated `SCOUTER_HOST_TESTS=1`), results in `docs/measurements.md` |

Budget targets, measured in task 10 (C9) and recorded by hand in `docs/measurements.md`:

| Operation | Target | Why |
|---|---|---|
| `darkroom.adjust`, sidecar untrusted (default) | < 3.5 s | one pre-commit bounce with `expect_change=False` (~1.0 s) plus one real set and commit (~1.8 s, phase 2) |
| `darkroom.adjust --trust-sidecar` | < 2.2 s | the set and commit only |
| `darkroom.say` with 3 moves | < 7 s | one pre-commit, then three sets at `trust_sidecar=True` |
| `preset.apply` to the open image | < 3 s | one bounce, with the apply inside it |

---

## 2. Non-goals

- **Sony A7 / NEX-5N white-balance tables** (row 390: excluded until Pratik's files exist). For a camera with no table, relative white balance refuses with `value_unknown` and names `scouter wb calibrate`.
- **E12 findings** (the 300-frame real job). That is still Pratik's own run.
- **AI masks, drawn masks, local adjustments.** Every move is a global module parameter. `blendop_params` are never decoded or written.
- **Tone equalizer, color calibration, lens correction, denoise.** They are not in the vocabulary. `darkroom.set` still reaches any pinned pair.
- **Color zones curve editing.** Color zones stores its zones as curves, and no pinned slider sets a zone value. The table carries the module as `unreachable` for zones (§4.3), with one global slider candidate.
- **Style memory / Dragon Radar (spec 4.5).** Presets are stored under `<library_root>/scouter/memory/presets/` because phase 2 reserved that directory. Phase 3 learns nothing from them: no acceptance signal, no `looks.json`.
- **Darkroom navigation (ruling R20).** Scouter still edits the image darktable has loaded. Opening a frame imported in the same session remains an honest refusal. This is not cheap (§12).
- **Undoing a preset application.** darktable's Lua API cannot truncate history, and no global-undo action path is pinned. `undo_last` on a preset application refuses and says how to do it in the GUI (§8.4).
- **Position-to-value inversion.** Values come from the XMP (and, for Kelvin, from the measured table). They never come from a slider's normalised position. The phase 1 honest limit stands.

---

## 3. Architecture

```
Pratik ──chat──▶ Claude ──MCP──▶ scouter.mcp.server (session job pinned at start: mcp-<ts>-<pid>)
shell ─────────────────────────▶ scouter.cli.main
                                       │
                                       ▼
                           scouter.registry.call(verb, **args)          (35 → 44 verbs)
                                       │
   ┌───────────────┬───────────────────┼─────────────────────────┬──────────────────────┐
   ▼               ▼                   ▼                         ▼                      ▼
core.vocab      core.phrases        core.moves                 core.explain          core.presets
(ParamSpec      (phrase → Move      darkroom.get / adjust / say (educative lines,     preset.save/list/
 table, status   list, pure)          │                          session "explained"   apply/apply_job
 from pins)                           │                          set in job state)       │
                                      ├─▶ core.darkroom.set_param.__wrapped__ (unchanged engine)
                                      ├─▶ core.darkroom._commit (pre-commit, expect_change=False)
                                      ├─▶ core.xmp (value read) / core.wb.kelvin (Kelvin read)
                                      └─▶ core.journal (group contextvar)            ├─▶ core.style.extract.__wrapped__
                                                                                     ├─▶ core.style.apply.__wrapped__
                                                                                     ├─▶ core.style.apply_set.__wrapped__
                                                                                     └─▶ bridge darkroom.apply_style (NEW Lua)
```

New modules: `scouter/core/vocab.py`, `scouter/core/phrases.py`, `scouter/core/moves.py`, `scouter/core/explain.py`, `scouter/core/presets.py`, `tests/host_support.py`. Extended: `scouter/config.py` (`educative`), `scouter/errors.py` (two classes), `scouter/types.py` (six results), `scouter/core/journal.py` (`group`), `scouter/core/darkroom.py` (`undo_last` group loop, preset refusal), `scouter/core/wb.py` (table version check), `scouter/mcp/server.py` (session job), `scouter/mcp/schemas.py`, `scouter/registry.py` (`_CORE_MODULES`), `darktable_mcp/lua/darktable_mcp.lua` (`darkroom.apply_style`), `darktable_mcp/bridge/client.py` (one timeout). After the host probe: `scouter/core/dtbridge.py` (`ACTION_PATHS`) and `scouter/core/xmp.py` (`LAYOUTS` named slots).

**The engine does not change.** `darkroom.set_param`, the commit path, the no-op shortcut, the journal's before/after semantics and the undo tiers stay as they are. Relative moves read a value and then call `set_param.__wrapped__`. Presets call the phase 2 style functions through `__wrapped__`. The educative layer only adds strings to results.

### 3.1 Conventions carried forward (binding)

1. Three layers over one core. Same verb id, argument names and result shape in Python, CLI and MCP.
2. One metrics event per top-level verb. Nested verbs are called through `__wrapped__` (ruling 23).
3. Dry runs make **no** darktable calls that write, open or move the view. A dry run may resolve a phrase and look up the vocabulary. It never reads the darkroom.
4. Ruling 30 stands. A darkroom write refuses when `darkroom.current` differs from the image this job opened.
5. stdlib + `mcp>=2,<3` only.
6. No `library.db` read or write.
7. Host tests are gated by `SCOUTER_HOST_TESTS=1` and the darktable binary. They commit before quitting and always quit darktable.
8. Honest limits are surfaced in the result, never hidden. That means no guessed value, no phantom success and no invented capability.
9. Never overwrite blind. That covers files, presets, fixtures and vault notes.
10. **The whitelist rule (phase 2 carried finding 1).** `dtbridge.ACTION_PATHS` is extended only with pairs a host probe answered non-`nan` for. Production code never passes `probe_unpinned=True`. A vocabulary row whose path is not pinned is data, not a capability.

---

## 4. The vocabulary (`scouter/core/vocab.py`)

### 4.1 The row

```python
@dataclass(frozen=True)
class ParamSpec:
    module: str                 # darktable operation name, e.g. "colorbalancergb"
    param: str                  # Scouter's param key == the XMP field / named-slot name
    ui: str                     # what darktable's GUI calls it, e.g. "color balance rgb › 4 ways › shadows lift › luminance"
    unit: str                   # "EV" | "K" | "ratio" | "%" | "°"
    display_scale: float        # shown value = internal value * display_scale (100 for "%", else 1)
    lo: float                   # Scouter's clamp, internal units
    hi: float
    step: float                 # one conversational notch, internal units
    value_source: str           # "xmp" | "wb_table" | "none"
    neutral: float | None       # value to assume when the module has no active history entry; only where 0 is neutral by construction
    wraps: bool                 # hue: wrap modulo 360 instead of clamping
    concept: str                # key into explain.CONCEPTS
    why: str                    # the reason clause of the brief explanation
    watch: str                  # what to look at after this move
    candidate_paths: tuple[str, ...] = ()   # probe order; only used while the pair is unpinned
    reachable: bool = True      # False: darktable has no slider for this at all
    note: str = ""
```

**Units are internal.** `darkroom.set`, `darkroom.adjust` and the table all use darktable's internal parameter units, which is what `dt.gui.action(..., "set", v)` takes (phase 0 finding 1). The phase 2 colorbalancergb probe confirms it: it set `0.2` and the XMP slot held `0.2`, while the GUI shows `20 %`. The `%` rows therefore carry `display_scale = 100`. `summary` and the educative lines show display units. Arguments and fields stay internal. The MCP descriptions say this in one sentence ("fractions: 0.05 means 5 %").

### 4.2 Status is derived, never stored

```python
def status(s: ParamSpec) -> str:
    if not s.reachable:                                   return "unreachable"
    if (s.module, s.param) not in dtbridge.ACTION_PATHS:  return "candidate"
    if s.value_source == "wb_table":                      return "live"
    if s.value_source == "xmp" and xmp_named(s.module, s.param): return "live"
    return "absolute_only"
```

`xmp_named(module, param)` is true when any `xmp.LAYOUTS[(module, v)]` names `param` in its `fields` or `named_slots`. Status follows the pins automatically. When task 10 pins a path or a slot, the row changes status, and the golden file changes in that same commit, which makes the upgrade visible.

| Status | `darkroom.set` | `darkroom.adjust` / `say` |
|---|---|---|
| `live` | works | works |
| `absolute_only` | works | refuses `value_unknown` (exit 20). The fix names the absolute form, e.g. `scouter darkroom set sigmoid skew 0.2` |
| `candidate` | refuses `usage` (the existing `action_path` refusal) | refuses `not_pinned` (exit 21). The fix names the nearest `live` row with the same concept, if there is one |
| `unreachable` | refuses `usage` | refuses `not_pinned`, and the fix states the reason in `note` |

### 4.3 The table (initial statuses as of `main` 33ebaef)

Ranges are Scouter's **conservative clamps**. They are not darktable's hard limits. darktable's soft and hard ranges are wider, and a clamp exists so that "much warmer" said five times cannot run away. `default` is darktable 5.4's module default where it is neutral by construction, and blank where it is not.

| module.param | UI name | unit (display ×) | clamp (internal) | step | value source | neutral | status now | candidate paths (probe order) |
|---|---|---|---|---|---|---|---|---|
| exposure.exposure | exposure › exposure | EV | −3.0 … 4.0 | 0.3 | xmp | — | **live** | pinned |
| exposure.black | exposure › black level correction | ratio | −0.1 … 0.1 | 0.005 | xmp | — | **live** | pinned |
| sigmoid.contrast | sigmoid › contrast | ratio | 0.5 … 3.0 | 0.1 | xmp | — | **live** | pinned |
| sigmoid.skew | sigmoid › skew | ratio | −1.0 … 1.0 | 0.1 | xmp (slot unpinned) | — | absolute_only | pinned (slot pin in task 10) |
| temperature.temperature | white balance › temperature | K | 2000 … 12000 (and inside the camera's table range) | 300 | wb_table | — | **live** (for a camera with a table) | pinned |
| temperature.tint | white balance › tint | ratio | 0.5 … 2.0 | 0.05 | none | — | absolute_only | pinned |
| colorbalancergb.contrast | color balance rgb › master › contrast | % (100) | −0.5 … 0.5 | 0.05 | xmp | 0.0 | **live** | pinned |
| colorbalancergb.global_saturation | color balance rgb › master › global saturation | % (100) | −0.5 … 0.5 | 0.05 | xmp (slot unpinned) | 0.0 | absolute_only | pinned (slot pin in task 10) |
| colorbalancergb.global_vibrance | color balance rgb › master › global vibrance | % (100) | −0.5 … 0.5 | 0.05 | xmp | 0.0 | candidate | `iop/colorbalancergb/global vibrance` |
| colorbalancergb.global_chroma | color balance rgb › master › global chroma | % (100) | −0.5 … 0.5 | 0.05 | xmp | 0.0 | candidate | `iop/colorbalancergb/global chroma` |
| colorbalancergb.shadows_luminance | color balance rgb › 4 ways › shadows lift › luminance | % (100) | −0.25 … 0.25 | 0.02 | xmp | 0.0 | candidate | `iop/colorbalancergb/shadows lift/luminance`, `iop/colorbalancergb/shadows luminance` |
| colorbalancergb.shadows_hue | … › shadows lift › hue | ° | 0 … 360 (wraps) | 10 | xmp | — | candidate | `iop/colorbalancergb/shadows lift/hue`, `iop/colorbalancergb/shadows hue` |
| colorbalancergb.shadows_chroma | … › shadows lift › chroma | % (100) | 0.0 … 0.3 | 0.02 | xmp | 0.0 | candidate | `iop/colorbalancergb/shadows lift/chroma`, `iop/colorbalancergb/shadows chroma` |
| colorbalancergb.midtones_luminance | … › power › luminance | % (100) | −0.25 … 0.25 | 0.02 | xmp | 0.0 | candidate | `iop/colorbalancergb/power/luminance`, `iop/colorbalancergb/midtones luminance` |
| colorbalancergb.midtones_hue | … › power › hue | ° | 0 … 360 (wraps) | 10 | xmp | — | candidate | `iop/colorbalancergb/power/hue`, `iop/colorbalancergb/midtones hue` |
| colorbalancergb.midtones_chroma | … › power › chroma | % (100) | 0.0 … 0.3 | 0.02 | xmp | 0.0 | candidate | `iop/colorbalancergb/power/chroma`, `iop/colorbalancergb/midtones chroma` |
| colorbalancergb.highlights_luminance | … › highlights gain › luminance | % (100) | −0.25 … 0.25 | 0.02 | xmp | 0.0 | candidate | `iop/colorbalancergb/highlights gain/luminance`, `iop/colorbalancergb/highlights luminance` |
| colorbalancergb.highlights_hue | … › highlights gain › hue | ° | 0 … 360 (wraps) | 10 | xmp | — | candidate | `iop/colorbalancergb/highlights gain/hue`, `iop/colorbalancergb/highlights hue` |
| colorbalancergb.highlights_chroma | … › highlights gain › chroma | % (100) | 0.0 … 0.3 | 0.02 | xmp | 0.0 | candidate | `iop/colorbalancergb/highlights gain/chroma`, `iop/colorbalancergb/highlights chroma` |
| filmicrgb.white_relative_exposure | filmic rgb › scene › white relative exposure | EV | 2.0 … 10.0 | 0.25 | xmp | — | candidate | `iop/filmicrgb/white relative exposure` |
| filmicrgb.black_relative_exposure | filmic rgb › scene › black relative exposure | EV | −14.0 … −3.0 | 0.25 | xmp | — | candidate | `iop/filmicrgb/black relative exposure` |
| filmicrgb.contrast | filmic rgb › look › contrast | ratio | 0.5 … 3.0 | 0.1 | xmp | — | candidate | `iop/filmicrgb/contrast` |
| colorzones.strength | color zones › mix | % (100) | 0.0 … 2.0 | 0.1 | xmp | — | candidate | `iop/colorzones/mix` |
| colorzones.curves | color zones › (zone curves) | — | — | — | none | — | unreachable | none: zones are curve nodes, not sliders |

Hue values are colour-wheel angles in degrees, as the GUI shows them. The candidate spellings come from the GUI labels. They are **guesses by construction**, which is why they are candidates. An `iop/<module>/<section>/<label>` path is tried before the flat `<section> <label>` form. That order follows how the pinned `iop/exposure/black level correction` and `iop/colorbalancergb/global saturation` are spelled. Task 10's probe decides, one launch per candidate (§10.1).

**Filmic, "if present".** darktable 5.4's default scene-referred tone mapper is sigmoid. Pratik's fixture uses sigmoid. The filmic rows exist so that "less contrast" reaches whichever tone mapper the image actually uses (§5.4). Until a filmic path is pinned, an image whose active tone mapper is filmic gets `not_pinned` for contrast phrases, and the refusal says why.

**Color zones.** The one global slider (`mix`, stored as `strength`) is a candidate. Per-zone hue, chroma and lightness are curves. The `colorzones.curves` row exists so that `vocab list` answers the question honestly. Its `note` points at colour balance rgb's per-zone sliders as the reachable alternative.

**The golden.** `tests/fixtures/vocab_golden.json` is `json.dumps(vocab.table(), indent=2, sort_keys=True)`. `vocab.table()` returns one dict per row with every `ParamSpec` field plus `status` and `pinned_path` (`ACTION_PATHS.get(...)` or `null`). A change to any number or status is a deliberate golden edit in the same commit.

### 4.4 `vocab.list`

```
scouter vocab list [--module M]
MCP: vocab_list {module?}
-> VocabResult{rows: [...], counts: {live, absolute_only, candidate, unreachable}}
```

A pure read with no bridge calls. Claude reads it once per session to learn which words work today.

---

## 5. Phrases (`scouter/core/phrases.py`)

### 5.1 Grammar (deterministic)

1. **Normalise.** Lowercase. `color` → `colour`. Replace every character outside `[a-z0-9.+%° -]` with a space. Collapse whitespace.
2. **Explicit amount.** Match `\bby\s+([+-]?\d+(?:\.\d+)?)\s*(ev|k|%|°|deg|degrees)?\b` once and remove it. It is allowed only when the phrase resolves to exactly **one** `adjust` move. The unit, if given, must match that row's display unit (`k` → `K`, `deg`/`degrees` → `°`). The number is in display units and becomes internal as `n / display_scale`. The sign comes from the phrase ("cooler by 300k" is −300 K). A negative number in the phrase is a `usage` error, because the direction words carry the sign.
3. **Intensity words** are matched longest first and removed. `a tiny bit`, `a touch`, `a little`, `a bit`, `slightly`, `a little bit` → **0.5**. `much`, `a lot`, `way`, `lots`, `strongly`, `a lot more` → **2.0**. None → **1.0**.
4. **Filler words** are removed: `make it`, `make the image`, `make the photo`, `please`, `can you`, `the image`, `the photo`, `the picture`, `it`, `a`, `bit`, `more of`.
5. **Exact synonym match** against the normalised synonym index. If nothing matches, raise `UsageError` with `detail.suggestions` = `difflib.get_close_matches(normalised, all_synonyms, n=3, cutoff=0.6)`.
6. `amount` (argument, `0 < amount ≤ 5`) replaces the intensity factor. Passing both `amount` and an in-phrase `by N` is a `usage` error.

### 5.2 The Move

```python
@dataclass(frozen=True)
class Move:
    module: str            # a vocab module, or "@tone" (resolved at run time, §5.4)
    param: str
    kind: str              # "adjust" | "set"
    notches: float | None  # adjust only: signed notches × intensity (delta = notches * spec.step)
    value: float | None    # set: absolute internal value; adjust with an explicit "by N": the signed internal delta
```

### 5.3 The phrase table

| key | synonyms (after normalisation) | moves (module.param: kind notches / value) |
|---|---|---|
| brighter | brighter, brighten, lighter, more exposure, brighten up | exposure.exposure: adjust +1 |
| darker | darker, darken, less exposure, lower exposure | exposure.exposure: adjust −1 |
| more_contrast | more contrast, punchier, add contrast, increase contrast, more punch | @tone.contrast: adjust +1 |
| less_contrast | less contrast, flatter, reduce contrast, lower contrast, softer contrast | @tone.contrast: adjust −1 |
| warmer | warmer, warm up, more warmth, warm | temperature.temperature: adjust +1 |
| cooler | cooler, colder, cool down, less warm, cool | temperature.temperature: adjust −1 |
| more_magenta | more magenta, less green | temperature.tint: adjust −1 |
| more_green | more green, less magenta | temperature.tint: adjust +1 |
| lift_shadows | lift shadows, lift the shadows, open shadows, open the shadows, open up the shadows, brighter shadows | colorbalancergb.shadows_luminance: adjust +1 |
| deepen_shadows | deeper shadows, darker shadows, crush the shadows, crush shadows | colorbalancergb.shadows_luminance: adjust −1 |
| brighter_midtones | brighter midtones, lift the midtones | colorbalancergb.midtones_luminance: adjust +1 |
| darker_midtones | darker midtones, lower the midtones | colorbalancergb.midtones_luminance: adjust −1 |
| brighter_highlights | brighter highlights, lift the highlights | colorbalancergb.highlights_luminance: adjust +1 |
| tame_highlights | tame the highlights, darker highlights, pull down the highlights, recover highlights | colorbalancergb.highlights_luminance: adjust −1 |
| deeper_blacks | deeper blacks, richer blacks | exposure.black: adjust +1 |
| faded_blacks | faded blacks, lift the blacks, matte blacks | exposure.black: adjust −1 |
| more_saturation | more saturation, more saturated, more colour, more colourful, saturate | colorbalancergb.global_saturation: adjust +1 |
| less_saturation | less saturation, less saturated, desaturate, muted, less colour | colorbalancergb.global_saturation: adjust −1 |
| more_vibrance | more vibrance, more vibrant | colorbalancergb.global_vibrance: adjust +1 |
| less_vibrance | less vibrance, less vibrant | colorbalancergb.global_vibrance: adjust −1 |
| more_chroma | more chroma | colorbalancergb.global_chroma: adjust +1 |
| less_chroma | less chroma | colorbalancergb.global_chroma: adjust −1 |
| warm_highlights | warm the highlights, warmer highlights | colorbalancergb.highlights_hue: set 60 ; colorbalancergb.highlights_chroma: adjust +1 |
| cool_shadows | cool the shadows, cooler shadows, blue shadows | colorbalancergb.shadows_hue: set 220 ; colorbalancergb.shadows_chroma: adjust +1 |
| teal_orange | teal and orange, orange and teal, cinematic grade | colorbalancergb.shadows_hue: set 200 ; colorbalancergb.shadows_chroma: adjust +1 ; colorbalancergb.highlights_hue: set 55 ; colorbalancergb.highlights_chroma: adjust +1 |
| stronger_grade | stronger colour grade, stronger grade, more grade, more colour grade | colorbalancergb.shadows_chroma, .midtones_chroma, .highlights_chroma: adjust +1 each |
| weaker_grade | weaker colour grade, weaker grade, less grade, less colour grade | the same three: adjust −1 each |

Pratik's "change the colour grade by this much" is `stronger_grade`/`weaker_grade` with `amount`. Claude turns "this much" into a multiplier of the notch. `sigmoid.skew` has **no phrase**: its sign convention is not verified, and a wrong-way phrase is worse than no phrase. It stays reachable as `darkroom adjust sigmoid skew ±x` once its slot is pinned.

**Sign conventions that need the host.** Three signs are darktable semantics that the unit suite cannot see: `exposure.black` (positive = deeper blacks), `temperature.tint` (below 1 = magenta), and the hue angles (60° warm, 200-220° teal/blue). Task 10 renders before/after `scout.stats` for each one and asserts the direction: mean luminance, the r/b ratio, and the r/b ratio of the brightest decile. A sign that comes back wrong is flipped in the table in task 10's commit. The golden records it.

**The golden.** `tests/fixtures/phrases_golden.json` maps each of 40 fixed test inputs (every synonym's canonical form, plus intensity and `by N` variants) to its resolution dict, `{key, intensity, moves: [...]}`.

### 5.4 `@tone` routing

At run time (in `say`, never in `resolve`), `@tone` becomes `filmicrgb` when the image's XMP has an **enabled, active** `filmicrgb` entry and no enabled active `sigmoid` entry. Otherwise it becomes `sigmoid`. `vocab.resolve` shows `module: "@tone"` with `routes: ["sigmoid", "filmicrgb"]`.

### 5.5 `vocab.resolve`

```
scouter vocab resolve PHRASE [--amount X]
MCP: vocab_resolve {phrase, amount?}
-> ResolveResult{phrase, normalised, key, intensity, moves: [{module, param, kind, delta?, value?, unit, status, pinned_path}]}
```

A pure read. It shows what `say` would do and which moves would be refused, with no darktable calls.

---

## 6. Relative moves (`scouter/core/moves.py`)

### 6.1 The read: where a current value comes from

Phase 0 finding 1 still holds. `dt.gui.action`'s status query returns a **normalised slider position**, not a value. The brief's "the bridge can read sliders" is true only in that sense, and phase 1 refused to invert positions. Phase 3 keeps that refusal. The new read verb reads values from the places that hold them:

| Order | Source | When |
|---|---|---|
| 1 | `journal` | the last landed Scouter write to this exact key used `commit=False`, so the sidecar is behind (the same fallback `darkroom._raw_and_before_state` applies) |
| 2 | `xmp` | the active entry decodes to a named field (`xmp.latest_value`) |
| 3 | `wb_table` | `temperature.temperature` only: the active coefficients go through `wb.kelvin(coeffs, camera)`. The camera comes from EXIF (`exif.camera_slug`). No table, or a value outside it, gives `None` plus the table's own reason |
| 4 | `neutral` | the module has **no** active history entry and the row declares a `neutral` (colour balance rgb sliders, 0.0) |
| — | refuse | `value_unknown`, exit 20 |

```
scouter darkroom get MODULE PARAM [--instance N]
MCP: darkroom_get {module, param, instance?}
-> ValueResult{module, param, instance, value, display_value, unit, source, enabled, fresh, status}
```

`darkroom.get` is a **read**, so it never commits and never bounces the view (phase 2 rule: reading must not move the darkroom). `fresh` is `history.watermark_fresh(...)[0]`. When it is `false`, a warning says the sidecar may predate darktable's live state and that `darkroom adjust` commits before it reads.

### 6.2 `darkroom.adjust`

```
scouter darkroom adjust MODULE PARAM DELTA [--instance N] [--no-commit] [--trust-sidecar]
                        [--educative off|brief|full] [--dry-run]
MCP: darkroom_adjust {module, param, delta, instance?, commit?, trust_sidecar?, educative?, dry_run?}
```

Algorithm:

1. `spec = vocab.spec(module, param)`. No row → `usage` ("not in the conversational vocabulary; `darkroom set` reaches any pinned pair"). Status gate as in §4.2. `delta` must be finite and non-zero (`usage` otherwise).
2. **Dry run:** return `outcome="dry_run"` with `plan=[{"action": "darkroom.adjust", "path": ACTION_PATHS[...], "delta": delta, "clamp": [lo, hi]}]` and the reason "dry run: no darktable calls; the current value was not read". No bridge calls.
3. `resolved = darkroom._current_image()`, then `darkroom._require_job_image(job, resolved.image_id)`, then `dtbridge.require_darkroom()`.
4. **Pre-commit** unless `trust_sidecar`: `darkroom._commit(job, image_id, strategy="auto", attempts=1, expect_change=False)`. This flushes any slider Pratik moved by hand, so the value read in step 5 is darktable's, not a stale file's. A `CommitTimeout` here becomes a warning, not a failure: the read then carries `fresh: false`.
5. `previous, source, reason = read_current(resolved, spec, instance, job)`. `previous is None` → `ValueUnknown` with `detail={module, param, source_tried, reason}`.
6. `target = previous + delta`. For hue rows, `target %= 360`. Otherwise `target, clamped = clamp(spec, target)`. For temperature, the clamp also narrows to the camera table's `[min k, max k]`.
7. If `clamped` and `abs(target - previous) < 1e-6`: return `noop=True`, `committed=False`, and the summary "already at the {lo|hi} limit ({value} {unit}); nothing sent". No bridge write.
8. `result = darkroom.set_param.__wrapped__(module, param, target, instance=instance, commit=commit, job=job)`. The phase 2 no-op shortcut, commit readback and journal all apply unchanged.
9. `applied`: taken from `result.applied`. For temperature, re-read Kelvin through the table. `None` stays `None`, with `set_param`'s own warning.
10. `summary` = `"{ui}: {delta} ({previous} → {applied or target})"`, each number with its display unit (for example `exposure › exposure: +0.3 EV (1.55 EV → 1.85 EV)`), plus `" [clamped at {hi|lo}]"` when clamped.
11. `explain` = `explain.lines([MoveNote(spec, previous, target)], level=explain.resolve_level(educative), job=job)`.

```python
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
```

**Why pre-commit by default.** The watermark (phase 2 §13) detects on-disk changes only. A slider Pratik drags by hand lives in darktable's memory until the next view change, so the file on disk looks fresh while it is wrong. Relative moves are exactly where that matters: "brighter" from a stale value lands somewhere Pratik never saw. One expect-no-change bounce costs about 1 s (task 12's measured path) and removes the problem. `--trust-sidecar` skips it for scripts that own the darkroom.

**Kelvin and tint.** The measured table was taken at tint 1.0 (`wb-em10iii.json`: `"tint": 1.0`). A relative Kelvin read at another tint is approximate. When the image's tint is not known to be 1.0, which is always the case since tint is not decodable, the result carries the warning "Kelvin read from the r/b ratio against a table measured at tint 1.0". The undo path does not change: phase 2's tier 3 (Kelvin inversion) restores it.

### 6.3 `darkroom.say`

```
scouter darkroom say PHRASE [--amount X] [--no-commit] [--trust-sidecar] [--educative L] [--dry-run]
MCP: darkroom_say {phrase, amount?, commit?, trust_sidecar?, educative?, dry_run?}
-> SayResult{phrase, key, intensity, group, moves: [AdjustResult dicts], refused: [...], summary, explain}
```

1. `res = phrases.resolve(phrase, amount)`. A `UsageError` with suggestions propagates.
2. **Dry run:** return the resolution with each move's status and pinned path. No darktable calls, and `@tone` is not routed (the reason says so).
3. Resolve `@tone` (§5.4) against the darkroom image's XMP.
4. **Pre-validate every move** before any write. If any move's status is not `live` (for adjust) or not `live`/`absolute_only` (for set), refuse the **whole phrase** with that move's error class, and list every refused move in `detail.refused`. A phrase lands whole or not at all, as far as pinning can decide.
5. `group = f"say-{seq}"`, where `seq` is the next journal seq. Set `journal.GROUP` for the duration.
6. Pre-commit once (unless `trust_sidecar`), then run each move in order: `moves._adjust_core(...)` (adjust's body after gating and pre-commit, with educative off), or `darkroom.set_param.__wrapped__(...)` for `set` moves. Educative lines are produced once, for all moves, after the loop, so concept lines are not repeated per move.
7. A move that raises after an earlier move landed makes the verb return `outcome="partial"` (exit 9, phase 2's code for "some of it worked"), with the landed moves in `moves` and the failure in `refused`. A failure on the first move re-raises its own error.
8. `summary` = the moves' summaries joined with `"; "`.

### 6.4 Undo by group

`journal.Entry` gains `group: str | None = None`. `journal.begin` stamps it from a module-level `contextvars.ContextVar[str | None]` named `GROUP` (default `None`). **No write verb's signature changes.** Old journal lines without the key load as `None`.

`darkroom.undo_last` gains `single: bool = False` (§14.1). By default (`single=False`), when the newest open entry has a `group`, every open entry with the same group is undone, newest first (the loop calls the current single-entry body, refactored into `_undo_entry(entry, job, dry_run)` without changing it), so each entry commits once. A four-move phrase costs four undo commits, about 7 s. That cost is accepted: sharing one commit would mean changing the tiered temperature undo, which is engine code this phase does not touch. Entries without a group (every phase 1 and phase 2 write) behave exactly as before. `UndoResult` gains `undone_group: list[dict]` (empty for a single entry) and keeps `undone` = the newest entry for compatibility. `single=True` restores the old single-entry behaviour.

---

## 7. Educative mode (`scouter/core/explain.py`), row 346

### 7.1 Config and override

`config.json` gains one **top-level** key, `"educative": "off" | "brief" | "full"`, default `"brief"`. Any other value is a `usage` error at load time naming the three values. The key is added to `TOP_KEYS` and to `Config` as `educative: str = "brief"`. `scouter config init`'s starter file writes it with a `_comment`.

`explain.resolve_level(override)` returns `override` when it is given (validated against `LEVELS`). Otherwise it returns `config.load().educative`. If loading the config fails, it returns `"brief"`: a broken config must not break editing, and the config verbs report that error themselves.

### 7.2 What each level produces

A **phrasing layer only**. It reads `ParamSpec` text, the move's numbers and the session's explained set. It changes no value, sends no bridge call, and changes no other field of the result.

- `off`: `explain == []`.
- `brief`: one sentence per move, in this format:

  `"{ui} {direction} {delta} ({previous} → {target}): {why}; watch {watch}."`, where each number carries its display unit (`1.55 EV`, `5%`, `220°`, a bare number for ratios)

  `direction` is `up`/`down` for adjust and `set to` for set. For example: `"exposure › exposure up +0.3 EV (1.55 EV → 1.85 EV): brightens the whole frame evenly, before any tone mapping; watch the highlights for clipping."`
- `full`: for each move whose `spec.concept` is **not** in the session's explained set, the concept paragraph `"[{concept title}] {text}"` comes first, and the concept is added to the set. The brief sentence follows. A concept is explained once per session and never repeated.

**Session.** The session is the job (`--job` / `$SCOUTER_JOB`, or the MCP server's pinned session job, §11.1). The explained set lives in the job state file under `"explained_concepts"` (a sorted list), written through `paths.job_state_update` (the locked read-modify-write seam from phase 2 task 12). A new job starts with an empty set. `scouter` CLI calls without `$SCOUTER_JOB` are each their own job, so every CLI call is a fresh session. The README says to set `SCOUTER_JOB` for a teaching session in the shell.

### 7.3 Concepts

| id | title | text (pinned in the golden) |
|---|---|---|
| exposure | Scene-referred exposure | Exposure scales all the light in the scene by the same factor, measured in EV (one EV doubles or halves it). In darktable's scene-referred workflow you set it first, so the subject sits at a sensible brightness, and let the tone mapper handle the extremes. |
| tone_mapper | Tone mapping | Sigmoid (or filmic rgb) squeezes the scene's wide range of light into what a screen can show. Its contrast slider sets how steep that curve is in the middle: more contrast separates midtones but pushes shadows and highlights toward the ends. |
| white_balance | White balance | White balance tells darktable what colour the light was. Raising the temperature slider says the light was bluer, so darktable compensates and the picture turns warmer; tint trades green against magenta. Get it right before any creative colour. |
| color_balance | Color balance rgb | Color balance rgb is darktable's colour-grading module. Master sliders act on the whole image; the 4-ways tab grades shadows (lift), midtones (power) and highlights (gain) separately, each with luminance, hue and chroma. |
| chroma_saturation | Chroma, saturation, vibrance | Chroma is colourfulness independent of brightness; saturation also darkens as colour increases, which reads more natural on skin; vibrance boosts the least colourful pixels most. Small numbers go a long way: 5 % is a visible move. |
| grading | Colour grading | A grade gives a look by tinting shadows and highlights differently, classically cool shadows and warm highlights. Hue picks the tint, chroma picks how much; keep chroma low and judge on a neutral grey or skin. |
| black_level | Black level | Black level correction moves the point that renders as pure black. Deeper blacks add density; lifting them gives a faded, matte look. It is a strong control: a few thousandths are visible. |
| color_zones | Color zones | Color zones adjusts hue, chroma and lightness by the colour a pixel already has (for example, only the blues). Scouter can reach only its global mix; the zone curves need darktable's own editor. |
| presets | Styles as presets | A darktable style is a saved list of module settings. Applying one appends those settings to an image's history, so it acts like a filter you can stack; it never replaces what is there, and each application is visible in the history panel. |

Every `ParamSpec.concept` is one of these ids (a test asserts it). The `presets` concept is used by `preset.save` and `preset.apply` (§8), which also produce one brief line of their own: `"saved {n} module(s) ({modules}) as style '{name}': apply it from darktable's styles panel or with preset apply."` and `"applied style '{name}' ({n} history item(s) appended): compare with the history panel; darktable's own history is the way back."`

**The golden.** `tests/fixtures/explain_golden.json` holds, for every row with a status other than `unreachable`, the `brief` line for a fixed synthetic move (`previous = lo + step`, `target = lo + 2*step`, adjust), plus every concept line. Wording changes are deliberate golden edits.

---

## 8. Named presets (`scouter/core/presets.py`)

A preset **is** a darktable style. Scouter adds three things: a default module list (what this session changed), a durable copy in library memory, and apply-to-the-open-image through a bounce.

### 8.1 `preset.save`

```
scouter preset save NAME [--modules a,b] [--description D] [--educative L] [--dry-run] [--yes]
MCP: preset_save {name, modules?, description?, educative?, confirm?, dry_run?}
```

1. `paths.validate_style_name(name)`, the same door phase 2 guards.
2. **Dry run:** the plan (name, modules if given, "modules default to this session's edits") with no bridge calls.
3. `resolved = darkroom._current_image()`, then `_require_job_image`, then `require_darkroom`.
4. **Flush:** `darkroom._commit(job, image_id, attempts=1, expect_change=False)`. The sidecar is what `style.extract` reads, so it must hold what Pratik sees.
5. `modules` defaults to `session_modules(job, image_id)`: the distinct `module` values of this job's journal entries for this image that are landed (outcome not an error), not undone, and not `module.reset`, in first-touched order, minus `cfg.style.never_include`. If that is empty, raise `usage` ("nothing was edited on this image in this session; pass --modules"). A preset of the whole history is `--modules` plus the list of modules, stated on purpose.
6. `sr = style.extract.__wrapped__(image_id, name, modules, description=description or f"Scouter preset from {basename}", confirm=confirm, job=job)`. Phase 2's gate, filter, verbatim params, `modules_missing` and `StyleWriteFailed` all apply unchanged. When the name exists and `confirm` is false, the extract returns `dry_run` and so does `preset.save`.
7. **Library memory** (when `cfg.library_root` is set): copy `sr.dtstyle_path` to `<library_root>/scouter/memory/presets/<name>.dtstyle` and upsert `presets.json` in the same directory. If a preset of that name exists there (only reachable with `confirm`), the old file is first renamed `<name>.<UTC ts>.dtstyle`. Never overwrite blind. The index write is atomic (temp file + `os.replace`). With no `library_root`: a warning, "saved in darktable and in the styles cache only; set library_root to keep presets in the backed-up library memory".

```json
{"schema": 1,
 "presets": [{"name": "harbour-warm", "created_at": "2026-09-24T16:02:11Z",
              "source_path": "2026/2026-06-28 harbour-wedding/raw/…_0001.orf",
              "image_id": 12, "modules": ["temperature", "colorbalancergb"],
              "dtstyle": "harbour-warm.dtstyle", "sha256": "…", "description": "…",
              "scouter_version": "0.3.0"}]}
```

`source_path` is relative to `library_root` when the image is inside it, and absolute otherwise (a one-picture session outside the library).

### 8.2 `preset.list`

```
scouter preset list
MCP: preset_list {}
-> PresetListResult{presets: [{name, scouter, in_darktable, modules, created_at}], darktable_only: [...], missing_in_darktable: [...]}
```

One `list_styles` bridge call merged with `presets.json`. A preset in the index but missing from darktable (deleted in the GUI) is listed in `missing_in_darktable`, with the fix "re-import `<memory>/presets/<name>.dtstyle` in darktable's styles module, or save it again". Nothing is re-imported silently.

### 8.3 `preset.apply`

```
scouter preset apply NAME [IMAGE] [--educative L] [--dry-run]
MCP: preset_apply {name, image?, educative?, dry_run?}
```

- The style must exist (`list_styles`). Otherwise `usage` with `difflib` suggestions from the style names.
- **`image` omitted, or equal to the darkroom image → the open image.** The new Lua method `darkroom.apply_style {image_id, name}` (§9) leaves the darkroom, applies the style **in the lighttable**, and returns to the darkroom. Applying while the darkroom holds the image risks the darkroom's in-memory history overwriting the append when the view next changes. That is unverified, so the design avoids the question rather than betting on it. Leaving first also commits Pratik's pending edits, which is what a commit bounce does. The Python side then runs `darkroom._commit(... expect_change=True)` and verifies that the sidecar's `history_end` advanced past the value read before the call. If it did not advance: `commit_timeout` with both numbers. Journal: one entry, `verb="preset.apply"`, `module=""`, `param=""`, `before={"history_end": n}`, `after={"history_end": m, "style": name}`.
- **`image` names another image → the library path.** `style.apply.__wrapped__(image, name)` is phase 1's single-image apply, which appends and does not touch the view.
- Result: `PresetResult{action: "apply", name, applied_to: "darkroom" | "library", history_end_before, history_end_after, explain}`.

### 8.4 Undo of a preset application

`darkroom.undo_last` on a `preset.apply` entry refuses with `unsupported_undo`. It marks the entry undone with a note, exactly as it does for `module.reset`, so older writes stay reachable. The message: "a style application appended {m − n} history item(s); darktable's Lua API cannot remove history items. In darktable, click the history item just below the style's first item and compress history, or press Ctrl-Z." This is a load-bearing honest limit, and the README says so.

### 8.5 `preset.apply_job`

```
scouter preset apply-job NAME --job-slug S [--exclude a,b] [--force] [--dry-run] --yes
MCP: preset_apply_job {name, job_slug, exclude?, force?, confirm?, dry_run?}
```

A thin, always-gated delegate to `style.apply_set.__wrapped__(name, job_slug=..., exclude=..., force=..., confirm=..., dry_run=...)`, returning its `StyleSetResult` unchanged: batching, the idempotency ledger (R16/R18), `outcome="partial"` and all. It exists so the preset family is complete as MCP tools, and it adds nothing but the name.

---

## 9. Bridge protocol addition

| Method | Params | Result |
|---|---|---|
| `darkroom.apply_style` | `{"image_id": 12, "name": "harbour-warm"}` | `{"applied": true, "name": "…", "image_id": 12, "view": "darkroom", "left_ms": 180, "returned_ms": 60}`. It errors (a JSON-RPC error, which the Python side maps like any bridge failure) when the style is not found, when the darkroom is not showing `image_id` (message prefix `wrong image loaded:`, mapped to `not_in_darkroom` by the existing `_WRONG_IMAGE_RE`), or when either leg of the bounce does not land in its budget. |

Implementation: `commit_view_bounce(between)` gains an optional `between` callback, run once in the lighttable dwell, after leg 1 lands and before leg 2 starts. `darkroom.apply_style` calls it with `function() image:apply_style(style) end`. `between = nil` keeps today's bounce byte for byte. Client timeout: `"darkroom.apply_style": 30.0` (below `STALE_AGE_SECONDS = 600`, so the cross-language invariant test still passes).

---

## 10. Host probe (task 10)

### 10.1 Candidate paths: one darktable launch per candidate

An unrecognised `dt.gui.action` path wedges darktable 5.4.1's Lua worker for the whole session (`docs/measurements.md`, white balance). A probe therefore gets **one launch per candidate**:

1. Launch `darktable <scratch ORF>`, the way every host class launches, and wait for the bridge.
2. `darkroom.status {path: candidate}`. The call is sent directly through `dtbridge.call`. It is not `dtbridge.action_path(..., probe_unpinned=True)`, because no module/param mapping is involved. It gets a **10 s** budget.
3. If it answers with `nan: false` → **pinned**. Then `view.current` must still answer within 5 s, which proves the worker is not wedged.
4. If it times out, or `view.current` then times out → **wedges**. `nan: true` → **absent**. In both cases darktable is killed through the quit escalation and the next candidate starts a fresh launch.
5. For each `(module, param)` the candidates are tried in `candidate_paths` order, and the probe stops at the first one pinned.

Output: `docs/measurements-generated.md` gets a table (candidate, verdict, ms). The implementer then hand-edits `ACTION_PATHS` with every pinned pair, citing the run. Launch count: 11 colour balance rgb candidates (2 master rows with one spelling each, 9 four-ways rows with two spellings each), 3 filmic and 1 color zones give at most 24 launches at about 25 s each, roughly 10 minutes, supervised.

### 10.2 Slot pinning: the sentinel method

This is phase 2 task 12's proven method, for every pinned pair whose XMP field is not named: `sigmoid.skew`, `colorbalancergb.global_saturation`, plus anything §10.1 pinned. In **one** launch: enable the module, set a sentinel that is unlikely to collide (for example `0.173` for a ratio or fraction, `37.0` for a hue), commit, read the sidecar, and find the float slot equal to the sentinel. Exactly one match → pin it as `named_slots[i] = param` in `xmp.LAYOUTS[(module, modversion)]`. A new `(module, modversion)` gets a layout with the observed `size` and `float_count`. Zero or several matches → not pinned, and the value is recorded. The live sidecar is copied to a throwaway file, never over a committed fixture (phase 2 final review, should-fix 4). A new fixture `tests/fixtures/<module>-v<n>.xmp` is committed by hand with the layout, so the unit suite asserts against it.

### 10.3 Signs, preset round trip, conversation timing

Same launch as §10.2:
- **Signs:** for `exposure.black`, `temperature.tint` and the hue presets, `scout.stats` (512 px) before and after `darkroom.set`. The assertions are mean luminance down for `black +`, the r/b ratio down for tint below 1, and the brightest-decile r/b up for highlights hue 60° with chroma. A failed sign is flipped in the phrase table and recorded.
- **Preset:** `darkroom say "warmer"` → `preset save scouter-probe-<pid>` → `preset list` shows it → `darkroom say "cooler"` → `preset apply scouter-probe-<pid>` (open image, bounce) → `history_end` advanced → `style.delete` with the `scouter-` prefix and `confirm` (phase 2 R14) → the memory copy is removed from the temp library root.
- **Timing:** five `darkroom.say` calls, each timed. The median is asserted below 3.5 s for one-move phrases.

### 10.4 Restore scope (ruling R22, closed)

Host classes install the plugin through `tests/host_support.PluginInstall` (extracted from `test_host_phase2._PluginInstall` in task 9), which backs up and restores **only** `~/.config/darktable/lua/darktable_mcp.lua` and `~/.config/darktable/luarc`. The phase 2 live run's manual wholesale `~/.config/darktable` snapshot is **not** part of the procedure and is forbidden in the task 10 brief. That snapshot rolled `data.db` back and would discard anything Pratik changed in darktable during a run. `library.db` and `data.db` are neither backed up nor restored. Probe styles are deleted through the bridge by name.

---

## 11. Cheap closures from phase 2

Everything the phase 2 ledger and README left open, sorted by cost. Only the cheap ones are in this phase.

| Item | Source | Cheap? | Phase 3 action |
|---|---|---|---|
| **MCP calls do not share a job.** `paths.current_job()` without `$SCOUTER_JOB` returns `job-<UTC second>-<pid>`, so two MCP calls a second apart write to different journals. `undo_last` in a Claude conversation then finds an empty journal, and the educative "already explained" set would reset on every call. | found while designing §7 (verified by reading `paths.current_job` and `mcp/server.py`, which never sets the variable) | yes | §11.1 |
| R22: host restore rolled `data.db` back wholesale | ledger R22, task 14 report concern 2 | yes | §10.4 (helper extraction + brief rule + a non-host test) |
| `wb.load_table` ignores the table's `version` | task 11 re-review, "out of scope for task 14" | yes | refuse a table whose `version` is not `1` with a `ValueError` naming the file and the version. `wb.kelvin` already turns that into `kelvin: null` + reason |
| README phase 1 honest limit "`style extract --modules` can honestly fail to filter" is stale (phase 2 made the filter real). It applies only to `--no-filter` now | README vs phase 2 §9.1 | yes | rewrite that bullet |
| `darkroom status` reports positions, not values | README honest limit | yes (by addition) | `darkroom get` (§6.1) is the value read. `status` stays the liveness check, and its warning names `darkroom get` |
| R20: darkroom cannot move onto a frame imported in the same session | ledger R20 | **no**: needs a new darktable mechanism and host work | stays an honest limit |
| Host runs mutate `library.db` (film rolls, ratings) | task 14 concern 1 | **no**: needs a separate darktable configdir for host runs, which costs the "real library" property | stays documented |
| Sony A7 / NEX-5N WB tables | ledger 09:26 | out of scope (row 390) | — |
| E12 300-frame job | phase 2 exit | out of scope (row 390) | — |

### 11.1 The MCP session job

`scouter.mcp.server.main()` sets `SCOUTER_JOB` to `mcp-<UTC %Y%m%d-%H%M%S>-<pid>` **if and only if** it is unset. It validates the value with `paths.validate_job` and logs it once to stderr. An explicit `SCOUTER_JOB` in the client config wins, so Pratik can name a session. The server's `call()` is unchanged: `registry.call` already reads the ambient job. One MCP server process is therefore one session, for the journal (undo), ruling 30's image guard, and the explained-concepts set. A test runs `main`'s env step in isolation (the function `_pin_session_job(environ) -> str`), so no server is started.

---

## 12. Honest limits, phase 3 (for the README)

- **Relative moves read values from the XMP, not from sliders.** A slider darktable reports only as a position is `absolute_only` until a host probe pins its XMP slot. `darkroom adjust` refuses it with `value_unknown` and names the absolute `darkroom set` form.
- **A vocabulary word is only as real as its pin.** Rows whose action path no probe has confirmed are `candidate`, and every relative or phrase verb refuses them with `not_pinned`. `scouter vocab list` shows the current status of every word.
- **Relative white balance needs the camera's measured table and assumes tint 1.0.** Without a table: `value_unknown`, naming `scouter wb calibrate`.
- **`darkroom adjust` bounces the view once before it reads** (about 1 s), because a hand-moved slider is invisible on disk until darktable commits. `--trust-sidecar` skips this for scripts that own the darkroom.
- **Undoing a preset application is refused.** darktable's Lua API cannot remove history items. Use the history panel.
- **Color zones' zone curves are unreachable.** Only its global mix is a candidate.
- **Presets are darktable styles.** They are not Lightroom presets, and applying one always appends.

---

## 13. Core signatures

```python
# scouter/core/vocab.py
@dataclass(frozen=True) class ParamSpec: ...                      # §4.1
PARAMS: tuple[ParamSpec, ...]
STATUSES = ("live", "absolute_only", "candidate", "unreachable")
def spec(module: str, param: str) -> ParamSpec | None: ...
def status(s: ParamSpec) -> str: ...
def xmp_named(module: str, param: str) -> bool: ...
def clamp(s: ParamSpec, value: float) -> tuple[float, bool]: ...
def to_display(s: ParamSpec, value: float) -> float: ...
def closest_live(s: ParamSpec) -> ParamSpec | None: ...
def table() -> list[dict]: ...
def list_vocab(module: str | None = None, *, job: str | None = None) -> VocabResult: ...   # verb vocab.list

# scouter/core/phrases.py
@dataclass(frozen=True) class Move: ...                           # §5.2
@dataclass(frozen=True) class Phrase: key: str; synonyms: tuple[str, ...]; moves: tuple[Move, ...]
@dataclass(frozen=True) class Resolution: phrase: str; normalised: str; key: str; intensity: float; explicit: bool; moves: tuple[Move, ...]
PHRASES: tuple[Phrase, ...]
def normalise(text: str) -> str: ...
def resolve(text: str, amount: float | None = None) -> Resolution: ...
def resolve_verb(phrase: str, *, amount: float | None = None, job: str | None = None) -> ResolveResult: ...  # verb vocab.resolve

# scouter/core/moves.py
def read_current(resolved: ResolvedImage, s: ParamSpec, instance: int, job: str) -> tuple[float | None, str, str | None]: ...
def route_tone(xmp_path_raw: str) -> str: ...
def get(module: str, param: str, *, instance: int = 0, job: str | None = None) -> ValueResult: ...          # verb darkroom.get
def adjust(module: str, param: str, delta: float, *, instance: int = 0, commit: bool = True,
           trust_sidecar: bool = False, educative: str | None = None, dry_run: bool = False,
           job: str | None = None) -> AdjustResult: ...                                                     # verb darkroom.adjust
def say(phrase: str, *, amount: float | None = None, commit: bool = True, trust_sidecar: bool = False,
        educative: str | None = None, dry_run: bool = False, job: str | None = None) -> SayResult: ...     # verb darkroom.say

# scouter/core/explain.py
LEVELS = ("off", "brief", "full")
CONCEPTS: dict[str, tuple[str, str]]          # id -> (title, text)
@dataclass(frozen=True) class MoveNote: spec: ParamSpec; previous: float | None; target: float | None; kind: str = "adjust"
def resolve_level(override: str | None) -> str: ...
def brief_line(note: MoveNote) -> str: ...
def concept_line(concept_id: str) -> str: ...
def lines(notes: list[MoveNote], *, level: str, job: str) -> list[str]: ...
def preset_lines(action: str, name: str, modules: list[str], items: int, *, level: str, job: str) -> list[str]: ...

# scouter/core/presets.py
def memory_dir(cfg: Config) -> Path | None: ...
def session_modules(job: str, image_id: int) -> list[str]: ...
def save(name: str, *, modules: list[str] | None = None, description: str = "", educative: str | None = None,
         confirm: bool = False, dry_run: bool = False, job: str | None = None) -> PresetResult: ...            # verb preset.save
def list_presets(*, job: str | None = None) -> PresetListResult: ...                                           # verb preset.list
def apply(name: str, image: str | int | None = None, *, educative: str | None = None, dry_run: bool = False,
          job: str | None = None) -> PresetResult: ...                                                         # verb preset.apply
def apply_job(name: str, *, job_slug: str, exclude: list[str] | None = None, force: bool = False,
              confirm: bool = False, dry_run: bool = False, job: str | None = None) -> StyleSetResult: ...     # verb preset.apply_job

# scouter/core/journal.py (addition)
GROUP: contextvars.ContextVar[str | None]
# Entry.group: str | None = None

# scouter/core/darkroom.py (change)
def undo_last(*, single: bool = False, dry_run: bool = False, job: str | None = None) -> UndoResult: ...
```

New result dataclasses in `scouter/types.py`: `VocabResult`, `ResolveResult`, `ValueResult`, `AdjustResult`, `SayResult`, `PresetResult`, `PresetListResult`. `UndoResult` gains `undone_group: list[dict]`.

---

## 14. The three layers

### 14.1 Verb table (9 new; 44 total)

| Verb id | CLI | MCP tool | Writes | Gated |
|---|---|---|---|---|
| `vocab.list` | `scouter vocab list [--module M]` | `vocab_list` | no | no |
| `vocab.resolve` | `scouter vocab resolve PHRASE [--amount X]` | `vocab_resolve` | no | no |
| `darkroom.get` | `scouter darkroom get MODULE PARAM [--instance N]` | `darkroom_get` | no | no |
| `darkroom.adjust` | `scouter darkroom adjust MODULE PARAM DELTA [--instance N] [--no-commit] [--trust-sidecar] [--educative L] [--dry-run]` | `darkroom_adjust` | yes | no (one image, one undoable history item) |
| `darkroom.say` | `scouter darkroom say PHRASE [--amount X] [--no-commit] [--trust-sidecar] [--educative L] [--dry-run]` | `darkroom_say` | yes | no (one image, undoable as a group) |
| `preset.save` | `scouter preset save NAME [--modules a,b] [--description D] [--educative L] [--dry-run] [--yes]` | `preset_save` | yes | **when the name exists** (phase 2's extract gate) |
| `preset.list` | `scouter preset list` | `preset_list` | no | no |
| `preset.apply` | `scouter preset apply NAME [IMAGE] [--educative L] [--dry-run]` | `preset_apply` | yes | no (one image, like `style.apply`) |
| `preset.apply_job` | `scouter preset apply-job NAME --job-slug S [--exclude a,b] [--force] [--dry-run] --yes` | `preset_apply_job` | yes | **yes** |

`darkroom.undo_last` gains one argument, `single` (bool, default `False`, CLI `--single`, help "undo only the newest write, not the whole move it was part of"). It is phrased as an opt-out because a CLI `store_true` flag cannot switch off a `True` default, and parity needs one name in every layer.

Two new CLI groups appear from the registry: `vocab`, `preset`. `--pretty` gains a table renderer for `vocab.list` (module.param, status, unit, clamp, step) in `cli/tables.py`.

A negative `DELTA` on the CLI: argparse treats `-0.1` as a positional when no option of that parser looks like a negative number. None does, so `scouter darkroom adjust sigmoid contrast -0.1` works. A test pins it.

### 14.2 MCP schemas

Handwritten, in `scouter/mcp/schemas.py`, following phase 1/2 conventions (`additionalProperties: false`, explicit `required`, `IMAGE`, `DRY_RUN`, `INSTANCE`). New shared fragments:

```python
EDUCATIVE = {"type": "string", "enum": ["off", "brief", "full"],
             "description": "explanation level for this call; omit to use the config's `educative` (default brief)"}
PHRASE = {"type": "string", "minLength": 1, "maxLength": 200,
          "description": "what to change, in plain words, e.g. 'warmer', 'a touch less contrast', "
                         "'lift the shadows', 'cooler by 300k'; call vocab_list for every phrase"}
AMOUNT = {"type": "number", "exclusiveMinimum": 0, "maximum": 5,
          "description": "multiplier on the phrase's default step (1 = one notch); replaces words like 'slightly'"}
PRESET_NAME = {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
               "description": "preset (darktable style) name"}
```

Tool descriptions (pinned by the golden; each tells the calling model when to use it):

- `darkroom_say`: "Edit the image open in darktable's darkroom by a plain-words phrase ('warmer', 'less contrast', 'lift the shadows', 'stronger colour grade'). Each move reads the current value, applies one step (scaled by 'slightly'/'much' or `amount`) and is visible live in darktable. All moves of one phrase are undone together by darkroom_undo_last. Refuses the whole phrase if any move is not reachable yet (not_pinned) or its current value cannot be read (value_unknown). Prefer this for conversational editing; use darkroom_adjust for an exact delta."
- `darkroom_adjust`: "Change one module parameter by a relative delta in internal units (EV for exposure, K for temperature, fractions for color balance rgb: 0.05 means 5 %). Reads the current value first (one view bounce to flush unsaved slider moves unless trust_sidecar), clamps to Scouter's safe range, sets it, and returns previous/target/applied plus a one-line summary."
- `darkroom_get`: "Read one parameter's current VALUE from the image's XMP sidecar (or Kelvin via the camera's measured table). Never moves the view; `fresh: false` means darktable may hold newer, uncommitted changes."
- `vocab_list`: "The conversational vocabulary: every module parameter Scouter can move by words, with unit, safe range, step, and status (live, absolute_only, candidate, unreachable). Read once per session."
- `vocab_resolve`: "Show what a phrase would do (module, parameter, delta) and whether each move is reachable, without touching darktable."
- `preset_save`: "Save the darkroom image's edits from this session as a named darktable style (a preset/filter). Modules default to the ones changed in this session; pass modules to choose. Needs confirm=true only to replace an existing preset of that name."
- `preset_list`: "List presets: darktable's styles merged with Scouter's saved-preset index, flagging any deleted in darktable."
- `preset_apply`: "Apply a preset to the image open in the darkroom (via a quick view bounce, visible live) or to another image by path/id. Always appends to history; cannot be undone by Scouter (use darktable's history panel)."
- `preset_apply_job`: "Apply a preset to every frame of a job, in batches, with per-image results and Scouter's idempotency ledger. Always gated: confirm=true."

`darkroom_undo_last`'s schema gains `single` (boolean, default false, "undo only the newest write, not the whole phrase it was part of").

The golden `tests/fixtures/scouter_mcp_golden.json` is updated **by each task that adds or changes a tool**, in that task's commit, with the diff in the commit message. That differs from phase 2's single regeneration because phase 3's verb tasks run sequentially and each one owns its own tools.

---

## 15. Errors and exit codes

| Class | `code` | Exit | Raised when | `fix` |
|---|---|---|---|---|
| `ValueUnknown` | `value_unknown` | 20 | a relative move or phrase needs a current value that neither the journal, the XMP, the WB table nor a declared neutral can give | "Scouter cannot read {module}.{param}'s current value (detail.reason). Set it absolutely: `scouter darkroom set {module} {param} <value>`; relative moves on it need its XMP slot pinned by a host probe (or, for white balance, `scouter wb calibrate`)." |
| `NotPinned` | `not_pinned` | 21 | a vocabulary row whose status is `candidate` or `unreachable` is asked to move | "{module}.{param} has no pinned darktable action path (status {status}); {closest live alternative, or the note}. Pinning is a host-probe job (docs/superpowers/specs/2026-09-24-scouter-phase3-design.md §10.1)." |

`EXIT_CODES` gains `"value_unknown": 20` and `"not_pinned": 21`. Both classes join `ERROR_CLASSES` and get error-path tests. `darkroom.say`'s `outcome="partial"` reuses exit 9. An unknown phrase, a bad `amount`, a bad `educative` value, `amount` together with `by N`, and an empty session module list are all `usage` (2).

---

## 16. Observability

No schema change. `PHASE = 3`, `__version__ = "0.3.0"`. Each new verb is `@metrics.instrumented` and emits one event. Nested calls go through `__wrapped__`: `adjust` → `set_param`, `say` → `adjust`/`set_param`, `preset.save` → `style.extract`, `preset.apply` → `style.apply`, `preset.apply_job` → `style.apply_set`. Payload counters, which the audit line captures in full:

- `darkroom.adjust`: `source`, `clamped`, `noop`, plus `ms_precommit` and `ms_set` in `plan[0]` (so no new top-level field is needed)
- `darkroom.say`: `key`, `len(moves)`, `len(refused)`, `group`
- `preset.save`: `modules`, `dtstyle_path`, `memory_path`
- `preset.apply`: `applied_to`, `history_end_before`, `history_end_after`

Acceptance numbers for spec 6.1 become computable from `metrics.db` with no new instrumentation. **Undo rate** = `darkroom.undo_last` events / (`darkroom.say` + `darkroom.adjust`) events per job. **Phrase refusal rate** = `darkroom.say` events whose outcome is `error:not_pinned` or `error:value_unknown`, over all `darkroom.say` events. Both are the signals that say which vocabulary rows to probe next.

---

## 17. Test strategy

Every new test is `unittest.TestCase` under `uv run --extra dev python -m pytest tests -q`. **No new non-host test touches darktable, `~/.config`, the vault or the network.** `tests/conftest.py`'s five pinned paths (`OBSIDIAN_VAULT_PATH`, `SCOUTER_CACHE`, `SCOUTER_CONFIG`, `SCOUTER_DARKTABLE_LIBRARY_DB`, and the two bridge doors `XDG_CACHE_HOME` + `DARKTABLE_MCP_PLUGIN_PATH`), plus the `SCOUTER_DARKTABLE_CLI` host guard, stay exactly as they are. A test that needs its own tree uses `allowed_tree`.

| Layer | File | Doubles / fixtures |
|---|---|---|
| Foundations | `tests/test_phase3_foundations.py` | temp `$SCOUTER_CONFIG`; `journal.GROUP` round-trip; error classes |
| Vocabulary | `tests/test_vocab.py` | **golden** `tests/fixtures/vocab_golden.json`; `ACTION_PATHS` patched with `mock.patch.dict` to prove statuses follow pins |
| Phrases | `tests/test_phrases.py` | **golden** `tests/fixtures/phrases_golden.json` (40 inputs); suggestions on a typo; `by N` unit rules |
| Relative moves | `tests/test_moves.py` | **FakeBridge that counts calls** + a temp copy of `tests/fixtures/P6281348.ORF.xmp` (exposure 1.5500002, sigmoid contrast 1.4, temperature coeffs) + `darkroom._commit` patched to a recorder (the `tests/test_darkroom_core.py` pattern) + `wb-em10iii.json` copied under the temp `wb_dir` for Kelvin |
| Educative | `tests/test_explain.py` | **golden** `tests/fixtures/explain_golden.json`; the session set across two calls in one job and one call in a new job |
| Say + group undo | `tests/test_say.py` | same fixture as moves; a FakeBridge whose `darkroom.action` fails on the second call, for `partial` |
| Presets | `tests/test_presets.py`, `tests/test_presets_apply.py` | FakeBridge (`list_styles`, `style.import_file`, `darkroom.apply_style`, `style.apply`); temp `library_root` via temp `$SCOUTER_CONFIG`; `style.extract` exercised for real against the fixture XMP (phase 2's filtered path) |
| Lua | `tests/lua/test_dispatcher.lua`, `tests/test_lua_dispatcher.py` | stub `dt.gui.current_view`, `dt.styles`, `image:apply_style`; asserts `darkroom.apply_style` is registered, applies inside the bounce, and the timeout invariant holds |
| Closures | `tests/test_phase3_closures.py` | `_pin_session_job({})`; `wb.load_table` version 2 refusal; `host_support.PluginInstall` touches only two paths (tmp HOME via `allowed_tree`) |
| Parity / MCP golden / metrics | existing three files | `EXPECTED_VERBS` +9; `SMOKE_ARGS` +9 |
| **Host** | `tests/test_host_phase3.py` | real darktable; §10 |

**Goldens are regenerated deliberately.** Each golden test, on mismatch, prints a unified diff and the one-line regeneration command. It never rewrites the file itself, except on first creation, which then fails the test so a human commits it (the phase 1 MCP-golden pattern).

---

## 18. Rulings (defaults chosen so the build never has to stop and ask)

- **P3-R1. Units are internal everywhere in arguments and fields; display units appear only in `summary`, `explain` and `vocab.list`'s `display` columns.** Reason: `darkroom.set` already takes internal units, and one unit system per argument type is what makes parity possible.
- **P3-R2. No position→value inversion.** Values come from journal → XMP → WB table → declared neutral, and are refused otherwise (§6.1). Pinning an XMP slot is the way to make a row `live`.
- **P3-R3. `darkroom.adjust` pre-commits by default** (one expect-no-change bounce). `trust_sidecar=True` opts out. A read verb (`darkroom.get`) never bounces.
- **P3-R4. A phrase is all-or-nothing on pinning.** A phrase with any unreachable move is refused whole, before any write. A runtime failure after a landed move is `partial` (exit 9), never a silent half-edit.
- **P3-R5. Undo is by move group by default.** A `say` phrase is one group. Every other write is a group of one, so phase 1 and 2 behaviour is unchanged. The ArgSpec is `single: bool = False` (§14.1: argparse `store_true` cannot switch off a `True` default).
- **P3-R6. Educative lines attach to the conversational verbs only:** `darkroom.adjust`, `darkroom.say`, `preset.save`, `preset.apply`. `darkroom.set` and `darkroom.nudge` stay bare scripting verbs, so their results and the whole phase 1/2 suite are untouched.
- **P3-R7. The educative session is the job.** The MCP server pins one job per process (§11.1). CLI users set `$SCOUTER_JOB`.
- **P3-R8. `educative` is a top-level config key, default `brief`**, overridable per call. A broken config falls back to `brief` for explanations only.
- **P3-R9. Preset modules default to this session's edited modules on this image**, minus `never_include`. Empty is a `usage` refusal, never a silent whole-history style.
- **P3-R10. Presets are stored in darktable and in `<library_root>/scouter/memory/presets/`** (the directory phase 2 reserved, inside the backup set). Without `library_root`: darktable plus the styles cache, with a warning. No config is required for the one-picture workflow.
- **P3-R11. Applying to the open image goes through a bounce, with the apply in the lighttable.** That avoids the unverified darkroom-overwrites-append question.
- **P3-R12. Undoing a preset application is refused** (`unsupported_undo`) and marked, like `module.reset`. No global-undo action path is probed in phase 3: probing it risks a wedge for a feature darktable's own history panel already provides.
- **P3-R13. Candidate paths are probed one darktable launch per candidate**, with a 10 s answer budget and a `view.current` liveness check after it. Only pairs that answer non-`nan` and leave the worker alive are pinned.
- **P3-R14. Sign conventions (black level, tint, grade hues) are asserted by rendered stats in the host run**, and flipped in the table if wrong. `sigmoid.skew` gets no phrase in phase 3.
- **P3-R15. Clamps are Scouter's own conservative ranges**, not darktable's limits. Hitting one is reported (`clamped: true`, summary suffix), and a move that would not change the value at the limit is a no-op with zero bridge writes.
- **P3-R16. The MCP golden is updated by each tool-adding task, in that task's commit**, not once at the end. Phase 3's verb tasks are sequential.
- **P3-R17. The build does not block on the host run.** Tasks 1-9 merge-ready with the current pins. Rows stay `candidate`/`absolute_only` and refuse honestly. Task 10 upgrades them when Pratik's machine is free (darktable closed, controller GO), as in phase 2 task 14.
- **P3-R18. No wholesale `~/.config/darktable` snapshot or restore in any host procedure** (R22 closed). Only the plugin file and `luarc` are backed up and restored, by `tests/host_support.PluginInstall`.

## 19. Open questions

None blocking. Every decision above has a default. Two are worth Pratik's eye after the host run: (1) whether the clamp ranges feel right in real use, since they are one table edit each, and (2) which `candidate` rows the probe could not pin, since those decide whether "lift the shadows" works on his darktable build.
