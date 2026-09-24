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
