from gauntlet.adapter import CallRecord, Observation
from gauntlet.evaluate import Policy, evaluate
from tests.gauntlet import taxonomy
from tests.gauntlet.layers import install

POLICY = Policy(
    refusal_codes=frozenset(), transient_codes=frozenset(), refusal_markers=(), claim_markers=()
)


def test_install_is_10_ordered_cli_cases():
    cases = install.generate(taxonomy.load())
    assert [c.meta["order"] for c in cases] == list(range(10))
    assert {c.mode for c in cases} == {"cli"} and {c.tier for c in cases} == {"P0"}


# -- Fix D (night-1 triage rank 8, INS-config-version-assert) ---------------


def test_config_version_actions_get_a_second_honest_check():
    cases = install.generate(taxonomy.load())
    by_action = {c.meta["action"]: c for c in cases}
    for action in ("setup",):
        checks = by_action[action].expect.checks
        assert len(checks) == 2
        paths = {chk.target["path"] for chk in checks}
        assert paths == {"action_ok", "detail.config_has_version"}
        version_check = next(
            chk for chk in checks if chk.target["path"] == "detail.config_has_version"
        )
        # GR-1: the tag key is `disputed`, never `needs_review` (that key
        # already means something else on checkmap's own Route-A move check).
        assert "disputed" in version_check.tags
        assert "needs_review" not in version_check.tags


def test_rerun_existing_checks_config_unchanged_not_has_version():
    """GR-2: ins-08 no longer runs `config init` itself (that would clobber
    the pre-seeded config and make it unable to catch a `setup` that
    overwrites a user's config); its own check is the untagged, hard
    `detail.config_unchanged` -- a real clobber is a genuine regression."""
    cases = install.generate(taxonomy.load())
    by_action = {c.meta["action"]: c for c in cases}
    checks = by_action["rerun_existing"].expect.checks
    assert len(checks) == 2
    paths = {chk.target["path"] for chk in checks}
    assert paths == {"action_ok", "detail.config_unchanged"}
    unchanged_check = next(chk for chk in checks if chk.target["path"] == "detail.config_unchanged")
    assert unchanged_check.tags == {}


def test_setup_check_keeps_its_single_check():
    """setup --check never asserts config content (dossier: 'config is
    missing but not required'), so it must not gain the second check."""
    cases = install.generate(taxonomy.load())
    by_action = {c.meta["action"]: c for c in cases}
    checks = by_action["setup_check"].expect.checks
    assert len(checks) == 1 and checks[0].target["path"] == "action_ok"


# -- Review round 4 (GR3-1, GR3-2) ------------------------------------------


def _by_action():
    return {c.meta["action"]: c for c in install.generate(taxonomy.load())}


def test_setup_again_has_no_config_version_check():
    """GR3-1: ins-09 runs after 07 teardown (rmtree) and 08's `{}` seed and
    never runs `config init`, so a config_has_version check there could
    never pass. Its idempotency is covered by action_ok (tree_unchanged)."""
    checks = _by_action()["setup_again"].expect.checks
    paths = [chk.target["path"] for chk in checks]
    assert paths == ["action_ok"]
    assert "detail.config_has_version" not in paths


def test_only_setup_keeps_the_disputed_version_check():
    carriers = sorted(
        a
        for a, c in _by_action().items()
        if any(chk.target["path"] == "detail.config_has_version" for chk in c.expect.checks)
    )
    assert carriers == ["setup"]


def test_every_install_case_needs_all_checks_for_partial():
    assert {c.meta.get("partial_min") for c in _by_action().values()} == {1.0}


def _rerun_obs(*, action_ok, config_unchanged):
    return Observation(
        calls=[
            CallRecord(
                "rehearsal",
                {"action": "rerun_existing"},
                {"ok": action_ok, "outcome": "ok", "data": {}},
            )
        ],
        extra={"action_ok": action_ok, "detail": {"config_unchanged": config_unchanged}},
    )


def test_a_caught_config_clobber_scores_failed_not_partial():
    """GR3-2: rerun_existing with setup exiting 0 but the pre-seeded config
    overwritten is a genuine regression -- `failed`, never half credit."""
    case = _by_action()["rerun_existing"]
    v = evaluate(case, _rerun_obs(action_ok=True, config_unchanged=False), POLICY)
    assert (v.score, v.outcome) == ("fail", "failed")


def test_rerun_existing_with_config_untouched_is_verified():
    case = _by_action()["rerun_existing"]
    v = evaluate(case, _rerun_obs(action_ok=True, config_unchanged=True), POLICY)
    assert (v.score, v.outcome) == ("pass", "verified")
