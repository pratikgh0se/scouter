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


# -- row 501 item H (fix round 1): --subset wiring end to end -------------

def test_generate_subset_keeps_only_the_named_layer_reduced_and_records_it(tmp_path):
    """(row 501 item H): --subset/--subset-layer/--subset-keys reduce ONLY
    the named layer's cases in cases.jsonl; every other layer is
    untouched; the subset's own record is written for Progress to fold
    into progress.json."""
    assert main(_args(tmp_path) + ["generate", "--stamp", "s1", "--subset", "2",
                                   "--subset-layer", "alpha", "--subset-keys", "sleep_s"]) == 0
    run_dir = tmp_path / "s1"
    cases = [json.loads(line) for line in
             (run_dir / "cases.jsonl").read_text().splitlines() if line.strip()]
    by_layer = {}
    for c in cases:
        by_layer.setdefault(c["layer"], 0)
        by_layer[c["layer"]] += 1
    assert by_layer["alpha"] == 2  # reduced from the toy adapter's default n=4
    assert by_layer["beta"] == 2  # untouched
    subset_doc = json.loads((run_dir / "subset.json").read_text())
    assert subset_doc == {"layer": "alpha", "size": 2, "of": 4, "seed": 0, "keys": ["sleep_s"]}
    assert "Subset:" in (run_dir / "generation.md").read_text()


def test_subset_flags_must_be_given_together(tmp_path):
    rc = main(_args(tmp_path) + ["generate", "--stamp", "s1", "--subset", "2"])
    assert rc == 2


def test_progress_folds_in_the_subset_record(tmp_path):
    """(row 501 item H): the ruling's own "recorded in progress.json"."""
    main(_args(tmp_path) + ["generate", "--stamp", "s1", "--subset", "2",
                            "--subset-layer", "alpha", "--subset-keys", "sleep_s"])
    assert main(_args(tmp_path) + ["run", "--stamp", "s1", "--resume"]) == 0
    doc = json.loads((tmp_path / "s1" / "progress.json").read_text())
    assert doc["subset"] == {"layer": "alpha", "size": 2, "of": 4, "seed": 0,
                             "keys": ["sleep_s"]}
