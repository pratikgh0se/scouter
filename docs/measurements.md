# Scouter measurements

Written by `tests/test_commit_trigger_host.py` (task 1). Host: this Mac, darktable 5.4.1, Apple M4.

## Commit trigger (phase 1 design 8.1)

- accepted candidates: none
- **strategy adopted for `history.commit(strategy="auto")`: `view_bounce`**
- view after C1: darkroom
- view bounce (C3) cost, wall clock: 510 ms
- view bounce (C3) cost, CPU time (os.clock, not wall clock): 241 ms
- global undo action probe: nan / darkroom undo: nan (phase-2 note only; phase 1 uses the journal, design 8.2)
- XMP mtime advanced during the run: True

## Scouter loop, 20 MP ORF at 1024 px (design 6.1)

- runs, (render+read s, stats s): [(2.068, 0.035), (2.08, 0.034), (1.929, 0.034)]
- best run: render+read: 1.929 s, stats pass: 0.034 s
- **loop total: 1.962 s** (budget 3.0 s)

## Raw probe records

    {"probe": "view_at_start", "value": "darkroom", "ms": 0, "note": null}
    {"probe": "enable_exposure", "value": 1.0, "ms": 0, "note": "iop/exposure"}
    {"probe": "set_exposure_0_9", "value": 0.89999997615814, "ms": 3, "note": "iop/exposure/exposure"}
    {"probe": "c1_selected_images", "value": "nan", "ms": 0, "note": "lib/selected image[s]/write sidecar files"}
    {"probe": "view_after_c1", "value": "darkroom", "ms": 0, "note": null}
    {"probe": "c2_metadata", "value": "nan", "ms": 1, "note": "lib/metadata/write sidecar files"}
    {"probe": "c2_image", "value": "nan", "ms": 0, "note": "lib/image/write sidecar files"}
    {"probe": "undo_global", "value": "nan", "ms": 0, "note": "global/undo"}
    {"probe": "undo_darkroom", "value": "nan", "ms": 39, "note": "views/darkroom/undo"}
    {"probe": "c3_view_bounce_cpu", "value": 1, "ms": 241, "note": "lighttable then darkroom (CPU time via os.clock, not wall clock)"}
    {"probe": "view_after_c3", "value": "darkroom", "ms": 0, "note": null}
    {"probe": "done"}
