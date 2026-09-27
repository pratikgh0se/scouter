"""Zenkai Rules Z4 (docs/ZENKAI.md): `tests/gauntlet/policy.py::load_policy`
refuses an unknown flag name and a malformed override record, and drops an
expired one. Review fix round 2, D-2.

Imports the two production modules BARE (`import ... as ...`), never
`from module import NEW_NAME`: `KNOWN_FLAGS`, `UnknownFlagError` and
`_active_flags` are all new names this same review round added, and a
`from` import of a name that does not exist fails at MODULE COLLECTION
time (breaking every other test in the file, and confusing the merge
gate's own revert-mutation check, which reverts production hunks one
range at a time). A bare module import always succeeds; the attribute
lookup happens inside each test body, where a missing name is an ordinary,
single-test `AttributeError` -- exactly the discriminating failure the
gate wants when it reverts the production change."""

from __future__ import annotations

import json

import pytest

from gauntlet import evaluate as evaluate_module
from tests.gauntlet import policy as policy_module


def test_known_flags_lists_parity_same_failure_is_pass():
    assert evaluate_module.KNOWN_FLAGS == frozenset({"parity.same_failure_is_pass"})


def test_active_flags_empty_by_default():
    assert policy_module._active_flags([]) == frozenset()


def test_active_flags_rejects_an_unknown_name():
    with pytest.raises(ValueError, match="not_a_real_flag") as exc_info:
        policy_module._active_flags([{"name": "not_a_real_flag", "reason": "x", "card": "y",
                                     "expires": "2099-01-01"}])
    assert isinstance(exc_info.value, policy_module.UnknownFlagError)
    assert "not_a_real_flag" in str(exc_info.value)


def test_active_flags_rejects_a_missing_field():
    with pytest.raises(ValueError, match="reason") as exc_info:
        policy_module._active_flags([{"name": "parity.same_failure_is_pass", "card": "y",
                                     "expires": "2099-01-01"}])
    assert isinstance(exc_info.value, policy_module.UnknownFlagError)
    assert "reason" in str(exc_info.value)


def test_active_flags_accepts_a_valid_unexpired_entry():
    flags = policy_module._active_flags(
        [{"name": "parity.same_failure_is_pass", "reason": "row 488 trial",
         "card": "scouter-row-488", "expires": "2099-01-01"}])
    assert flags == frozenset({"parity.same_failure_is_pass"})


def test_active_flags_drops_an_expired_entry_with_a_warning():
    with pytest.warns(UserWarning, match="expired"):
        flags = policy_module._active_flags(
            [{"name": "parity.same_failure_is_pass", "reason": "x", "card": "y",
             "expires": "2000-01-01"}])
    assert flags == frozenset()


def test_real_check_map_json_loads_with_no_active_flags():
    # The default-off oracle: the shipped check_map.json's "flags" list is
    # empty, so the real loader must produce an empty Policy.flags.
    assert policy_module.load_policy().flags == frozenset()


def test_load_policy_raises_on_an_unknown_flag_in_a_temp_check_map(tmp_path, monkeypatch):
    cm = json.loads((policy_module.DATA / "check_map.json").read_text(encoding="utf-8"))
    cm["flags"] = [{"name": "bogus.flag", "reason": "x", "card": "y", "expires": "2099-01-01"}]
    tmp_data = tmp_path / "data"
    tmp_data.mkdir()
    (tmp_data / "check_map.json").write_text(json.dumps(cm))
    monkeypatch.setattr(policy_module, "DATA", tmp_data)
    with pytest.raises(ValueError) as exc_info:
        policy_module.load_policy()
    assert isinstance(exc_info.value, policy_module.UnknownFlagError)
    assert "bogus.flag" in str(exc_info.value)
