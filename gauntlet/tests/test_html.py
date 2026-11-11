from gauntlet import html as page


def test_html_escapes_and_handles_empty_progress():
    out = page.render({"stamp": "<x>", "layers": {"a<b": {"total": 0}}}, None, "T&T")
    assert "&lt;x&gt;" in out and "T&amp;T" in out and "a&lt;b" in out
    assert "prefers-color-scheme:dark" in out


def test_bars_signoff_and_parked_line():
    prog = {"stamp": "s", "status": "parked", "parked": ["a", "b"],
            "layers": {"coverage": {"total": 4, "done": 4, "pass": 2, "partial": 1, "fail": 1,
                                    "p0_pass": 1, "p0_total": 1, "silent": 1}}}
    rep = {"S8_signoff": {"release_bar_met": False},
           "S3_non_pass": [{"id": "c1", "outcome": "silent_failure", "repro": "python -m x"}]}
    out = page.render(prog, rep, "Scouter Gauntlet")
    assert "Release bar: NOT MET" in out and "Parked (waiting on a usage limit): 2" in out
    assert 'style="width:50.00%"' in out and "<code>c1</code>" in out
