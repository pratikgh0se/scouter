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
    assert verdicts == {"verified": 1740}, verdicts


def test_sweep_runs_every_phrase_without_crashing(cases):
    s = OfflineSession()
    out = collections.Counter(evaluate(c, s.run_case(c), POLICY).score for c in cases
                              if "-gram-" not in c.id)
    assert sum(out.values()) == 979
