from tests.gauntlet import taxonomy
from tests.gauntlet.layers import install


def test_install_is_10_ordered_cli_cases():
    cases = install.generate(taxonomy.load())
    assert [c.meta["order"] for c in cases] == list(range(10))
    assert {c.mode for c in cases} == {"cli"} and {c.tier for c in cases} == {"P0"}
