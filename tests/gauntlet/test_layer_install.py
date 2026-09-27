from tests.gauntlet import taxonomy
from tests.gauntlet.layers import install


def test_install_is_10_ordered_cli_cases():
    cases = install.generate(taxonomy.load())
    assert [c.meta["order"] for c in cases] == list(range(10))
    assert {c.mode for c in cases} == {"cli"} and {c.tier for c in cases} == {"P0"}


# -- Fix D (night-1 triage rank 8, INS-config-version-assert) ---------------


def test_config_version_actions_get_a_second_honest_check():
    cases = install.generate(taxonomy.load())
    by_action = {c.meta["action"]: c for c in cases}
    for action in ("setup", "rerun_existing", "setup_again"):
        checks = by_action[action].expect.checks
        assert len(checks) == 2
        paths = {chk.target["path"] for chk in checks}
        assert paths == {"action_ok", "detail.config_has_version"}
        version_check = next(chk for chk in checks
                             if chk.target["path"] == "detail.config_has_version")
        assert "needs_review" in version_check.tags


def test_setup_check_keeps_its_single_check():
    """setup --check never asserts config content (dossier: 'config is
    missing but not required'), so it must not gain the second check."""
    cases = install.generate(taxonomy.load())
    by_action = {c.meta["action"]: c for c in cases}
    checks = by_action["setup_check"].expect.checks
    assert len(checks) == 1 and checks[0].target["path"] == "action_ok"
