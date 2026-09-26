"""Product close, item 2 (row 427 item 4): trust the sidecar by default,
with a staleness guard.

Row 415 measured the pre-read bounce at ~2.7 s of a 4.1 s one-move `say`
(1.37 s without it). The guard skips it when the sidecar is provably at least
as new as the journal's last move on the same image, and bounces when the
image changed, there is no sidecar, or the last move is newer than the
sidecar (a commit=False move darktable has not written yet). `bounce=True`
forces it; `trust_sidecar=True` still skips it unconditionally.

The fake bridge's `darkroom._commit` is patched (tests.test_moves's fixture)
and records every bounce in `self.commits`; a pre-read bounce is the
`(1, False, expect_module)` call -- `expect_module` is the module the
caller (`get`/`adjust`) is about to touch, exactly like every other writer's
commit call (row 461 follow-up).
"""

from __future__ import annotations

import os
import time

from scouter.core import history, journal, moves
from scouter.errors import UsageError
from tests.test_moves import _MovesFixture

PRE = (1, False, "exposure")
#: `say` moves may span several modules, so its pre-commit bounce expects none
#: in particular (unlike `adjust`/`get`'s single-module bounce).
PRE_SAY = (1, False, None)


class PrecommitGuardTest(_MovesFixture):
    def _land_move(self, image_id: int = 12, *, sidecar_after: bool = True) -> None:
        entry = journal.begin("job-test", verb="darkroom.set", image_id=image_id,
                              path=str(self.raw), module="exposure", param="exposure",
                              instance=0, before={"value": 1.55})
        journal.complete("job-test", entry.seq, after={"value": 1.55})
        sidecar = str(self.raw) + ".xmp"
        now = time.time()
        stamp = now + 5 if sidecar_after else now - 60
        os.utime(sidecar, (stamp, stamp))

    def test_a_single_move_on_a_fresh_sidecar_performs_no_bounce(self) -> None:
        self._land_move()
        r = moves.adjust.__wrapped__("exposure", "exposure", 0.3, commit=False, job="job-test")
        self.assertEqual(self.commits, [], "no pre-read bounce on a fresh sidecar")
        self.assertEqual(r.plan[0]["ms_precommit"], 0)
        self.assertFalse(r.plan[0]["precommit"]["bounced"])
        self.assertIn("newer than the last move", r.plan[0]["precommit"]["reason"])

    def test_say_on_a_fresh_sidecar_performs_no_bounce(self) -> None:
        self._land_move()
        r = moves.say.__wrapped__("a touch brighter", commit=False, job="job-test")
        self.assertEqual(self.commits, [])
        self.assertFalse(r.plan[0]["bounced"])

    def test_first_move_on_an_image_bounces_once(self) -> None:
        r = moves.say.__wrapped__("a touch brighter", commit=False, job="job-test")
        self.assertEqual(self.commits, [PRE_SAY])
        self.assertTrue(r.plan[0]["bounced"])
        self.assertIn("first move", r.plan[0]["reason"])

    def test_a_sidecar_older_than_the_last_move_bounces(self) -> None:
        self._land_move(sidecar_after=False)
        moves.adjust.__wrapped__("exposure", "exposure", 0.3, commit=False, job="job-test")
        self.assertEqual(self.commits, [PRE])

    def test_an_uncommitted_move_makes_the_next_one_bounce(self) -> None:
        self._land_move()
        moves.adjust.__wrapped__("exposure", "exposure", 0.3, commit=False, job="job-test")
        self.assertEqual(self.commits, [])
        # That move was commit=False: its journal entry is newer than the sidecar.
        os.utime(str(self.raw) + ".xmp", (time.time() - 60, time.time() - 60))
        moves.adjust.__wrapped__("exposure", "exposure", 0.1, commit=False, job="job-test")
        self.assertEqual(self.commits, [PRE])

    def test_a_changed_image_bounces(self) -> None:
        self._land_move(image_id=99)
        r = moves.adjust.__wrapped__("exposure", "exposure", 0.3, commit=False, job="job-test")
        self.assertEqual(self.commits, [PRE])
        self.assertIn("image changed", r.plan[0]["precommit"]["reason"])

    def test_scouters_own_watermark_counts_as_fresh(self) -> None:
        history.watermark_write("job-test", 12, str(self.raw) + ".xmp")
        moves.adjust.__wrapped__("exposure", "exposure", 0.3, commit=False, job="job-test")
        self.assertEqual(self.commits, [])

    def test_bounce_forces_the_pre_read_commit(self) -> None:
        self._land_move()
        moves.adjust.__wrapped__("exposure", "exposure", 0.3, commit=False, bounce=True,
                                 job="job-test")
        self.assertEqual(self.commits, [PRE])
        moves.say.__wrapped__("a touch brighter", commit=False, bounce=True, job="job-test")
        self.assertEqual(self.commits, [PRE, PRE_SAY])

    def test_trust_sidecar_still_never_bounces(self) -> None:
        moves.adjust.__wrapped__("exposure", "exposure", 0.3, commit=False,
                                 trust_sidecar=True, job="job-test")
        self.assertEqual(self.commits, [])

    def test_bounce_and_trust_sidecar_together_are_a_usage_error(self) -> None:
        with self.assertRaises(UsageError):
            moves.adjust.__wrapped__("exposure", "exposure", 0.3, bounce=True,
                                     trust_sidecar=True, job="job-test")

    def test_get_never_bounces_by_default_and_bounces_on_request(self) -> None:
        moves.get.__wrapped__("exposure", "exposure", job="job-test")
        self.assertEqual(self.commits, [])
        moves.get.__wrapped__("exposure", "exposure", bounce=True, job="job-test")
        self.assertEqual(self.commits, [PRE])
