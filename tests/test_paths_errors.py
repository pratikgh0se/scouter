import json
import os
import tempfile
import unittest
from pathlib import Path

from scouter import errors, paths
from scouter.types import Envelope


class PathsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._env = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)

    def test_scouter_cache_env_wins(self) -> None:
        os.environ["SCOUTER_CACHE"] = "/tmp/x"
        self.assertEqual(paths.cache_root(), Path("/tmp/x"))
        self.assertEqual(paths.events_path(), Path("/tmp/x/events.jsonl"))
        self.assertEqual(paths.audit_path("j1"), Path("/tmp/x/audit/j1.jsonl"))
        self.assertEqual(paths.journal_path("j1"), Path("/tmp/x/journal/j1.jsonl"))

    def test_xdg_cache_home_is_second(self) -> None:
        os.environ.pop("SCOUTER_CACHE", None)
        os.environ["XDG_CACHE_HOME"] = "/tmp/xdg"
        self.assertEqual(paths.cache_root(), Path("/tmp/xdg/scouter"))

    def test_job_id_from_env_or_clock(self) -> None:
        os.environ["SCOUTER_JOB"] = "job-fixed"
        self.assertEqual(paths.current_job(), "job-fixed")
        os.environ.pop("SCOUTER_JOB")
        self.assertRegex(paths.current_job(), r"^job-\d{8}-\d{6}-\d+$")

    def test_ensure_creates_parent_dir_and_returns_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested" / "deeper" / "file.jsonl"
            self.assertFalse(target.parent.exists())
            result = paths.ensure(target)
            self.assertEqual(result, target)
            self.assertTrue(target.parent.is_dir())
            self.assertFalse(target.exists())  # ensure() makes the dir, not the file

    def test_ensure_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "a" / "b.jsonl"
            paths.ensure(target)
            paths.ensure(target)  # must not raise on an already-existing dir
            self.assertTrue(target.parent.is_dir())


class JobStateTest(unittest.TestCase):
    """Task 12 fix round 1, should-fix 5: `job_state_read`/`job_state_update`
    is the ONE locked, merge-on-write seam both `darkroom.py`
    (`opened_image_id`/`opened_path`) and `history.py` (`watermarks`) go
    through, replacing each module's own private unlocked pair."""

    def setUp(self) -> None:
        self._env = dict(os.environ)
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["SCOUTER_CACHE"] = self._tmp.name

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)
        self._tmp.cleanup()

    def test_read_of_a_missing_file_is_an_empty_dict(self) -> None:
        self.assertEqual(paths.job_state_read("job-x"), {})

    def test_update_merges_rather_than_overwrites(self) -> None:
        paths.job_state_update("job-x", lambda s: s.update({"opened_image_id": 12}))
        paths.job_state_update("job-x", lambda s: s.update({"watermarks": {"12": {"size": 1}}}))
        state = paths.job_state_read("job-x")
        self.assertEqual(state["opened_image_id"], 12)
        self.assertEqual(state["watermarks"], {"12": {"size": 1}})

    def test_update_returns_the_merged_state(self) -> None:
        result = paths.job_state_update("job-x", lambda s: s.update({"a": 1}))
        self.assertEqual(result, {"a": 1})

    def test_concurrent_writers_never_lose_an_update(self) -> None:
        """Losing `opened_image_id` to a race would silently disable ruling
        30's guard -- this races many threads, each incrementing their own
        counter key, and asserts every single increment from every thread
        survived (the lock spans the whole read+merge+write, not just the
        write)."""
        import threading

        job = "job-concurrent"
        n_threads = 8
        n_writes_each = 25

        def worker(thread_id: int) -> None:
            key = f"t{thread_id}"
            for _ in range(n_writes_each):

                def _mutate(state: dict, key: str = key) -> None:
                    counters = state.setdefault("counters", {})
                    counters[key] = counters.get(key, 0) + 1

                paths.job_state_update(job, _mutate)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        counters = paths.job_state_read(job).get("counters", {})
        self.assertEqual(len(counters), n_threads, "every thread's key must survive")
        for t in range(n_threads):
            self.assertEqual(
                counters.get(f"t{t}"), n_writes_each, f"thread {t} lost at least one update"
            )


