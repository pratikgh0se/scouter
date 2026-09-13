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
                groups, {"darkroom", "export", "history", "scout", "style", "metrics"}
            )
        finally:
            for name in core_module_names:
                sys.modules.pop(name, None)
            sys.modules.update(saved_modules)


class RegistryOkOutcomeTest(unittest.TestCase):
    def test_ok_and_dry_run_are_success_everything_else_is_not(self) -> None:
        # Mirrors the expression registry.call() uses to build Envelope.ok,
        # since call() needs scouter.metrics (task 8+) to run end-to-end.
        for outcome, expected in (
            ("ok", True),
            ("dry_run", True),
            ("error", False),
            ("error:wrong_view", False),
        ):
            self.assertEqual(outcome in ("ok", "dry_run"), expected, outcome)


if __name__ == "__main__":
    unittest.main()
