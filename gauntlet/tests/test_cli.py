import json

from gauntlet.cli import EXIT_PARKED, main


def _args(tmp_path):
    return ["--adapter", "gauntlet.tests.toy:make", "--runs-root", str(tmp_path)]


def test_generate_run_report_html_end_to_end(tmp_path, capsys):
    assert main(_args(tmp_path) + ["generate", "--stamp", "s1"]) == 0
    assert (tmp_path / "s1" / "cases.jsonl").exists()
    assert main(_args(tmp_path) + ["run", "--stamp", "latest", "--resume"]) == 0
    assert main(_args(tmp_path) + ["report", "--stamp", "s1"]) == 0
    assert main(_args(tmp_path) + ["progress-html", "--stamp", "s1", "--title", "Toy"]) == 0
    text = (tmp_path / "s1" / "progress.html").read_text()
    assert "<title>Toy</title>" in text and "Release bar: MET" in text


def test_config_file_supplies_adapter(tmp_path, monkeypatch):
    cfg = tmp_path / "gauntlet.json"
    cfg.write_text(json.dumps({"adapter": "gauntlet.tests.toy:make",
                               "runs_root": str(tmp_path / "runs")}))
    assert main(["--config", str(cfg), "generate", "--stamp", "s"]) == 0


def test_single_case_rerun(tmp_path):
    main(_args(tmp_path) + ["generate", "--stamp", "s"])
    assert main(_args(tmp_path) + ["run", "--stamp", "s", "--case", "beta-1"]) == 0
    assert (tmp_path / "s" / "cases" / "beta-1.json").exists()
    assert not (tmp_path / "s" / "cases" / "beta-0.json").exists()


def test_parked_exit_code_constant():
    assert EXIT_PARKED == 75


def test_tier_flag(tmp_path):
    main(_args(tmp_path) + ["generate", "--stamp", "s"])
    assert main(_args(tmp_path) + ["run", "--stamp", "s", "--tier", "P0"]) == 0
    done = sorted(p.stem for p in (tmp_path / "s" / "cases").glob("*.json"))
    assert done == ["alpha-0", "beta-0"]


def test_prefix_and_where_filters(tmp_path):
    main(_args(tmp_path) + ["generate", "--stamp", "s"])
    assert main(_args(tmp_path) + ["run", "--stamp", "s", "--prefix", "beta-",
                                   "--where", "sleep_s=5"]) == 0
    done = sorted(p.stem for p in (tmp_path / "s" / "cases").glob("*.json"))
    assert done == ["beta-0", "beta-1"]


def test_rerun_runs_done_cases_again(tmp_path):
    main(_args(tmp_path) + ["generate", "--stamp", "s"])
    main(_args(tmp_path) + ["run", "--stamp", "s"])
    first = (tmp_path / "s" / "cases" / "alpha-0.json").stat().st_mtime_ns
    assert main(_args(tmp_path) + ["run", "--stamp", "s", "--tier", "P0", "--rerun"]) == 0
    assert (tmp_path / "s" / "cases" / "alpha-0.json").stat().st_mtime_ns > first
