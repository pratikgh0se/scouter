import shutil
import time
import unittest
from pathlib import Path
from unittest import mock

from scouter import metrics, registry
from tests.scouter_fakes import FakeBridge, TempCache, with_new_history_hash, with_param_value

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "P6281348.ORF.xmp"


class EmitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = TempCache()
        self.cache.start()

    def tearDown(self) -> None:
        self.cache.stop()

    def test_instrumented_writes_one_event_one_row_one_audit_line(self) -> None:
        from scouter.types import Payload

        @metrics.instrumented("test.verb")
        def verb(*, value: int, job: str | None = None) -> Payload:
            return Payload(image_id=12, path="/abs/x.ORF")

        verb(value=3)
        events = self.cache.events()
        self.assertEqual(len(events), 1)
        event = events[0]
        for key in metrics.EVENT_KEYS:
            self.assertIn(key, event)
        self.assertEqual(event["verb"], "test.verb")
        self.assertEqual(event["layer"], "core")
        self.assertEqual(event["outcome"], "ok")
        self.assertEqual(event["images"], [12])
        self.assertTrue(event["args_digest"].startswith("sha256:"))
        self.assertEqual(event["est_tokens"], -(-event["bytes_out"] // 4))
        self.assertEqual(len(self.cache.db_rows()), 1)
        audit = self.cache.audit()
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["args"]["value"], 3)
        # Review round 1, should-fix (D3, row 407): "startup_ms" -- time
        # from process start to THIS call's verb start -- attributes a
        # future stall that happens before a verb's body runs, which `ms`
        # itself cannot see. Not in EVENT_KEYS/the sqlite schema on
        # purpose (no metrics.db migration path exists yet), so it is
        # checked in events.jsonl and the audit line specifically, not in
        # the loop above.
        self.assertIn("startup_ms", event)
        self.assertIsInstance(event["startup_ms"], int)
        self.assertGreaterEqual(event["startup_ms"], 0)
        self.assertIn("startup_ms", audit[0])

    def test_startup_ms_grows_with_process_age(self) -> None:
        """Pinned against the real mechanism, not just the key's presence:
        moving the recorded process-start further into the past must move
        `startup_ms` up by roughly the same amount."""
        from scouter.types import Payload

        @metrics.instrumented("test.verb")
        def verb(*, job: str | None = None) -> Payload:
            return Payload()

        import scouter

        with mock.patch.object(scouter, "_PROCESS_STARTED_MONOTONIC", time.monotonic() - 5.0):
            verb()
        event = self.cache.events()[0]
        self.assertGreaterEqual(event["startup_ms"], 4900)

    def test_args_digest_excludes_job_and_is_stable(self) -> None:
        a = metrics.args_digest({"module": "exposure", "job": "job-1"})
        b = metrics.args_digest({"job": "job-2", "module": "exposure"})
        self.assertEqual(a, b)

    def test_error_still_emits_one_event_with_the_error_class(self) -> None:
        from scouter.errors import WrongView
        from scouter.types import Payload

        @metrics.instrumented("test.boom")
        def boom(*, job: str | None = None) -> Payload:
            raise WrongView("nope")

        with self.assertRaises(WrongView):
            boom()
        events = self.cache.events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["outcome"], "error:wrong_view")

    def test_layer_comes_from_the_contextvar(self) -> None:
        from scouter.types import Payload

        @metrics.instrumented("test.layered")
        def verb(*, job: str | None = None) -> Payload:
            return Payload()

        token = metrics.LAYER.set("mcp")
        try:
            verb()
        finally:
            metrics.LAYER.reset(token)
        self.assertEqual(self.cache.events()[0]["layer"], "mcp")

    def test_emit_200_times_closes_every_sqlite_connection(self) -> None:
        """emit()/summary() must not leak sqlite connections (fix round 1)."""
        from scouter.types import Payload

        class _CountingConn:
            """Wraps a real sqlite3.Connection and records whether it was closed."""

            def __init__(self, conn) -> None:
                self._conn = conn
                self.closed = False

            def __getattr__(self, name: str):
                return getattr(self._conn, name)

            def __enter__(self):
                self._conn.__enter__()
                return self

            def __exit__(self, *exc_info):
                return self._conn.__exit__(*exc_info)

            def close(self) -> None:
                self.closed = True
                self._conn.close()

        opened: list[_CountingConn] = []
        real_connect = metrics.sqlite3.connect

        def tracking_connect(*args, **kwargs):
            wrapped = _CountingConn(real_connect(*args, **kwargs))
            opened.append(wrapped)
            return wrapped

        @metrics.instrumented("test.manyconns")
        def verb(*, job: str | None = None) -> Payload:
            return Payload()

        with mock.patch.object(metrics.sqlite3, "connect", side_effect=tracking_connect):
            for _ in range(200):
                verb()
            metrics.summary(since="1d")

        self.assertGreaterEqual(len(opened), 200)
        for conn in opened:
            self.assertTrue(conn.closed, "a sqlite connection was left open")

    def test_metrics_write_failure_does_not_fail_the_verb(self) -> None:
        from scouter.types import Payload

        @metrics.instrumented("test.resilient")
        def verb(*, job: str | None = None) -> Payload:
            return Payload()

        with mock.patch.object(metrics, "_connect", side_effect=OSError("no disk")):
            self.assertEqual(verb().outcome, "ok")
        self.assertEqual(len(self.cache.events()), 1)


class SummaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = TempCache()
        self.cache.start()

    def tearDown(self) -> None:
        self.cache.stop()

    def test_summary_counts_and_alerts(self) -> None:
        from scouter.types import Payload

        @metrics.instrumented("test.flaky")
        def flaky(*, fail: bool, job: str | None = None) -> Payload:
            if fail:
                from scouter.errors import BridgeTimeout

                raise BridgeTimeout("no darktable")
            return Payload()

        for i in range(21):
            try:
                flaky(fail=i % 4 == 0)
            except Exception:
                pass
        result = metrics.summary(since="1d")
        row = next(r for r in result.verbs if r["verb"] == "test.flaky")
        self.assertEqual(row["calls"], 21)
        self.assertEqual(row["by_class"]["bridge_timeout"], 6)
        self.assertTrue(result.alerts, "6/21 failures must raise the 10% alert")
        self.assertIn("test.flaky", result.alerts[0]["message"])

    def test_bad_since_is_a_usage_error(self) -> None:
        with self.assertRaises(ValueError):
            metrics.summary(since="7 days")


class RegistryCoverageTest(unittest.TestCase):
    """Exit criterion E5: every registered verb emits exactly one event."""

    def setUp(self) -> None:
        self.cache = TempCache()
        self.cache.start()

    def tearDown(self) -> None:
        self.cache.stop()

    def test_every_registered_verb_emits_one_event(self) -> None:
        from tests.scouter_smoke_args import SMOKE_ARGS, install_doubles

        verbs = registry.load_verbs()
        self.assertTrue(verbs, "no verbs registered")
        missing = sorted(set(verbs) - set(SMOKE_ARGS))
        self.assertEqual(
            missing,
            [],
            "every registered verb needs smoke args in tests/scouter_smoke_args.py "
            f"so this coverage test can call it; missing: {missing}",
        )
        for verb_id in sorted(verbs):
            with self.subTest(verb=verb_id):
                before = len(self.cache.events())
                with install_doubles(verb_id) as args:
                    try:
                        registry.call(verb_id, "core", **{**SMOKE_ARGS[verb_id], **args})
                    except Exception:
                        # A verb can legitimately fail here (e.g. a fake bridge
                        # raising) — the point of this test is the event count
                        # below, not the call's success.
                        pass
                after = self.cache.events()
                self.assertEqual(
                    len(after) - before,
                    1,
                    f"{verb_id} emitted {len(after) - before} events, expected exactly 1",
                )
                self.assertEqual(after[-1]["verb"], verb_id)


