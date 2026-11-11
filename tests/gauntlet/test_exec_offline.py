import collections

import pytest

from gauntlet.evaluate import evaluate
from tests.gauntlet import taxonomy
from tests.gauntlet.exec_offline import OfflineSession
from tests.gauntlet.layers import offline
from tests.gauntlet.policy import POLICY


@pytest.fixture(scope="module")
def cases():
    return offline.generate(taxonomy.load())


def test_grammar_cases_pass_on_todays_resolver(cases):
    gram = [c for c in cases if "-gram-" in c.id]
    s = OfflineSession()
    verdicts = collections.Counter(evaluate(c, s.run_case(c), POLICY).outcome for c in gram)
    assert verdicts == {"verified": 2235}, verdicts


def test_sweep_runs_every_phrase_without_crashing(cases):
    s = OfflineSession()
    out = collections.Counter(
        evaluate(c, s.run_case(c), POLICY).score for c in cases if "-gram-" not in c.id
    )
    assert sum(out.values()) == 979


def test_tax_and_req_cases_all_pass_on_todays_resolver(cases):
    """Item 3 (2026-09-26 fix pass): every one of the 979 off-tax-*/off-req-*
    cases verifies -- including the 21 off-tax-* cases that resolve to the
    phrase's own synonym key (e.g. off-tax-071 'deeper shadows' filed under
    module `sigmoid`), which pass now that `allowed_keys` includes it.

    off-tax-235 ('make it crisper') verifies too: `SERVED_MODULES` in
    tests/gauntlet/layers/offline.py now includes `sharpen` (row 437 family
    3), so the taxonomy-ownership oracle allows `sharpen_more` on its own --
    the phrase is filed under module `sharpen` in the taxonomy, independent
    of whatever `phrases.resolve` itself does with filler words."""
    s = OfflineSession()
    tax_req = [c for c in cases if c.id.startswith("off-tax-") or c.id.startswith("off-req-")]
    verdicts = collections.Counter(evaluate(c, s.run_case(c), POLICY).outcome for c in tax_req)
    assert verdicts == {"verified": 979}, verdicts
