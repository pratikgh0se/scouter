"""Layer 7: install rehearsal (spec 3.7). The 8 install_rehearsal steps, an
idempotent re-run of `scouter setup`, and the teardown leak check. P0, CLI."""

from __future__ import annotations

from gauntlet.case import Case, Check, Expect, Step, case_id
from tests.gauntlet.taxonomy import Taxonomy

LAYER, PREFIX = "install", "ins"
#: One concrete action per rehearsal step (the JSON's steps are prose).
ACTIONS = (
    ("01_isolated_dirs", "make_dirs", "rehearsal root has home/, config/, cache/"),
    ("02_env_scoped", "check_env", "every subprocess env points inside the root"),
    ("03_first_launch", "first_launch", "darktable --configdir writes darktablerc and library.db"),
    ("04_setup", "setup", "scouter setup writes a valid config (version field present)"),
    ("05_setup_check", "setup_check", "scouter setup --check reports healthy"),
    ("06_mcp_register", "mcp_register", "claude mcp list shows scouter -> python -m scouter.mcp.server"),  # noqa: E501
    ("07_teardown", "teardown", "scratch root deleted"),
    ("08_rerun_on_existing", "rerun_existing", "setup on a pre-existing empty config is idempotent"),  # noqa: E501
    ("09_setup_idempotent", "setup_again", "a second scouter setup changes nothing"),
    ("10_leak_check", "leak_check", "no file newer than the stamp outside the root; mcpServers hash unchanged"),  # noqa: E501
)
#: Fix D (night-1 triage rank 8, INS-config-version-assert): `setup` and
#: `setup_again` run `scouter config init --yes` (row 496 item 7), so
#: `config.json` should carry a version field -- still a disputed
#: assertion (spec 3.7 vs scouter/cli/setup.py's own report-only config
#: item), not a product bug the rehearsal can silently keep failing on. A
#: second check against exec_install.Rehearsal's own
#: `detail.config_has_version` (never folded into `action_ok`) keeps that
#: fact visible without either forcing a pass or a plain fail while it is
#: undecided (needs Pratik). Review round 3, GR-1: the tag KEY is
#: `disputed`, never `needs_review` -- that string already means something
#: else on checkmap.wb_route_a_pinned_edit's own move check
#: (`sign_unverified`), and gauntlet/evaluate.py's new typed needs_review
#: path reads only `disputed`.
_CONFIG_VERSION_ACTIONS = {"setup", "setup_again"}
_CONFIG_VERSION_TAG = {"disputed": "spec 3.7 vs scouter/cli/setup.py's own report-only "
                       "config item (setup writes it via `config init`, not `setup`) -- "
                       "night1-parity-install-guide-triage.md rank 8, needs Pratik"}
#: Review round 3, GR-2: `rerun_existing` (ins-08, "setup on a pre-existing
#: config is idempotent") no longer runs `config init` itself (that would
#: clobber the pre-seeded config and make the case unable to catch a
#: `setup` that overwrites a user's config -- config_has_version would pass
#: trivially either way). Its own check is `detail.config_unchanged`
#: (exec_install.Rehearsal's own before/after byte comparison) instead,
#: UNTAGGED: a real clobber is a genuine regression, never a disputed spec
#: question.
_CONFIG_UNCHANGED_ACTIONS = {"rerun_existing"}


def _checks(action: str) -> list[Check]:
    out = [Check("extra", target={"path": "action_ok", "op": "truthy"})]
    if action in _CONFIG_VERSION_ACTIONS:
        out.append(Check("extra", target={"path": "detail.config_has_version", "op": "truthy"},
                         tags=dict(_CONFIG_VERSION_TAG)))
    elif action in _CONFIG_UNCHANGED_ACTIONS:
        out.append(Check("extra", target={"path": "detail.config_unchanged", "op": "truthy"}))
    return out


def generate(tax: Taxonomy) -> list[Case]:
    assert len(tax.measurement["install_rehearsal"]["steps"]) == 8
    return [Case(id=case_id(PREFIX, label), layer=LAYER, tier="P0", mode="cli",
                 steps=[Step("rehearsal", {"action": action}, 120.0, False)],
                 expect=Expect("workflow", _checks(action)),
                 source={"file": "measurement-and-robustness.json", "step": i + 1},
                 meta={"action": action, "asserts": what, "order": i})
            for i, (label, action, what) in enumerate(ACTIONS)]


EXPECTED = 10
