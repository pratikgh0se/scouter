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


def generate(tax: Taxonomy) -> list[Case]:
    assert len(tax.measurement["install_rehearsal"]["steps"]) == 8
    return [Case(id=case_id(PREFIX, label), layer=LAYER, tier="P0", mode="cli",
                 steps=[Step("rehearsal", {"action": action}, 120.0, False)],
                 expect=Expect("workflow", [Check("extra", target={"path": "action_ok",
                                                                   "op": "truthy"})]),
                 source={"file": "measurement-and-robustness.json", "step": i + 1},
                 meta={"action": action, "asserts": what, "order": i})
            for i, (label, action, what) in enumerate(ACTIONS)]


EXPECTED = 10
