import unittest
from unittest import mock

from scouter import metrics, registry
from tests.scouter_fakes import TempCache


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
            missing, [],
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
                    len(after) - before, 1,
                    f"{verb_id} emitted {len(after) - before} events, expected exactly 1",
                )
                self.assertEqual(after[-1]["verb"], verb_id)
