"""Zenkai Rules Z4 (docs/ZENKAI.md): `tests/gauntlet/policy.py::load_policy`
refuses an unknown flag name and a malformed override record, and drops an
expired one. Review fix round 2, D-2."""

from __future__ import annotations

import json

import pytest

from gauntlet.evaluate import KNOWN_FLAGS
from tests.gauntlet import policy as policy_module
from tests.gauntlet.policy import UnknownFlagError, _active_flags, load_policy


def test_known_flags_lists_parity_same_failure_is_pass():
    assert KNOWN_FLAGS == frozenset({"parity.same_failure_is_pass"})


def test_active_flags_empty_by_default():
    assert _active_flags([]) == frozenset()


def test_active_flags_rejects_an_unknown_name():
    with pytest.raises(UnknownFlagError, match="not_a_real_flag"):
        _active_flags([{"name": "not_a_real_flag", "reason": "x", "card": "y",
                       "expires": "2099-01-01"}])


def test_active_flags_rejects_a_missing_field():
    with pytest.raises(UnknownFlagError, match="reason"):
        _active_flags([{"name": "parity.same_failure_is_pass", "card": "y",
                       "expires": "2099-01-01"}])


def test_active_flags_accepts_a_valid_unexpired_entry():
    flags = _active_flags([{"name": "parity.same_failure_is_pass", "reason": "row 488 trial",
                           "card": "scouter-row-488", "expires": "2099-01-01"}])
    assert flags == frozenset({"parity.same_failure_is_pass"})


def test_active_flags_drops_an_expired_entry_with_a_warning():
    with pytest.warns(UserWarning, match="expired"):
        flags = _active_flags([{"name": "parity.same_failure_is_pass", "reason": "x",
                               "card": "y", "expires": "2000-01-01"}])
    assert flags == frozenset()


def test_real_check_map_json_loads_with_no_active_flags():
    # The default-off oracle: the shipped check_map.json's "flags" list is
    # empty, so the real loader must produce an empty Policy.flags.
    assert load_policy().flags == frozenset()


def test_load_policy_raises_on_an_unknown_flag_in_a_temp_check_map(tmp_path, monkeypatch):
    cm = json.loads((policy_module.DATA / "check_map.json").read_text(encoding="utf-8"))
    cm["flags"] = [{"name": "bogus.flag", "reason": "x", "card": "y", "expires": "2099-01-01"}]
    tmp_data = tmp_path / "data"
    tmp_data.mkdir()
    (tmp_data / "check_map.json").write_text(json.dumps(cm))
    monkeypatch.setattr(policy_module, "DATA", tmp_data)
    with pytest.raises(UnknownFlagError):
        policy_module.load_policy()