class JobStateMultiProcessLockTest(unittest.TestCase):
    """Carried finding 7 (task 12): the same no-lost-update property, across
    PROCESSES.

    `test_concurrent_writers_never_lose_an_update` races threads, which is
    the weaker test: threads share one process, so an implementation that
    guarded the read-modify-write with a `threading.Lock` (or that simply
    got lucky on the GIL) would pass it while a second Scouter process --
    `darkroom.set` in one shell, `scouter metrics summary` in another, the
    launchd job alongside either -- still clobbered its update. `flock` is
    per-open-file-description, so only separate processes actually exercise
    it.
    """

    def setUp(self) -> None:
        self._env = dict(os.environ)
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["SCOUTER_CACHE"] = self._tmp.name

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)
        self._tmp.cleanup()

    def test_concurrent_processes_never_lose_an_update(self) -> None:
        import subprocess
        import sys

        job = "job-multiproc"
        n_procs = 6
        n_writes_each = 20
        repo_root = Path(paths.__file__).resolve().parent.parent
        script = (
            "import os, sys, time\n"
            "sys.path.insert(0, sys.argv[3])\n"
            "from scouter import paths\n"
            "key, n = sys.argv[1], int(sys.argv[2])\n"
            "def mutate(state):\n"
            "    counters = state.setdefault('counters', {})\n"
            "    counters[key] = counters.get(key, 0) + 1\n"
            "for _ in range(n):\n"
            f"    paths.job_state_update({job!r}, mutate)\n"
            "    time.sleep(0.001)\n"
        )
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", script, f"p{i}", str(n_writes_each), str(repo_root)],
                env={**os.environ, "SCOUTER_CACHE": self._tmp.name},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for i in range(n_procs)
        ]
        for proc in procs:
            out, err = proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0, err.decode())

        counters = paths.job_state_read(job).get("counters", {})
        self.assertEqual(len(counters), n_procs, f"a whole process's key vanished: {counters}")
        for i in range(n_procs):
            self.assertEqual(
                counters.get(f"p{i}"),
                n_writes_each,
                f"process p{i} lost at least one update: {counters}",
            )


class JobValidationTest(unittest.TestCase):
    """B3: a job id becomes a file name, so traversal must be impossible."""

    def setUp(self) -> None:
        self._env = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)

    def test_a_traversal_job_is_a_usage_error_everywhere_it_enters(self) -> None:
        bad = "../../x"
        with self.assertRaises(errors.UsageError) as ctx:
            paths.validate_job(bad)
        self.assertEqual(ctx.exception.code, "usage")
        self.assertEqual(errors.exit_code_for("error:usage"), 2)
        for factory in (paths.audit_path, paths.journal_path, paths.job_state_path):
            with self.subTest(factory=factory.__name__):
                with self.assertRaises(errors.UsageError):
                    factory(bad)

    def test_the_ambient_job_from_the_environment_is_validated(self) -> None:
        os.environ["SCOUTER_JOB"] = "../../x"
        with self.assertRaises(errors.UsageError):
            paths.current_job()

    def test_registry_call_refuses_a_traversal_job_before_running_the_verb(self) -> None:
        from scouter import registry

        with self.assertRaises(errors.UsageError):
            registry.call("history.read", "cli", image="/nope.ORF", job="../../x")

    def test_the_cli_refuses_a_traversal_job_with_exit_code_2(self) -> None:
        import contextlib
        import io
        import json as _json

        from scouter.cli.main import main

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["--job", "../../x", "history", "read", "/nope.ORF"])
        self.assertEqual(code, 2)
        payload = _json.loads(out.getvalue())
        self.assertEqual(payload["error"]["code"], "usage")

    def test_the_mcp_adapter_refuses_a_traversal_job(self) -> None:
        import json as _json

        from scouter.mcp.server import ScouterServer

        payload = _json.loads(
            ScouterServer().call("history_read", {"image": "/nope.ORF", "job": "../../x"})
        )
        self.assertEqual(payload["outcome"], "error:usage")

    def test_good_job_ids_are_accepted_unchanged(self) -> None:
        for job in ("job-test", "j", "job-20260914-120000-123", "a" * 64, "A_b.c-1"):
            with self.subTest(job=job):
                self.assertEqual(paths.validate_job(job), job)

    def test_bad_job_ids_are_all_refused(self) -> None:
        for job in ("", ".hidden", "a/b", "a" * 65, "with space", "-leading", "x\ny"):
            with self.subTest(job=job):
                with self.assertRaises(errors.UsageError):
                    paths.validate_job(job)


