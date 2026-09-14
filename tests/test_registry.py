import importlib
import sys
import tempfile
import unittest
from pathlib import Path

from scouter import registry


class LoadVerbsImportGuardTest(unittest.TestCase):
    """load_verbs() must only swallow an ImportError for the module it was
    trying to import (or one of its parent packages) — never a typo-shaped
    ImportError raised from inside a core module that does exist."""

    def setUp(self) -> None:
        self._orig_core_modules = registry._CORE_MODULES
        self._orig_registry = dict(registry.VERB_REGISTRY)
        self._orig_fully_loaded = registry._FULLY_LOADED
        self._orig_sys_path = list(sys.path)
        self._orig_sys_modules = set(sys.modules)
        self._tmpdir = tempfile.TemporaryDirectory()
        sys.path.insert(0, self._tmpdir.name)

    def tearDown(self) -> None:
        registry._CORE_MODULES = self._orig_core_modules
        registry.VERB_REGISTRY.clear()
        registry.VERB_REGISTRY.update(self._orig_registry)
        registry._FULLY_LOADED = self._orig_fully_loaded
        sys.path[:] = self._orig_sys_path
        for name in set(sys.modules) - self._orig_sys_modules:
            del sys.modules[name]
        importlib.invalidate_caches()
        self._tmpdir.cleanup()

    def _write_module(self, dotted_name: str, source: str) -> None:
        parts = dotted_name.split(".")
        pkg_dir = Path(self._tmpdir.name)
        for part in parts[:-1]:
            pkg_dir = pkg_dir / part
            pkg_dir.mkdir(exist_ok=True)
            init = pkg_dir / "__init__.py"
            if not init.exists():
                init.write_text("")
        (pkg_dir / f"{parts[-1]}.py").write_text(source)
        importlib.invalidate_caches()

    def test_absent_module_is_skipped(self) -> None:
        registry.VERB_REGISTRY.clear()
        registry._FULLY_LOADED = False
        registry._CORE_MODULES = ("scouter_test_totally_absent_module_xyz",)
        self.assertEqual(registry.load_verbs(), {})

    def test_typo_inside_an_existing_module_is_not_swallowed(self) -> None:
        self._write_module(
            "scouter_test_broken_core.bad",
            "import scouter_test_this_name_does_not_exist_anywhere\n",
        )
        registry.VERB_REGISTRY.clear()
        registry._FULLY_LOADED = False
        registry._CORE_MODULES = ("scouter_test_broken_core.bad",)
        with self.assertRaises(ImportError):
            registry.load_verbs()

    def test_a_direct_import_of_one_core_module_does_not_truncate_the_registry(
        self,
    ) -> None:
        """A partial `VERB_REGISTRY` (populated by some other code directly
        importing e.g. `scouter.core.style` before `load_verbs()` ever runs)
        must not be mistaken for "fully loaded" — `load_verbs()` still has to
        walk every module in `_CORE_MODULES` and return the complete set.

        Simulates a fresh process: every core module (plus `scouter.core.style`
        itself) is dropped from `sys.modules` so a plain `import` actually
        re-executes each module's top-level `register()` calls, exactly like
        the first import in a real interpreter session would.
        """
        core_module_names = list(registry._CORE_MODULES) + ["scouter.core.style"]
        saved_modules = {
            name: sys.modules.pop(name) for name in core_module_names if name in sys.modules
        }
        try:
            registry.VERB_REGISTRY.clear()
            registry._FULLY_LOADED = False

            import scouter.core.style  # noqa: F401  (direct import, bypassing load_verbs())

            self.assertTrue(registry.VERB_REGISTRY)  # style's register() calls ran
            self.assertFalse(registry._FULLY_LOADED)  # but load_verbs() hasn't run yet

            verbs = registry.load_verbs()
            groups = {verb.group for verb in verbs.values()}
            self.assertEqual(
                groups,
                {
                    "config",
                    "darkroom",
                    "export",
                    "history",
                    "scout",
                    "style",
                    "library",
                    "cull",
                    "outliers",
                    "backup",
                    "metrics",
                    "wb",
                },
            )
        finally:
            for name in core_module_names:
                sys.modules.pop(name, None)
            sys.modules.update(saved_modules)
            # Task 11 fix: `sys.modules` is not the only place a module
            # lives once it has been imported -- `import scouter.core.style`
            # (and `load_verbs()`'s own re-imports) also set each leaf as an
            # ATTRIBUTE on its parent package object (`scouter.core.darkroom
            # = <the fresh module>`), which `from scouter.core import
            # darkroom` prefers over a `sys.modules` lookup. Restoring only
            # `sys.modules` left that attribute pointing at the throwaway
            # module this test created, so every LATER `from scouter.core
            # import darkroom` (e.g. `wb.calibrate`'s own lazy import) kept
            # getting a different `darkroom` module object than the one
            # `mock.patch("scouter.core.darkroom._commit", ...)` patched --
            # a real bug this test's restoration used to leave behind.
            for name, module in saved_modules.items():
                parent_name, _, leaf = name.rpartition(".")
                parent = sys.modules.get(parent_name)
                if parent is not None:
                    setattr(parent, leaf, module)


