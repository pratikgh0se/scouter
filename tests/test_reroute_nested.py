"""Nested cross-image WB reroute guard (htc-colorcal-rereview follow-up).

`scouter.core.darkroom.NO_WB_REROUTE` is a `contextvars.ContextVar` that
`moves.adjust`/`moves.say` set for the duration of their own write, so
`darkroom.set_param` never re-decides a WB route they already chose. The
re-review flagged a theoretical gap: "a nested cross-image reroute (a
reroute triggered while another image's reroute context is active) could
inherit or clobber the flag."

No caller in this codebase actually nests these calls today (see
`darkroom.no_wb_reroute_scope`'s docstring for the full argument: a genuinely
nested, single-thread call is safe by construction -- `contextvars.Token.
reset` restores the exact prior value in strict LIFO order). This module
forces the nesting anyway, by patching `moves._adjust_core` so image A's own
adjust call synchronously re-enters `moves.adjust` for a SECOND image before
it finishes -- reproducing the scenario the re-review described -- and
checks two things with independent oracles (Zenkai Z2):

- the nested call for image B is REFUSED (`RerouteNested`), typed, before
  anything reaches the bridge for B (oracle 1: the FakeBridge's recorded
  `darkroom.action` calls never name image B's path);
- image A's own reroute state and result are unaffected by the refused
  nested attempt (oracle 2: `NO_WB_REROUTE` reads back exactly as image A's
  own scope left it, and A's move completes normally).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest import mock

from scouter.core import darkroom, moves
from scouter.errors import RerouteNested
from tests.test_moves import _MovesFixture

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class NestedCrossImageRerouteTest(_MovesFixture):
    def setUp(self) -> None:
        super().setUp()
        # A second image, so the nested call is unambiguously "another
        # image's reroute", not a second call on the same one.
        self.raw_b = self.raw.parent / "P6281349.ORF"
        self.raw_b.write_bytes(b"not really a raw either")
        shutil.copy(FIXTURES / "P6281348.ORF.xmp", str(self.raw_b) + ".xmp")

    def _point_bridge_at(self, image_id: int, path: Path) -> None:
        self.bridge.responses["darkroom.current"] = {
            "image_id": image_id,
            "path": str(path),
            "view": "darkroom",
            "source": "action_images",
        }
        self.bridge.responses["image.lookup"] = {
            "found": True,
            "image_id": image_id,
            "path": str(path),
        }

    def test_nested_call_for_another_image_is_refused_and_outer_is_unaffected(self) -> None:
        events: list = []
        real_adjust_core = moves._adjust_core

        def nesting_adjust_core(
            module,
            param,
            delta,
            *,
            resolved,
            instance,
            commit,
            level,
            job,
            ms_precommit=0,
            flushed=False,
        ):
            if job == "job-A" and not events:
                # Still inside image A's `no_wb_reroute_scope` (moves.adjust
                # hasn't reached its own `finally` yet): switch the darkroom
                # to image B and try a second, nested reroute for it.
                events.append("entered-nested-attempt")
                self.assertTrue(
                    darkroom.NO_WB_REROUTE.get(), "image A's reroute must still be active here"
                )
                self._point_bridge_at(13, self.raw_b)
                try:
                    moves.adjust.__wrapped__("exposure", "exposure", 0.1, commit=False, job="job-B")
                    events.append("nested-attempt-did-not-raise")
                except RerouteNested as e:
                    events.append(("nested-refused", e.code))
                finally:
                    self._point_bridge_at(12, self.raw)
            return real_adjust_core(
                module,
                param,
                delta,
                resolved=resolved,
                instance=instance,
                commit=commit,
                level=level,
                job=job,
                ms_precommit=ms_precommit,
                flushed=flushed,
            )

        with mock.patch("scouter.core.moves._adjust_core", side_effect=nesting_adjust_core):
            result = moves.adjust.__wrapped__(
                "exposure", "exposure", 0.3, commit=False, job="job-A"
            )

        self.assertEqual(events, ["entered-nested-attempt", ("nested-refused", "reroute_nested")])
        # Oracle 1: nothing was sent to darktable for image B -- every
        # recorded `darkroom.action` call landed on image A's path.
        for call in self.sets():
            self.assertNotIn(str(self.raw_b), str(call))
        # Oracle 2: image A's own move completed exactly as an unnested
        # `adjust` would -- the refused nested attempt left no trace.
        self.assertAlmostEqual(result.previous, 1.5500002, places=5)
        self.assertAlmostEqual(result.target, 1.8500002, places=5)
        self.assertFalse(
            darkroom.NO_WB_REROUTE.get(), "image A's own scope must still have reset cleanly"
        )

    def test_a_reroute_already_active_on_this_context_refuses_immediately(self) -> None:
        """The guard itself, isolated from any call-stack nesting: entering
        `no_wb_reroute_scope` while it is already active always refuses,
        typed, and leaves the outer `.set` completely untouched."""
        token = darkroom.NO_WB_REROUTE.set(True)
        try:
            with self.assertRaises(RerouteNested) as ctx:
                with darkroom.no_wb_reroute_scope():
                    self.fail("must refuse before the body ever runs")
            self.assertEqual(ctx.exception.code, "reroute_nested")
            # The outer's own flag is exactly as it was -- the failed inner
            # attempt reset nothing that wasn't its own.
            self.assertTrue(darkroom.NO_WB_REROUTE.get())
        finally:
            darkroom.NO_WB_REROUTE.reset(token)
