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


class LockTimeoutTest(unittest.TestCase):
    """E12 D3, row 407: `state/<job>.json.lock`'s exclusive `flock` used to
    block forever -- the E12 live run stalled 15-18 minutes on it, twice.
    `job_state_read`/`job_state_update` now bound the wait (`cache.lock_wait_s`,
    default 30s, overridable per call via `wait_s` for tests) and fail fast
    with `state_locked`, naming who holds it."""

    def setUp(self) -> None:
        self._env = dict(os.environ)
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["SCOUTER_CACHE"] = self._tmp.name

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)
        self._tmp.cleanup()

    def _hold_the_lock(self, job: str, pid: int, since: str):
        """Simulate another process already holding this job's lock,
        exactly like `_locked_job_state` itself would: an exclusive `flock`
        on the sibling `.lock` file, with a holder record written into it.
        Returns the open file handle -- the caller must keep it open (and
        close it to release) for the lock to mean anything."""
        import fcntl

        lock_path = paths.ensure(paths._job_state_lock_path(job))
        fh = open(lock_path, "a+")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        fh.seek(0)
        fh.truncate()
        fh.write(json.dumps({"pid": pid, "since": since}))
        fh.flush()
        return fh

    def test_a_held_lock_fails_fast_with_the_holder_named(self) -> None:
        import time

        holder_fh = self._hold_the_lock("job-locked", pid=99999, since="2026-09-24T03:00:00+00:00")
        try:
            started = time.monotonic()
            with self.assertRaises(errors.StateLocked) as ctx:
                paths.job_state_read("job-locked", wait_s=0.3)
            elapsed = time.monotonic() - started
        finally:
            holder_fh.close()

        # Bounded: nowhere near the old unbounded block, and not much more
        # than the wait_s asked for.
        self.assertLess(elapsed, 2.0, "must fail fast, not block for minutes")
        self.assertEqual(ctx.exception.code, "state_locked")
        self.assertEqual(errors.exit_code_for("error:state_locked"), 22)
        self.assertIn("99999", str(ctx.exception))
        self.assertIn("2026-09-24T03:00:00", str(ctx.exception))
        self.assertEqual(ctx.exception.detail["pid"], 99999)
        self.assertEqual(ctx.exception.detail["job"], "job-locked")

    def test_a_lock_released_before_the_deadline_succeeds(self) -> None:
        import threading
        import time

        job = "job-released-in-time"
        holder_fh = self._hold_the_lock(job, pid=os.getpid(), since="now")

        def release_soon() -> None:
            time.sleep(0.1)
            holder_fh.close()

        threading.Thread(target=release_soon).start()
        # Long enough to outlast the 0.1s release, short enough the test
        # doesn't hang if this regresses.
        result = paths.job_state_update(job, lambda s: s.update({"ok": True}), wait_s=3.0)
        self.assertEqual(result["ok"], True)

    def test_default_wait_budget_comes_from_config(self) -> None:
        self.assertEqual(paths.DEFAULT_LOCK_WAIT_S, 30.0)
        from scouter.config import Config

        self.assertEqual(Config().cache.lock_wait_s, 30.0)


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
            "value_unknown",
            "not_pinned",
            "state_locked",
            "darktable_unresponsive",
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

    def test_allowed_tree_releases_the_entry_on_exit(self) -> None:
        """Fix round 1, minor d: a permanently registered prefix keeps
        widening the isolation guard for every test that runs afterwards, so
        a class-scoped registration needs a class-scoped lifetime."""
        from tests import conftest

        with tempfile.TemporaryDirectory(prefix="scouter-allow-ctx-") as d:
            before = list(conftest._ALLOWED_TREES)
            with conftest.allowed_tree(d) as prefix:
                self.assertEqual(prefix, d)
                self.assertTrue(conftest._is_allowed(os.path.join(d, "nested", "x.json")))
            self.assertEqual(conftest._ALLOWED_TREES, before, "the entry must be released")
            self.assertFalse(conftest._is_allowed(os.path.join(d, "nested", "x.json")))

    def test_allowed_tree_releases_even_when_the_body_raises(self) -> None:
        from tests import conftest

        with tempfile.TemporaryDirectory(prefix="scouter-allow-raise-") as d:
            before = list(conftest._ALLOWED_TREES)
            with self.assertRaises(RuntimeError):
                with conftest.allowed_tree(d):
                    raise RuntimeError("boom")
            self.assertEqual(conftest._ALLOWED_TREES, before)

    def test_it_refuses_the_bridge_spool_and_the_installed_plugin(self) -> None:
        """Blocker 4's paths are forbidden too: `allow_tree` must not be
        usable to point a test at the real spool or the real plugin."""
        from tests import conftest

        for candidate in (
            conftest.REAL_BRIDGE_SPOOL,
            conftest.REAL_PLUGIN_PATH,
            os.path.dirname(os.path.dirname(conftest.REAL_PLUGIN_PATH)),
        ):
            with self.subTest(path=candidate):
                with self.assertRaises(ValueError):
                    conftest.allow_tree(candidate)

    def test_the_forbidden_list_uses_the_vault_from_the_environment(self) -> None:
        """Derived from `$OBSIDIAN_VAULT_PATH` as it was BEFORE the pin, not
        hard-coded `~/second-brain` (fix round 1, minor d): a user whose vault
        lived elsewhere would have had the one path that caused the original
        incident left unguarded."""
        from tests import conftest

        forbidden = conftest._real_forbidden()
        self.assertIn(os.path.realpath(os.path.expanduser("~/.config/scouter")), forbidden)
        if conftest._REAL_VAULT_FROM_ENV:
            self.assertIn(os.path.realpath(conftest._REAL_VAULT_FROM_ENV), forbidden)

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


