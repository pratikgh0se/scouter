# Changelog

Scouter has no prior changelog; this file starts with the `fix/e12-findings`
branch, which fixes defects found by the E12 live evaluation against real
darktable (`Knowledge/Scouter - E12 live evaluation 2026-09-24.md` in the
vault; decision row 407).

## Unreleased (fix/e12-findings)

- CLI/README drift (D2): `cull propose`, `outliers list` and `export release`
  now accept `--pretty` after the group/verb, the same as the README's phase
  2 examples show, in addition to `scouter --pretty <group> <verb>`.
  `style apply-set --name ..`, `export release --job-slug ..` and
  `export verify --manifest ..` now work as options as well as the existing
  positional form. A new test (`tests/test_readme_cli_parity.py`) parses
  every `scouter ...` line shown in README.md against the real CLI parser.
- Cull floor made relative per job (D1, row 407 call 2): `cull propose`'s
  singleton sharpness gate is now a percentile of the job's own sharpness
  distribution (`cull.sharpness_keep_top_pct`, default keeps the top 60%),
  not the fixed `cull.sharpness_floor` (0.20) real raws never cleared in the
  E12 live run (measured 0.003-0.04). `sharpness_floor` is kept only as a
  diagnostic field. `cull propose` now renders every frame from a scratch
  copy with no adjacent `.xmp`, so it scores the frame as imported rather
  than its current edited history (editing a hero no longer moves its
  burst's pick on the next proposal).