class WriteVerbSingleEventTest(unittest.TestCase):
    """Fix round 1, point 1: a real (un-patched) commit must still be ONE event.

    `RegistryCoverageTest` above proves every verb emits one event when its
    *own* body runs, but `darkroom.set`'s dry-run-free smoke args commit
    through `_commit`, which internally calls `history.commit` -- a second
    `@instrumented` function. `test_darkroom_core.py` patches `_commit` out
    entirely, so it cannot catch a double emission there; this test leaves
    `_commit` real, with a FakeBridge whose `history.commit` handler actually
    advances the sidecar's mtime, so `history.commit`'s own poll loop runs for
    real too.
    """

    def setUp(self) -> None:
        self.cache = TempCache()
        root = self.cache.start()
        self.raw = root / "P6281348.ORF"
        self.raw.write_bytes(b"not really a raw")
        self.sidecar = Path(str(self.raw) + ".xmp")
        shutil.copy(FIXTURE, self.sidecar)

    def tearDown(self) -> None:
        from scouter.core import dtbridge

        dtbridge.set_bridge(None)
        self.cache.stop()

    def test_darkroom_set_with_a_real_commit_emits_exactly_one_event(self) -> None:
        from scouter.core import darkroom, dtbridge

        text = self.sidecar.read_text()

        def fake_commit(params: dict) -> dict:
            time.sleep(0.01)  # guarantee the mtime tick
            # a real rewrite, like darktable's own -- carrying the value
            # that was set (row 433: an unchanged value is an unverified write)
            self.sidecar.write_text(
                with_new_history_hash(
                    with_param_value(text, "exposure", "exposure", 1.2),
                    params.get("expect_module"),
                )
            )
            return {
                "strategy_used": "sidecar_action",
                "ok": True,
                "view_after": "darkroom",
                "cpu_ms": 5,
            }

        dtbridge.set_bridge(
            FakeBridge(
                {
                    "darkroom.open": {
                        "image_id": 12,
                        "path": str(self.raw),
                        "view": "darkroom",
                        "opened": False,
                        "already_open": True,
                    },
                    "darkroom.current": {
                        "image_id": 12,
                        "path": str(self.raw),
                        "view": "darkroom",
                        "source": "action_images",
                    },
                    "image.lookup": {"found": True, "image_id": 12, "path": str(self.raw)},
                    "darkroom.action": lambda p: {"nan": False, "view": "darkroom", **p},
                    "history.commit": fake_commit,
                }
            )
        )

        before = len(self.cache.events())
        result = darkroom.set_param("exposure", "exposure", 1.2, commit=True, job="job-test")
        after = self.cache.events()

        self.assertTrue(result.committed)
        self.assertEqual(
            len(after) - before,
            1,
            f"darkroom.set with commit=True emitted {len(after) - before} events, "
            "expected exactly 1 (history.commit must run un-instrumented, via "
            "`.__wrapped__`, when called from another core verb)",
        )
        self.assertEqual(after[-1]["verb"], "darkroom.set")

    def test_the_escalated_set_path_also_emits_exactly_one_event(self) -> None:
        """Carried finding 7 (task 12): the ESCALATED path, pinned.

        The test above drives the ordinary `commit=True` path, where
        `_commit` calls `history.commit` once. The mid path is different: it
        tries `expect_change=False` first, cannot confirm the result, and
        ESCALATES to a second, `expect_change=True` `history.commit` call --
        two nested `@instrumented` entries in one top-level verb. Two calls
        through `__wrapped__` are exactly as easy to get wrong as one, and
        nothing pinned this branch, so a regression there would have been a
        silent double-count in every metrics summary.
        """
        from scouter.core import darkroom, dtbridge

        text = self.sidecar.read_text()
        calls = {"n": 0}

        def commit_handler(params: dict) -> dict:
            calls["n"] += 1
            if params.get("expect_change") is False:
                # Nothing flushed, and the sidecar is GONE, so no readback
                # can confirm the prediction -- this is what forces the
                # escalation to a real, expect_change=True commit.
                self.sidecar.unlink(missing_ok=True)
                return {
                    "strategy_used": "sidecar_action",
                    "ok": True,
                    "sidecar_written": False,
                    "view_after": "darkroom",
                    "cpu_ms": 1,
                }
            time.sleep(0.01)
            self.sidecar.write_text(with_new_history_hash(text))
            return {
                "strategy_used": "view_bounce",
                "ok": True,
                "sidecar_written": True,
                "view_after": "darkroom",
                "cpu_ms": 5,
            }

        dtbridge.set_bridge(
            FakeBridge(
                {
                    "darkroom.current": {
                        "image_id": 12,
                        "path": str(self.raw),
                        "view": "darkroom",
                        "source": "action_images",
                    },
                    "image.lookup": {"found": True, "image_id": 12, "path": str(self.raw)},
                    "darkroom.action": lambda p: {"nan": False, "view": "darkroom", **p},
                    "module.enable": {"nan": False, "view": "darkroom"},
                    "history.commit": commit_handler,
                }
            )
        )

        # No watermark at all + a requested value the sidecar already
        # carries: that is the MID path -- `expect_change=False` first,
        # because the pre-check matched but nothing yet certifies it.
        before = len(self.cache.events())
        result = darkroom.set_param("exposure", "exposure", 1.5500002, commit=True, job="job-test")
        after = self.cache.events()

        self.assertGreaterEqual(calls["n"], 2, "this test must actually drive the escalation")
        self.assertEqual(
            len(after) - before,
            1,
            f"the escalated darkroom.set path emitted {len(after) - before} events, "
            "expected exactly 1 (both history.commit calls must go through "
            "`.__wrapped__`)",
        )
        self.assertEqual(after[-1]["verb"], "darkroom.set")
        self.assertTrue(result.committed)