class HostClassTreeReleaseTest(unittest.TestCase):
    """Fix round 2, item 3: `Phase2EndToEndTest` must release its
    `allowed_tree` registrations however `setUpClass` ends -- including a
    SKIP, which is not an error and so is the easy case to forget.

    Non-host: this drives `_close_trees` and the `ExitStack` discipline
    directly, without launching darktable. What it cannot do is run the real
    `setUpClass` (that needs a host), so it asserts the two things that make
    the failure path correct: the helper is idempotent, and `setUpClass`'s
    own handler calls it.
    """

    def test_close_trees_releases_registrations_and_is_idempotent(self) -> None:
        import contextlib

        from tests import conftest
        from tests.test_host_phase2 import Phase2EndToEndTest

        cls = Phase2EndToEndTest
        saved = cls._trees
        try:
            with tempfile.TemporaryDirectory(prefix="scouter-trees-") as d:
                before = list(conftest._ALLOWED_TREES)
                cls._trees = contextlib.ExitStack()
                cls._trees.enter_context(conftest.allowed_tree(d))
                self.assertTrue(conftest._is_allowed(os.path.join(d, "x")))

                cls._close_trees()
                self.assertEqual(conftest._ALLOWED_TREES, before)
                self.assertFalse(conftest._is_allowed(os.path.join(d, "x")))
                self.assertIsNone(cls._trees)

                # Called again from tearDownClass after the failure path
                # already ran: must not raise.
                cls._close_trees()
        finally:
            cls._trees = saved

    def test_set_up_class_failure_path_closes_the_trees(self) -> None:
        """A source check, because the real `setUpClass` needs a host: the
        `except BaseException` handler must call `_close_trees`, and
        `tearDownClass` must too."""
        import inspect

        from tests import test_host_phase2

        source = inspect.getsource(test_host_phase2.Phase2EndToEndTest)
        setup = source.split("def setUpClass", 1)[1].split("def _close_trees", 1)[0]
        handler = setup.split("except BaseException:", 1)[1]
        self.assertIn(
            "_close_trees()",
            handler,
            "setUpClass's failure/skip path must release the allowed_tree "
            "registrations before re-raising",
        )
        teardown = source.split("def tearDownClass", 1)[1]
        self.assertIn("_close_trees()", teardown)


class IsAllowedResolvesBothSidesTest(unittest.TestCase):
    """Fix round 2, minor m3: `_is_allowed` must resolve BOTH sides.

    It resolved only `value`, so on macOS -- where `/var` is a symlink to
    `/private/var` and `tempfile.mkdtemp()` hands back a `/var/...` path --
    the session root compared unequal to its own resolved form. Only nested
    paths matched (that arm did resolve its side), so a test pinning an env
    var at the session root exactly would have been re-pinned out from under
    it.
    """

    def test_the_session_root_itself_is_allowed(self) -> None:
        from tests import conftest

        self.assertTrue(
            conftest._is_allowed(conftest._SESSION_TMP),
            "the session tmp root must be allowed as itself",
        )
        self.assertTrue(conftest._is_allowed(os.path.realpath(conftest._SESSION_TMP)))
        self.assertTrue(
            conftest._is_allowed(os.path.join(conftest._SESSION_TMP, "nested", "x.json")),
            "and so must anything under it",
        )

    def test_a_registered_tree_is_allowed_as_itself_too(self) -> None:
        from tests import conftest

        with tempfile.TemporaryDirectory(prefix="scouter-selfpath-") as d:
            with conftest.allowed_tree(d):
                self.assertTrue(conftest._is_allowed(d))
                self.assertTrue(conftest._is_allowed(os.path.realpath(d)))

    def test_an_unrelated_path_is_still_refused(self) -> None:
        from tests import conftest

        self.assertFalse(conftest._is_allowed(os.path.expanduser("~/second-brain")))
        self.assertFalse(conftest._is_allowed(""))
        # A sibling whose name merely starts with the session root's name
        # must not match -- that is what the explicit separator is for.
        self.assertFalse(conftest._is_allowed(conftest._SESSION_TMP + "-evil"))
