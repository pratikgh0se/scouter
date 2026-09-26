from tests.gauntlet import chart_render


def test_write_chart_pfm_and_tiff(tmp_path):
    pfm = chart_render.write_chart(tmp_path)
    assert pfm.read_bytes().startswith(b"PF\n1536 1152\n")
    tif = chart_render.write_chart(tmp_path, "tiff")
    assert tif.read_bytes()[:4] == b"II*\x00"


def test_golden_compare_and_candidate(tmp_path):
    from gauntlet import chart

    st = chart.patch_stats(chart.build())
    cand = chart_render.write_golden_candidate(tmp_path, st)
    res = chart_render.compare_golden(st, golden=cand)
    assert res["verdict"] == "pass" and res["patch_de"][0] < 1e-6
    assert chart_render.compare_golden(st, golden=tmp_path / "none.json")["verdict"] == "no_golden"
