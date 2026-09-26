import re
from pathlib import Path

from tests.gauntlet.layers import guide


def test_guide_count():
    cases = guide.generate()
    assert len(cases) == guide.EXPECTED == 35 and len({c.id for c in cases}) == 35


def test_walkthrough_matches_the_readme():
    readme = (Path(__file__).resolve().parents[2] / "README.md").read_text()

    def section(title):
        m = re.search(r"^#+ " + re.escape(title) + r".*?$(.*?)(?=^#+ |\Z)", readme, re.M | re.S)
        return m.group(1) if m else ""

    cmds = []
    for title in ("A hero edit", "A hero session"):
        inb = False
        for line in section(title).splitlines():
            if line.strip().startswith("```"):
                inb = not inb
                continue
            if inb and line.strip().startswith("scouter "):
                cmds.extend(x.strip() for x in line.strip().split("&&"))
    assert len(cmds) == 11, "README walkthrough changed: update guide.WALKTHROUGH"
    listed = [" ".join(p) for _l, k, p, _w in guide.WALKTHROUGH[:11] if k == "cli"]
    for got, want in zip(cmds, listed):
        words = [w for w in want.split() if not w.startswith("{")]
        assert all(w.strip('"') in got for w in words), (got, want)
