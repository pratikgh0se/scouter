# Scouter editor benchmark: olympus (phase2-main-canonical)

- run `20260924t112233z`, git `53ddb182c9` on `bench/editor`, 2026-09-24T11:22:33.971122+00:00 to 2026-09-24T11:25:55.157967+00:00
- camera: OM Digital / Olympus E-M10 Mark III (.ORF)
- **17 pass / 3 partial / 7 fail** of 27 (18.5 points); median task 1.39s, median darkroom_set 1.453s
- outcomes: correct_refusal 2, failed 1, honest_refusal 3, partial 3, reported_ok_unverified 3, verified 15

## Sessions

- calib: skipped (_wb_table_present already present)
- A (dark): launch ok=True ready 2.66s; setup: history_commit=TimeoutError: tools/call did not answer within 30.0s; quit osascript; WEDGED after E1
- B (bright): launch ok=True ready 2.18s; setup: history_commit=error:commit_timeout; quit osascript
- C (hero): launch ok=True ready 2.16s; setup: history_commit=error:commit_timeout, library_import=ok; quit osascript
- A (dark): launch ok=True ready 2.12s; setup: history_commit=error:commit_timeout; quit osascript

## Category scores

| category | pass | partial | fail | score |
|---|---|---|---|---|
| exposure | 1 | 1 | 0 | 75.0% |
| white_balance | 3 | 0 | 1 | 75.0% |
| contrast_tone | 3 | 1 | 0 | 87.5% |
| colour | 0 | 1 | 3 | 12.5% |
| lens | 2 | 0 | 0 | 100.0% |
| finishing | 2 | 0 | 0 | 100.0% |
| workflow | 4 | 0 | 3 | 57.1% |
| unreachable | 2 | 0 | 0 | 100.0% |

## Tasks

| id | category | sentence | plan | outcome | score | s | measured |
|---|---|---|---|---|---|---|---|
| E1 | exposure | This one came out dark. Bring it up a full stop. | history_read, darkroom_set | verified | **pass** | 1.974 | delta=1.0; near=1.0 |
| T4 | contrast_tone | The shadows look milky. Crush the blacks a touch. | history_read, darkroom_set | verified | **pass** | 1.201 | delta=0.010044 |
| S2 | finishing | It's ISO 6400 and it shows. Clean up the noise. | darkroom_enable | verified | **pass** | 1.201 |  |
| E2 | exposure | The sky's blown out. Pull the highlights back. | scout_stats, history_read, darkroom_set, scout_stats | partial | **partial** | 5.983 | delta=-0.7; clip_down={"before": 0.0, "after": 0.0} (x) |
| W1 | white_balance | Warm it up a little. | wb_kelvin, darkroom_set, wb_kelvin | verified | **pass** | 1.804 | warmer=0.14658; kelvin=5500 |
| W2 | white_balance | Now it's too warm. Cool it down. | wb_kelvin, darkroom_set, wb_kelvin | verified | **pass** | 1.39 | cooler=-0.22787; kelvin=4558.709398174372 |
| W3 | white_balance | There's a green cast. Push it a touch toward magenta. | darkroom_set | verified | **pass** | 1.599 | magenta=-0.13752 |
| W4 | white_balance | Match daylight. | darkroom_set, wb_kelvin | reported_ok_unverified | **fail** | 1.26 | kelvin=5934.294988009581 (x); honest_null=null (x) |
| T1 | contrast_tone | Give it more punch. Add contrast. | history_read, darkroom_set | verified | **pass** | 1.755 | delta=0.3 |
| T2 | contrast_tone | Soften the midtone contrast a little. | darkroom_enable, history_read, darkroom_set | verified | **pass** | 2.456 | delta=-0.1 |
| T3 | contrast_tone | Skew the tone curve a bit, try 0.2. | darkroom_set | partial | **partial** | 1.622 | readback=null (x); landed=["f1"] |
| C1 | colour | Boost the colour saturation a bit. | darkroom_enable, darkroom_set | partial | **partial** | 7.049 | readback=null (x); landed=["f19"] |
| C2 | colour | Add a little vibrance. | darkroom_set | honest_refusal | **fail** | 0.248 | darkroom_set: usage - no pinned darktable action path for colorbalancergb.global_vibrance |
| C3 | colour | Shift the greens toward yellow. | darkroom_set | honest_refusal | **fail** | 0.228 | darkroom_set: usage - no pinned darktable action path for colorzones.green_hue |
| C4 | colour | Grade it: warm the shadows, cool the highlights. | darkroom_set, darkroom_set, darkroom_set, darkroom_set | honest_refusal | **fail** | 0.233 | darkroom_set: usage - no pinned darktable action path for colorbalancergb.shadows_hue |
| F1 | workflow | Hmm, no. Undo that. | darkroom_undo_last | reported_ok_unverified | **fail** | 0.001 | did_undo=null (x); restored=1.9999998807907104 (x) |
| F2 | workflow | Undo the last two changes. | darkroom_undo_last, darkroom_undo_last | reported_ok_unverified | **fail** | 0.003 | restored_contrast=2.0999999046325684 (x); restored_exposure=1.000000238418579 (x) |
| F3 | workflow | Save this look as a preset called scouter-bench-olympus-0924112233. | style_extract | verified | **pass** | 0.64 | filtered=true |
| F4 | workflow | Apply scouter-bench-olympus-0924112233 to these three: P6281361.ORF, P6281373.ORF, P5240152.ORF. | style_apply_set | verified | **pass** | 0.333 | count=3 |
| F5 | workflow | Now apply it to the rest of the job. | style_apply_set | verified | **pass** | 0.658 |  |
| F6 | workflow | Export a client proof of this one. | export_run | verified | **pass** | 2.867 |  |
| U1 | unreachable | Brighten just her face. Mask it and lift it a third of a stop. | (no call: refuse) | correct_refusal | **pass** | 0.0 |  |
| U2 | unreachable | Remove the person walking through the left of the frame. | (no call: refuse) | correct_refusal | **pass** | 0.0 |  |
| L1 | lens | The corners are dark. Fix the lens vignetting. | darkroom_enable | verified | **pass** | 1.132 |  |
| L2 | lens | And correct the barrel distortion. | darkroom_enable | verified | **pass** | 5.522 |  |
| S1 | finishing | Sharpen it a little for the web. | darkroom_enable | verified | **pass** | 1.632 |  |
| F7 | workflow | Open P6281368.ORF. I want to work on that one next. | darkroom_open, darkroom_describe_history | failed | **fail** | 20.155 | darkroom_open: bridge_timeout - bridge method darkroom.open failed: handler raised: ~/.config/darktable/lua/da |

## Cleanup

- {'style': 'scouter-bench-olympus-0924112233', 'ok': True, 'result': {'deleted': True, 'existed': True, 'name': 'scouter-bench-olympus-0924112233'}}
