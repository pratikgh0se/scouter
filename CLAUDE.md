# scouter

## Zenkai Rules

The Zenkai Rules (Capsule Corp reliability doctrine, decision rows 454 and 455) live in
`docs/ZENKAI.md`, a checksummed verbatim mirror of the vault note
`Knowledge/Capsule Corp - Zenkai Rules.md` (`tests/test_zenkai_mirror.py` fails on drift).
Cite them by item; never copy them. Agents may not edit `docs/ZENKAI.md`.

- Every tool, evaluator and gauntlet check: Z1 intent examples and Z2 two oracles (for example
  sidecar + library db, parameter delta + image statistic). A darktable call that reports success
  is not its own oracle.
- Every agent working here: Z3 precedence, Z4 overrides (no skip, xfail or host-test bypass
  without one), Z6 autonomy by reversibility (writing a user's edit history or deleting files is
  irreversible unless one recorded command restores it the same day).
- The gauntlet: Z5 (a canary slice after every merge, t_c9cf359b).