class _FakeResult:
    """The minimal shape `registry.call` needs back from a verb function."""

    def __init__(self, outcome: str) -> None:
        self.outcome = outcome

    def to_dict(self) -> dict:
        return {"outcome": self.outcome, "detail": "from the test verb"}


class RegistryOkOutcomeTest(unittest.TestCase):
    """Which outcomes count as success -- asserted THROUGH `registry.call`.

    This used to re-state call()'s own `outcome in ("ok", "dry_run")`
    expression, which could not fail: the assertion and the code under test
    were the same line written twice (parked nit from task 2, final review
    should-fix 10). A throwaway verb is registered instead, so the envelope
    really is built by the code that ships.
    """

    VERB_ID = "metrics.registry_ok_probe"

    def setUp(self) -> None:
        from tests.scouter_fakes import TempCache

        self.cache = TempCache()
        self.cache.start()
        self.outcome = "ok"
        registry.load_verbs()  # so the throwaway verb is not clobbered
        self.verb = registry.Verb(
            id=self.VERB_ID,
            group="metrics",
            fn=self._probe,
            args=(),
            writes=False,
            gated=False,
            summary="test-only probe verb",
        )
        registry.register(self.verb)
        self.addCleanup(registry.VERB_REGISTRY.pop, self.VERB_ID, None)
        self.addCleanup(self.cache.stop)

    def _probe(self, *, job: str | None = None) -> _FakeResult:
        self.seen_job = job
        return _FakeResult(self.outcome)

    def test_ok_and_dry_run_are_success_everything_else_is_not(self) -> None:
        for outcome, expected in (
            ("ok", True),
            ("dry_run", True),
            ("error", False),
            ("error:wrong_view", False),
        ):
            with self.subTest(outcome=outcome):
                self.outcome = outcome
                envelope = registry.call(self.VERB_ID, "core")
                self.assertEqual(envelope.ok, expected, outcome)
                self.assertEqual(envelope.outcome, outcome)
                self.assertEqual(envelope.data["detail"], "from the test verb")

    def test_call_passes_the_ambient_job_through_to_the_verb(self) -> None:
        envelope = registry.call(self.VERB_ID, "core")
        self.assertEqual(self.seen_job, "job-test")
        self.assertEqual(envelope.job, "job-test")

    def test_a_scouter_error_from_the_verb_becomes_an_error_envelope(self) -> None:
        from scouter.errors import WrongView

        def boom(*, job: str | None = None):
            raise WrongView("the view is lighttable")

        registry.VERB_REGISTRY[self.VERB_ID] = registry.Verb(
            id=self.VERB_ID,
            group="metrics",
            fn=boom,
            args=(),
            writes=False,
            gated=False,
            summary="test-only probe verb",
        )
        envelope = registry.call(self.VERB_ID, "core")
        self.assertFalse(envelope.ok)
        self.assertEqual(envelope.outcome, "error:wrong_view")
        assert envelope.error is not None
        self.assertEqual(envelope.error["code"], "wrong_view")


if __name__ == "__main__":
    unittest.main()
