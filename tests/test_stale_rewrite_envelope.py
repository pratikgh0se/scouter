"""The stale-rewrite retry must be visible in the tool envelope on every write path that
uses `darkroom._commit`: a warning for the human and `data.stale_rewrite_retried: true` for
the gauntlet. Faked: the first history.commit_expecting is a restored stale_rewrite, the next
succeeds."""

from __future__ import annotations

import unittest
from unittest import mock

from scouter import registry
from scouter.core import darkroom
from scouter.errors import CommitTimeout
from scouter.types import CommitResult
from tests.test_moves import _MovesFixture

_REAL_COMMIT = darkroom._commit


def _stale() -> CommitTimeout:
    return CommitTimeout(
        "stale_rewrite: older history",
        detail={
            "stale_rewrite": "history_end went back from 76 to 54",
            "stale_rewrite_restored": True,
            "strategy_used": "view_bounce_action",
        },
    )


class _Base(_MovesFixture):
    def setUp(self) -> None:
        super().setUp()
        self.calls = 0
        outer = self

        def fake(image, **kw):
            if kw.get("expect_change", True):
                outer.calls += 1
                if outer.calls == 1:
                    raise _stale()
            outer.land()
            return CommitResult(committed=True, changed=kw.get("expect_change", True), warnings=[])

        # re-route the fixture's `_commit` patch to the real one, faking only history
        p1 = mock.patch("scouter.core.darkroom._commit", side_effect=_REAL_COMMIT)
        p2 = mock.patch("scouter.core.history.commit_expecting", side_effect=fake)
        p1.start()
        p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)

    def land(self) -> None:
        """What darktable's sidecar says after the last set landed (row 433)."""
        from pathlib import Path

        from scouter.core import dtbridge
        from tests.scouter_fakes import with_new_history_hash, with_param_value

        acts = [
            p for m, p in self.bridge.calls if m == "darkroom.action" and p.get("effect") == "set"
        ]
        if not acts:
            return
        key = next((k for k, v in dtbridge.ACTION_PATHS.items() if v == acts[-1]["path"]), None)
        sidecar = Path(str(self.raw) + ".xmp")
        text = sidecar.read_text()
        if key and key[0] in ("exposure", "sigmoid"):
            text = with_param_value(text, key[0], key[1], acts[-1]["value"])
        sidecar.write_text(with_new_history_hash(text))

    def check(self, env) -> None:
        self.assertGreaterEqual(self.calls, 2, "the retry did not run")
        self.assertIn(env.outcome, ("ok", "moved_to_limit"), env)
        self.assertTrue(env.data.get("stale_rewrite_retried"), env.data)
        self.assertIn("re-bounce", " ".join(env.data.get("warnings", [])), env.data)


class EnvelopeTest(_Base):
    def test_set(self) -> None:
        self.check(
            registry.call(
                "darkroom.set",
                "cli",
                module="exposure",
                param="exposure",
                value=0.9,
                job="job-test",
            )
        )

    def test_adjust(self) -> None:
        self.check(
            registry.call(
                "darkroom.adjust",
                "cli",
                module="exposure",
                param="exposure",
                delta=0.3,
                job="job-test",
            )
        )

    def test_say(self) -> None:
        self.check(registry.call("darkroom.say", "cli", phrase="less contrast", job="job-test"))

    def test_no_flag_without_a_retry(self) -> None:
        self.calls = 99  # never stale
        env = registry.call(
            "darkroom.set", "cli", module="exposure", param="exposure", value=0.9, job="job-test"
        )
        self.assertNotIn("stale_rewrite_retried", env.data)


if __name__ == "__main__":
    unittest.main()