class ErrorsTest(unittest.TestCase):
    def test_every_spec_error_class_exists_with_a_unique_code(self) -> None:
        expected = {
            "bridge_timeout",
            "wrong_view",
            "not_in_darkroom",
            "cli_wrote_nothing",
            "db_locked",
            "style_partial",
            "commit_timeout",
            "backup_not_configured",
            "checksum_mismatch",
            "destination_collision",
            "exif_unreadable",
            "style_write_failed",
            "encryption_unavailable",
            "manifest_unauthenticated",
            "export_all_failed",
        }
        codes = {cls.code for cls in errors.ERROR_CLASSES}
        self.assertEqual(codes, expected)
        self.assertEqual(len(codes), len(errors.ERROR_CLASSES))

    def test_every_code_has_an_exit_code_and_a_fix(self) -> None:
        for cls in errors.ERROR_CLASSES:
            self.assertIn(cls.code, errors.EXIT_CODES, cls.__name__)
            self.assertTrue(cls.fix, f"{cls.__name__} has no fix sentence")

    def test_exit_code_for(self) -> None:
        self.assertEqual(errors.exit_code_for("ok"), 0)
        self.assertEqual(errors.exit_code_for("dry_run"), 5)
        self.assertEqual(errors.exit_code_for("error:wrong_view"), 4)
        self.assertEqual(errors.exit_code_for("error:nonsense"), 1)

    def test_to_dict_carries_code_message_fix(self) -> None:
        d = errors.WrongView("not in darkroom", detail={"view": "lighttable"}).to_dict()
        self.assertEqual(d["code"], "wrong_view")
        self.assertEqual(d["outcome"], "error")
        self.assertEqual(d["detail"], {"view": "lighttable"})
        self.assertIn("darkroom open", d["fix"])


class EnvelopeTest(unittest.TestCase):
    def test_to_dict_omits_error_key_when_none(self) -> None:
        env = Envelope(
            verb="darkroom.open",
            layer="cli",
            job="job-1",
            ok=True,
            outcome="ok",
            ms=12,
            data={"opened": True},
        )
        d = env.to_dict()
        self.assertNotIn("error", d)
        self.assertEqual(d["verb"], "darkroom.open")
        self.assertEqual(d["ok"], True)
        # round-trips through JSON (what the CLI actually prints / the MCP
        # adapter actually returns)
        self.assertEqual(json.loads(json.dumps(d)), d)

    def test_to_dict_includes_error_key_when_set(self) -> None:
        err = errors.WrongView("not in darkroom").to_dict()
        env = Envelope(
            verb="darkroom.set",
            layer="mcp",
            job="job-2",
            ok=False,
            outcome="error:wrong_view",
            ms=3,
            data={},
            error=err,
        )
        d = env.to_dict()
        self.assertIn("error", d)
        self.assertEqual(d["error"]["code"], "wrong_view")
        self.assertEqual(json.loads(json.dumps(d)), d)

    def test_to_dict_cleans_nan_and_inf_in_data(self) -> None:
        env = Envelope(
            verb="darkroom.status",
            layer="python",
            job="job-3",
            ok=True,
            outcome="ok",
            ms=1,
            data={"positions": {"x": float("nan"), "y": float("inf")}},
        )
        d = env.to_dict()
        self.assertIsNone(d["data"]["positions"]["x"])
        self.assertIsNone(d["data"]["positions"]["y"])
        # a plain json.dumps would raise ValueError on nan/inf by default if not cleaned
        json.dumps(d)


class ConftestAllowTreeTest(unittest.TestCase):
    """`tests/conftest.py::allow_tree` is the ONE opt-in that lets a test
    pin its own throwaway tree past the session isolation fixture. It must
    never be usable to point a test at a real user path -- that is the rule
    the fixture exists to enforce (a suite run once wrote a 1.7 MB test note
    into the real Obsidian vault)."""

    def test_it_accepts_a_temp_directory_and_returns_it(self) -> None:
        from tests.conftest import allow_tree

        with tempfile.TemporaryDirectory(prefix="scouter-allow-") as d:
            self.assertEqual(allow_tree(d), d)

    def test_it_refuses_the_real_vault_config_cache_and_home(self) -> None:
        from tests.conftest import allow_tree

        for candidate in (
            "~/second-brain",
            "~/second-brain/Projects",
            "~/.config/scouter",
            "~/.cache/scouter",
            "~/.hermes",
            "~",
        ):
            with self.subTest(path=candidate):
                with self.assertRaises(ValueError):
                    allow_tree(os.path.expanduser(candidate))
