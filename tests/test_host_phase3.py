# tests/test_host_phase3.py
"""Phase 3 task 10: the host probe (design §10). Gated: SCOUTER_HOST_TESTS=1 + darktable.

Never snapshots ~/.config/darktable (P3-R18): PluginInstall touches the plugin
file and luarc only. Every class quits darktable in tearDown/finally.

The pure pinning helpers (`probe_verdict`, `sentinel_for`, `sentinel_slots`,
`entry_floats`) run ungated in `PinningHelpersTest` against fakes, so the
logic that decides what gets pinned is covered by the unit suite; only the
two host classes need darktable.
"""

from __future__ import annotations

import os
import shutil
import statistics
import struct
import time
import unittest
from pathlib import Path
from typing import Any, Callable

import pytest

from tests import host_support as hs

RUN = os.environ.get("SCOUTER_HOST_TESTS") == "1" and hs.DARKTABLE.exists()
REASON = "host-only: set SCOUTER_HOST_TESTS=1 with darktable installed"
SCRATCH = hs.SCRATCH_DIR / "phase3-probe.ORF"
RESULTS = Path(__file__).resolve().parent.parent / "docs" / "measurements-generated.md"
WB_TABLE = Path(__file__).resolve().parent / "fixtures" / "wb-em10iii.json"


def _record(line: str) -> None:
    print(line)
    with RESULTS.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


# --- pure helpers (unit-tested below with fakes) ---------------------------

Call = Callable[..., Any]


def probe_verdict(call: Call, path: str) -> tuple[str, int]:
    """§10.1 steps 2-4 for ONE candidate: `pinned`, `absent` or `wedges`, plus ms.

    `call(method, params, timeout=...)` is `dtbridge.call` on the host. A
    non-nan answer only counts as `pinned` if `view.current` still answers
    within 5 s afterwards (the worker is not wedged).
    """
    t0 = time.monotonic()
    try:
        r = call("darkroom.status", {"path": path, "instance": 0}, timeout=10.0)
        verdict = "absent" if r.get("nan") else "pinned"
    except Exception:  # noqa: BLE001 -- any failure to answer is a wedge
        verdict = "wedges"
    ms = int((time.monotonic() - t0) * 1000)
    if verdict == "pinned":
        try:
            call("view.current", {}, timeout=5.0)
        except Exception:  # noqa: BLE001
            verdict = "wedges"
    return verdict, ms


#: A path pinned since phase 1: the per-launch positive control.
CONTROL_PATH = "iop/exposure/exposure"
#: Host finding (2026-09-24 task 10 run): `wait_ready` returns ~2 s after
#: launch, while darktable is still loading the darkroom pipeline; any
#: `darkroom.status`/`darkroom.action` sent then got no answer within 10 s --
#: INCLUDING the already-pinned `iop/colorbalancergb/global saturation`. After
#: a 20 s settle the same call answers non-nan in ~110 ms.
SETTLE_S = 20.0


def settle(call: Call, sleep: Callable[[float], None] = time.sleep) -> bool:
    """Wait out the pipeline load, then prove the worker answers a KNOWN path.

    False means this launch cannot judge a candidate at all (recorded as
    `error`, never as a verdict on the candidate)."""
    sleep(SETTLE_S)
    try:
        r = call("darkroom.status", {"path": CONTROL_PATH, "instance": 0}, timeout=30.0)
    except Exception:  # noqa: BLE001
        return False
    return not r.get("nan")


def sentinel_for(spec: Any, k: int) -> float:
    """A distinct, in-range sentinel for the k-th row of a module (§10.2).

    Rows of one module accumulate in ONE history entry, so each gets its own
    value or the second row would find two matching slots. Two decimals at
    most: the 2026-09-24 host run showed darktable rounds a `set` to the
    slider's displayed precision (sigmoid skew 0.173 -> 0.17, filmic black
    relative exposure -9.917 -> -9.92, color zones mix 0.173 -> 0.17), so a
    three-decimal sentinel matched no slot at all.
    """
    if spec.wraps:
        return 37.0 + k
    value = round(0.17 - 0.01 * k, 2)
    if spec.lo <= value <= spec.hi:
        return value
    return round(spec.lo + (spec.hi - spec.lo) * 0.37 + 0.01 * k, 2)


def sentinel_slots(floats: list[float], sentinel: float, tol: float = 1e-4) -> list[int]:
    return [i for i, v in enumerate(floats) if abs(v - sentinel) < tol]


def entry_floats(params_hex: str) -> list[float]:
    """Every float of a history entry's params, in slot order, layout or not."""
    from scouter.core import xmp

    blob = xmp.decode_params(params_hex)
    count = len(blob) // 4
    return list(struct.unpack(f"<{count}f", blob[: count * 4]))


class PinningHelpersTest(unittest.TestCase):
    """Ungated: the helpers that decide a pin, against fake bridges."""

    def _fake(self, status: Any, view: Any = None) -> tuple[Call, list]:
        calls: list = []

        def call(method: str, params: dict, timeout: float | None = None) -> Any:
            calls.append((method, dict(params), timeout))
            answer = status if method == "darkroom.status" else view
            if isinstance(answer, BaseException):
                raise answer
            return answer if answer is not None else {"view": "darkroom"}

        return call, calls

    def test_non_nan_and_alive_is_pinned(self) -> None:
        call, calls = self._fake({"nan": False, "position": 0.5})
        self.assertEqual(probe_verdict(call, "iop/x/y")[0], "pinned")
        self.assertEqual([c[0] for c in calls], ["darkroom.status", "view.current"])
        self.assertEqual(calls[0][1], {"path": "iop/x/y", "instance": 0})
        self.assertEqual((calls[0][2], calls[1][2]), (10.0, 5.0))

    def test_nan_is_absent_and_skips_the_liveness_check(self) -> None:
        call, calls = self._fake({"nan": True})
        self.assertEqual(probe_verdict(call, "iop/x/y")[0], "absent")
        self.assertEqual(len(calls), 1)

    def test_no_answer_is_a_wedge(self) -> None:
        call, _ = self._fake(TimeoutError("no answer"))
        self.assertEqual(probe_verdict(call, "iop/x/y")[0], "wedges")

    def test_an_answer_then_a_dead_worker_is_a_wedge_not_a_pin(self) -> None:
        call, _ = self._fake({"nan": False}, view=TimeoutError("worker gone"))
        self.assertEqual(probe_verdict(call, "iop/x/y")[0], "wedges")

    def test_settle_waits_then_requires_the_control_path_to_answer(self) -> None:
        slept: list[float] = []
        call, calls = self._fake({"nan": False, "position": 0.4})
        self.assertTrue(settle(call, sleep=slept.append))
        self.assertEqual(slept, [SETTLE_S])
        self.assertEqual(calls[0][:2], ("darkroom.status", {"path": CONTROL_PATH, "instance": 0}))

    def test_a_silent_or_nan_control_means_the_launch_cannot_judge(self) -> None:
        for status in (TimeoutError("busy"), {"nan": True}):
            call, _ = self._fake(status)
            self.assertFalse(settle(call, sleep=lambda s: None))

    def test_sentinels_are_distinct_and_in_range(self) -> None:
        from scouter.core import vocab

        by_module: dict[str, list[float]] = {}
        for s in vocab.PARAMS:
            k = len(by_module.setdefault(s.module, []))
            v = sentinel_for(s, k)
            by_module[s.module].append(v)
            with self.subTest(row=f"{s.module}.{s.param}"):
                if s.wraps:
                    self.assertTrue(0.0 <= v < 360.0)
                elif s.reachable:
                    self.assertTrue(s.lo <= v <= s.hi, v)
                self.assertEqual(v, round(v, 2))
        for module, values in by_module.items():
            self.assertEqual(len(values), len(set(values)), module)

    def test_exactly_one_slot_is_found_and_a_collision_is_visible(self) -> None:
        self.assertEqual(sentinel_slots([0.0, 0.17000001, 1.0], 0.17), [1])
        self.assertEqual(sentinel_slots([0.173, 0.173], 0.173), [0, 1])
        self.assertEqual(sentinel_slots([0.0, 1.0], 0.173), [])

    def test_entry_floats_decode_the_committed_colorbalancergb_fixture(self) -> None:
        from scouter.core import xmp

        fixture = Path(__file__).resolve().parent / "fixtures" / "colorbalancergb.xmp"
        entry = next(e for e in xmp.parse(fixture.read_text()).entries
                     if e.operation == "colorbalancergb")
        floats = entry_floats(entry.params_hex)
        self.assertEqual(len(floats), 33)
        self.assertEqual(sentinel_slots(floats, 0.2), [31], "contrast's pinned slot")


# --- host classes ------------------------------------------------------------


def _scratch() -> None:
    hs.SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
    if not SCRATCH.exists():
        shutil.copy(hs.HERO, SCRATCH)


@unittest.skipUnless(RUN, REASON)
@pytest.mark.host
class ProbeCandidatePathsHostTest(unittest.TestCase):
    """§10.1: ONE darktable launch per candidate path (an unknown path wedges the worker)."""

    def test_probe_every_candidate(self) -> None:
        from scouter.core import dtbridge, vocab

        _scratch()
        install = hs.PluginInstall()
        install.install()
        _record("\n## Phase 3 candidate-path probe "
                f"({time.strftime('%Y-%m-%d %H:%M')})\n\n"
                "| module.param | path | verdict | ms |\n|---|---|---|---|")
        try:
            for s in vocab.PARAMS:
                if vocab.status(s) != "candidate":
                    continue
                for path in s.candidate_paths:
                    proc = hs.launch(SCRATCH)
                    verdict, ms = "error", 0
                    try:
                        hs.wait_ready()
                        if settle(dtbridge.call):
                            verdict, ms = probe_verdict(dtbridge.call, path)
                        else:
                            verdict = "error (control path did not answer)"
                    except Exception as e:  # noqa: BLE001 -- recorded, next launch
                        verdict = f"error ({type(e).__name__})"
                    finally:
                        hs.quit_darktable(proc)
                    _record(f"| {s.module}.{s.param} | `{path}` | {verdict} | {ms} |")
                    if verdict == "pinned":
                        break
        finally:
            install.restore()


#: A private raw file for `ProbeExposureBlackDefaultHostTest`, never shared
#: with `SCRATCH` (which other classes in this module write history onto and
#: which can persist a stale sidecar across runs under `~/Pictures/
#: ScouterScratch`): this probe needs a frame with NO exposure history entry
#: at all, so it copies its own raw file and deletes any leftover sidecar
#: before every launch.
_BLACK_DEFAULT_SCRATCH = hs.SCRATCH_DIR / "phase3-black-default.ORF"


@unittest.skipUnless(RUN, REASON)
@pytest.mark.host
class ProbeExposureBlackDefaultHostTest(unittest.TestCase):
    """Row 474 (final-review issue I1): host-verifies `exposure.black`'s
    `default_candidate` (`scouter/core/vocab.py`, 0.0).

    The phase3 design spec's own §4.3 table
    (docs/superpowers/specs/2026-09-24-scouter-phase3-design.md:148-153)
    leaves this row's `default` column blank by design -- it fills that
    column only where a value is asserted neutral by construction, and
    groups `exposure.black` with rows it does NOT assert that for. Nothing
    else in this repo may claim the value is known; this is the probe that
    can.

    Row 495/496: the host run found a fresh exposure module's `black` at
    -0.000244140625, not exactly 0.0. Fix round 1, finding 2 (reviewer,
    darktable `src/iop/exposure.c reload_defaults()`, master, fetched
    2026-09-27): this is NOT a quantisation artifact -- it is darktable's
    own DECLARED default for `black` on a scene-referred colour raw
    (`vocab.EXPOSURE_BLACK_SCENE_RAW_DEFAULT`, -(2**-12)); every other
    workflow (monochrome, non-scene-raw) still defaults `black` to exactly
    0.0. Equality here uses a tolerance (`vocab.default_tolerance`) sized
    to cover BOTH of those real defaults, instead of a fixed decimal-places
    check, since a places-based `assertAlmostEqual` would fail on this
    exact, expected scene-raw reading. PASS: `default_candidate=0.0` has
    been promoted to `default=0.0` in `scouter/core/vocab.py`, citing this
    test (not spec §4.3, which stays unedited); `tests/gauntlet/
    exec_looks.py`'s `chart_golden` no longer flags `exposure.black` as
    `default_unverified` (it now reads `row.default`, ahead of
    `default_candidate`, in `_probe_base`).
    """

    def test_fresh_exposure_module_defaults_black_to_zero(self) -> None:
        from scouter import registry
        from scouter.core import dtbridge, history

        hs.SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy(hs.HERO, _BLACK_DEFAULT_SCRATCH)
        Path(str(_BLACK_DEFAULT_SCRATCH) + ".xmp").unlink(missing_ok=True)

        install = hs.PluginInstall()
        install.install()
        proc = hs.launch(_BLACK_DEFAULT_SCRATCH)
        try:
            hs.wait_ready()
            self.assertTrue(settle(dtbridge.call),
                            f"control path {CONTROL_PATH} did not answer after settle")
            enabled = registry.call("darkroom.enable", "cli", module="exposure")
            self.assertTrue(enabled.ok, enabled.error)
            committed = registry.call("history.commit", "cli", image=str(_BLACK_DEFAULT_SCRATCH))
            self.assertTrue(committed.ok, committed.error)

            entries = [e for e in history.read(str(_BLACK_DEFAULT_SCRATCH)).entries
                       if e.operation == "exposure"]
            self.assertTrue(entries, "enabling exposure wrote no history entry")
            entry = max(entries, key=lambda e: e.num)
            self.assertIsNotNone(entry.decoded, entry.reason)
            black = entry.decoded["black"]
            _record("\n## Phase 3 exposure.black default probe "
                    f"({time.strftime('%Y-%m-%d %H:%M')})\n\n"
                    f"fresh exposure module (modversion {entry.modversion}, "
                    f"num {entry.num}, enabled={entry.enabled}): black={black!r}\n")
            from scouter.core import vocab

            self.assertAlmostEqual(black, 0.0, delta=vocab.default_tolerance("exposure", "black"))
        finally:
            hs.quit_darktable(proc)
            install.restore()


#: Two PRIVATE raw files for `ProbeColorzonesStrengthDefaultHostTest`,
#: never shared with `SCRATCH` or with each other: this probe needs a frame
#: with NO colorzones history entry at all (same reasoning as
#: `_BLACK_DEFAULT_SCRATCH` above), and -- fix round 2, host run
#: 2026-09-29: a shared scratch copy/session let oracle A's "fresh enable"
#: read back a sidecar oracle B's sentinel had already rewritten, so the
#: two readings agreed for a reason that proved nothing -- oracle A and
#: oracle B now each get their OWN copy and their OWN darktable launch, so
#: neither can observe the other's write.
_COLORZONES_STRENGTH_SCRATCH_A = hs.SCRATCH_DIR / "phase3-colorzones-strength-default-a.ORF"
_COLORZONES_STRENGTH_SCRATCH_B = hs.SCRATCH_DIR / "phase3-colorzones-strength-default-b.ORF"


@unittest.skipUnless(RUN, REASON)
@pytest.mark.host
class ProbeColorzonesStrengthDefaultHostTest(unittest.TestCase):
    """Card t_29a9ef47 (decision rows 548, 549; row 531's `default_candidate`
    follow-up): host-verifies `colorzones.strength`'s `default_candidate`
    (`scouter/core/vocab.py:315-318`, 0.0).

    Review round 2 (b3-item9-review.md) rejected a single-oracle design: a
    read of `_effective_current` after `default=` is set only reads back the
    declared number, so it is not an independent measurement. This probe
    instead uses two INDEPENDENT host oracles, both grounded in the XMP
    sidecar, never in `dt.gui.action`'s post-write `status` echo (row
    430/461 class; see `ProbeExposureBlackDefaultHostTest`'s docstring and
    b3-item9-review.md finding 1 for why that echo is not a value oracle):

    - Oracle A: on its OWN fresh scratch copy and its OWN darktable launch,
      enable colorzones with no other change, commit, decode the newest
      `colorzones` history entry's `strength` slot (xmp.py:148, colorzones
      v5 named slot 127 -- the same slot the phase 3 task 10 host run's
      0.16 sentinel pinned, docs/measurements.md:440).
    - Oracle B: on a SEPARATE fresh scratch copy and a SEPARATE darktable
      launch, first prove slot 127 is live on THIS host/darktable build by
      setting the same 0.16 sentinel, committing, and asserting it decodes
      back as ~0.16 (a dead or zero-filled slot would silently agree with a
      0.0 default reading in oracle A without this check). Then reset
      colorzones to its defaults, commit, and decode `strength` again.

    Fix round 2 (host run 2026-09-29): the first two-oracles-one-session
    design had oracle A read back EXACTLY the sentinel value -- a shared
    scratch copy/session meant "fresh enable" was not actually fresh by the
    time it was read. Splitting into two independent copies/launches fixes
    the cause; an explicit guard (below) fails loudly if it ever recurs.

    PASS requires oracle A's reading and oracle B's post-reset reading to
    agree (within `vocab.default_tolerance`, fixed at 1e-6 here -- no
    darktable source found citing a non-zero declared default for
    `colorzones.strength`'s `mix` param, unlike `exposure.black`'s
    documented scene-raw constant).

    PASS: promote `default_candidate=0.0` (vocab.py:318) to `default=0.0`,
    citing this test; add the `tests/intent/` example the report's draft
    names (Z1); this also fixes `_mid`/`_at_baseline`/`_neutral` for this
    row in `tests/gauntlet/layers/coverage.py:155-175` and unblocks the two
    P0 gauntlet cases (`cov-colorzones.strength-{olympus,sony_a7}-set_lo`).
    """

    def test_fresh_colorzones_module_defaults_strength_to_zero(self) -> None:
        from scouter import registry
        from scouter.core import dtbridge, history, vocab

        hs.SCRATCH_DIR.mkdir(parents=True, exist_ok=True)

        def newest_strength(image: str) -> float:
            entries = [e for e in history.read(image).entries if e.operation == "colorzones"]
            self.assertTrue(entries, "no colorzones history entry")
            entry = max(entries, key=lambda e: e.num)
            self.assertIsNotNone(entry.decoded, entry.reason)
            return entry.decoded["strength"]

        install = hs.PluginInstall()
        install.install()
        try:
            # Oracle A: its own scratch copy, its own launch, quit before
            # oracle B's copy even exists -- nothing it does can touch A.
            shutil.copy(hs.HERO, _COLORZONES_STRENGTH_SCRATCH_A)
            Path(str(_COLORZONES_STRENGTH_SCRATCH_A) + ".xmp").unlink(missing_ok=True)
            image_a = str(_COLORZONES_STRENGTH_SCRATCH_A)
            proc = hs.launch(_COLORZONES_STRENGTH_SCRATCH_A)
            try:
                hs.wait_ready()
                self.assertTrue(settle(dtbridge.call),
                                f"control path {CONTROL_PATH} did not answer after settle")
                enabled = registry.call("darkroom.enable", "cli", module="colorzones")
                self.assertTrue(enabled.ok, enabled.error)
                committed = registry.call("history.commit", "cli", image=image_a)
                self.assertTrue(committed.ok, committed.error)
                strength_a = newest_strength(image_a)
            finally:
                hs.quit_darktable(proc)

            # Oracle B: a separate scratch copy, a separate launch. Sentinel
            # first (mandatory), then reset.
            shutil.copy(hs.HERO, _COLORZONES_STRENGTH_SCRATCH_B)
            Path(str(_COLORZONES_STRENGTH_SCRATCH_B) + ".xmp").unlink(missing_ok=True)
            image_b = str(_COLORZONES_STRENGTH_SCRATCH_B)
            proc = hs.launch(_COLORZONES_STRENGTH_SCRATCH_B)
            try:
                hs.wait_ready()
                self.assertTrue(settle(dtbridge.call),
                                f"control path {CONTROL_PATH} did not answer after settle")
                sentinel = registry.call("darkroom.set", "cli", module="colorzones",
                                          param="strength", value=0.16)
                self.assertTrue(sentinel.ok, sentinel.error)
                committed = registry.call("history.commit", "cli", image=image_b)
                self.assertTrue(committed.ok, committed.error)
                strength_sentinel = newest_strength(image_b)

                reset = registry.call("darkroom.reset", "cli", module="colorzones", confirm=True)
                self.assertTrue(reset.ok, reset.error)
                committed = registry.call("history.commit", "cli", image=image_b)
                self.assertTrue(committed.ok, committed.error)
                strength_reset = newest_strength(image_b)
            finally:
                hs.quit_darktable(proc)
        finally:
            install.restore()

        # All three decoded values, printed BEFORE any value assertion
        # below can fail, so one host run always yields the measurement
        # even on a FAIL.
        _record("\n## Phase 3 colorzones.strength default probe "
                f"({time.strftime('%Y-%m-%d %H:%M')})\n\n"
                f"oracle A (fresh enable) strength={strength_a!r}; "
                f"sentinel (0.16 requested) strength={strength_sentinel!r}; "
                f"oracle B (post-reset) strength={strength_reset!r} "
                "(internal units 0..2, display_scale 100 i.e. GUI 100% == internal 1.0)\n")

        # Contamination guard (fix round 2, host run 2026-09-29): oracle A
        # must never read a sidecar the sentinel already touched. With the
        # two-copy/two-launch split above this should be structurally
        # impossible; this fails loudly, not silently, if it ever recurs.
        self.assertFalse(
            abs(strength_a - 0.16) <= 1e-5,
            f"oracle A read a post-sentinel sidecar (strength_a={strength_a!r} "
            "matches the 0.16 sentinel) -- the two oracles are contaminated")

        # The sentinel only needs to prove slot 127 is LIVE (not dead, not
        # zero-filled, not a different slot) -- it passes through
        # darktable's float32 param storage and the GTK slider's own
        # quantisation, the same floor row 502 hit for exposure.black's
        # host-measured value. 1e-5 is two orders of magnitude looser than
        # that floor (host run 2026-09-29 measured 0.16 as
        # 0.1600027084350586, delta 2.7e-6) while staying far below any
        # real slider step (colorzones.strength's own step is 0.1), so a
        # dead or moved slot (which decodes 0.0 or garbage) still fails
        # this check.
        self.assertAlmostEqual(strength_sentinel, 0.16, delta=1e-5,
                                msg="slot 127 did not decode the 0.16 sentinel -- "
                                    "either the slot moved or it is dead")

        # The default comparison (oracle A vs the post-reset read) stays at
        # the tight 1e-6: both sides are darktable running the module's
        # REAL default (no live user value in play, no sentinel involved),
        # so there is no float32-floor reason to widen it here.
        tolerance = vocab.default_tolerance("colorzones", "strength", fallback=1e-6)
        self.assertAlmostEqual(strength_a, strength_reset, delta=tolerance,
                                msg="oracle A (fresh enable) and oracle B (post-reset) "
                                    "disagree on colorzones.strength's default")
        self.assertAlmostEqual(strength_a, vocab.spec("colorzones", "strength").default,
                               delta=tolerance,
                               msg="host default disagrees with vocab.py's declared default")


#: The 10 colorbalancergb rows that carry only `neutral` today (no
#: host-verified `default`) and gate the 26 coverage cases
#: m1-default-report.md's "Fix round 1" M4 section lists as staying failed
#: after the M1 declared-default fallback (b2edf66, decision rows 548/549):
#: the fallback only credits a row with a verified `default`, never
#: `neutral` (I1 of that fix). The hue rows (`shadows_hue`/`midtones_hue`/
#: `highlights_hue`) already declare a host-verified `default`
#: (`_CB_HUE_DEFAULT`, `scouter/core/vocab.py`) and are NOT probed here.
_CB_NEUTRAL_ROWS = (
    "shadows_luminance", "shadows_chroma",
    "midtones_luminance", "midtones_chroma",
    "highlights_luminance", "highlights_chroma",
    "global_chroma", "global_saturation", "global_vibrance",
    "contrast",
)


@unittest.skipUnless(RUN, REASON)
@pytest.mark.host
class ProbeColorbalancergbNeutralDefaultsHostTest(unittest.TestCase):
    """Card t_cc03dcb4 (decision row 560; follows `t_29a9ef47`, rows
    548/549/554): host-verifies the darktable default for the 10
    colorbalancergb rows in `_CB_NEUTRAL_ROWS`, exactly the rows
    `m1-default-report.md` names as neutral-only.

    Same two-oracle design as `ProbeColorzonesStrengthDefaultHostTest`
    (fresh-enable vs. mandatory-sentinel-then-reset, each its own scratch
    copy and darktable launch, never trusting `dt.gui.action`'s post-write
    `status` echo -- rows 430/461/548), batched across all 10 rows within
    each oracle's ONE launch because darktable writes every colorbalancergb
    param into a SINGLE history entry per commit (xmp.py's
    `("colorbalancergb", 5)` named-slot layout, task 12's slot probe):

    - Oracle A: on its OWN fresh scratch copy and its OWN darktable launch,
      enable colorbalancergb with no other change, commit once, decode the
      newest `colorbalancergb` history entry, read all 10 named slots.
    - Oracle B: on a SEPARATE fresh scratch copy and a SEPARATE darktable
      launch, first prove every slot is LIVE on this host/build (the
      sentinel is mandatory, per the colorzones design) by setting a
      distinct in-range sentinel (`sentinel_for`) on each of the 10 rows in
      turn, committing and decoding after EACH set (so a dead or colliding
      slot fails at its own row, never silently smoothed over by the next
      row's write), asserting each decodes back near its own sentinel.
      Then reset colorbalancergb to its defaults, commit once, and decode
      all 10 slots again.

    A row is verified only when its sentinel read back live AND oracle A's
    fresh-enable reading agrees with oracle B's post-reset reading within
    `vocab.default_tolerance` (1e-6 fallback -- no darktable source found
    citing a non-zero declared default for any of these 10 fields, same
    reasoning as `colorzones.strength`). A row that disagrees, or whose
    sentinel does not read back live, is recorded as NOT verified; this
    test never loosens the tolerance or the two-oracle design to force a
    pass -- it fails loudly instead, per row.

    `hs.assert_no_concurrent_darktable()` runs first, before anything else,
    refusing if a second darktable process or a non-stale pending bridge
    request/response file is found -- two darktable launches sharing a
    plugin config, library.db or bridge spool would let one read or write
    the OTHER's scratch copy through the wrong worker. Every scratch path
    below is unique per run (timestamp + pid) and asserted absent before
    use, so a rerun can never open on stale `library.db` history from an
    earlier run on the same path. See docs/measurements.md "colorbalancergb
    neutral-only defaults" for the run this test's evidence lives in.
    """

    def test_fresh_colorbalancergb_module_defaults_rows_to_neutral(self) -> None:
        from scouter import registry
        from scouter.core import dtbridge, history, vocab

        # Refuse before touching anything if a second darktable or a
        # non-stale pending bridge request could contaminate this run.
        # Called here, not from PluginInstall.install(), so a non-host unit
        # test of PluginInstall never depends on the real host's live spool.
        hs.assert_no_concurrent_darktable()

        hs.SCRATCH_DIR.mkdir(parents=True, exist_ok=True)

        # a unique name per run, so a rerun can never reopen a path
        # `library.db` already has colorbalancergb history on.
        stamp = f"{time.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}"
        scratch_a = hs.SCRATCH_DIR / f"phase3-cb-neutral-{stamp}-a.ORF"
        scratch_b = hs.SCRATCH_DIR / f"phase3-cb-neutral-{stamp}-b.ORF"

        def assert_fresh(scratch: Path) -> None:
            self.assertFalse(scratch.exists(), f"{scratch} already exists")
            xmp_path = Path(str(scratch) + ".xmp")
            self.assertFalse(xmp_path.exists(), f"{xmp_path} already exists")

        def colorbalancergb_entries(image: str) -> list:
            return [e for e in history.read(image).entries if e.operation == "colorbalancergb"]

        def newest_decoded(image: str) -> dict:
            entries = colorbalancergb_entries(image)
            self.assertTrue(entries, "no colorbalancergb history entry")
            entry = max(entries, key=lambda e: e.num)
            self.assertIsNotNone(entry.decoded, entry.reason)
            return entry.decoded

        install = hs.PluginInstall()
        install.install()
        try:
            # Oracle A: its own scratch copy, its own launch, quit before
            # oracle B's copy even exists -- nothing it does can touch A.
            assert_fresh(scratch_a)
            shutil.copy(hs.HERO, scratch_a)
            image_a = str(scratch_a)
            proc = hs.launch(scratch_a)
            try:
                hs.wait_ready()
                self.assertTrue(settle(dtbridge.call),
                                f"control path {CONTROL_PATH} did not answer after settle")
                enabled = registry.call("darkroom.enable", "cli", module="colorbalancergb")
                self.assertTrue(enabled.ok, enabled.error)
                committed = registry.call("history.commit", "cli", image=image_a)
                self.assertTrue(committed.ok, committed.error)
                # I1: a fresh path must carry exactly one colorbalancergb
                # entry (the enable) -- more would mean stale library.db
                # history leaked in, which breaks oracle A's independence.
                a_entries = colorbalancergb_entries(image_a)
                self.assertEqual(len(a_entries), 1,
                                 f"oracle A: expected exactly one colorbalancergb history "
                                 f"entry (fresh enable), found {len(a_entries)}")
                decoded_a = newest_decoded(image_a)
            finally:
                hs.quit_darktable(proc)

            # Oracle B: a separate scratch copy, a separate launch.
            # Sentinel first (mandatory, one row at a time), then reset.
            assert_fresh(scratch_b)
            shutil.copy(hs.HERO, scratch_b)
            image_b = str(scratch_b)
            proc = hs.launch(scratch_b)
            sentinels: dict[str, float] = {}
            decoded_sentinel: dict[str, Any] = {}
            try:
                hs.wait_ready()
                self.assertTrue(settle(dtbridge.call),
                                f"control path {CONTROL_PATH} did not answer after settle")
                enabled = registry.call("darkroom.enable", "cli", module="colorbalancergb")
                self.assertTrue(enabled.ok, enabled.error)
                committed = registry.call("history.commit", "cli", image=image_b)
                self.assertTrue(committed.ok, committed.error)
                decoded_baseline_b = newest_decoded(image_b)
                for k, param in enumerate(_CB_NEUTRAL_ROWS):
                    spec = vocab.spec("colorbalancergb", param)
                    self.assertIsNotNone(spec, f"no vocab row for colorbalancergb.{param}")
                    sentinel = sentinel_for(spec, k)
                    r = registry.call("darkroom.set", "cli", module="colorbalancergb",
                                      param=param, value=sentinel)
                    self.assertTrue(r.ok, f"colorbalancergb.{param}: sentinel set failed: {r.error}")
                    committed = registry.call("history.commit", "cli", image=image_b)
                    self.assertTrue(committed.ok, committed.error)
                    sentinels[param] = sentinel
                    current = newest_decoded(image_b)
                    decoded_sentinel[param] = current.get(param)
                    # M1: this set must land in ONE slot only -- every other
                    # row of this probe must still read its own sentinel
                    # (already set) or the fresh-enable baseline (not yet
                    # set), never a value smeared in from this write.
                    for other in _CB_NEUTRAL_ROWS:
                        if other == param:
                            continue
                        expected = sentinels.get(other, decoded_baseline_b.get(other))
                        self.assertAlmostEqual(
                            current.get(other), expected, delta=1e-4,
                            msg=f"setting colorbalancergb.{param}={sentinel!r} moved "
                                f"colorbalancergb.{other} from {expected!r} to "
                                f"{current.get(other)!r}")

                reset = registry.call("darkroom.reset", "cli", module="colorbalancergb",
                                       confirm=True)
                self.assertTrue(reset.ok, reset.error)
                committed = registry.call("history.commit", "cli", image=image_b)
                self.assertTrue(committed.ok, committed.error)
                # I1: enable + 10 sentinel sets + reset == 12 entries.
                b_entries = colorbalancergb_entries(image_b)
                self.assertEqual(len(b_entries), 12,
                                 f"oracle B: expected exactly 12 colorbalancergb history "
                                 f"entries (1 enable + {len(_CB_NEUTRAL_ROWS)} sentinels + "
                                 f"1 reset), found {len(b_entries)}")
                decoded_reset = newest_decoded(image_b)
            finally:
                hs.quit_darktable(proc)
        finally:
            install.restore()

        # Every row's three readings, printed BEFORE any value assertion
        # below can fail, so one host run always yields the measurement
        # even on a FAIL. The stamp identifies exactly which scratch files
        # this table's numbers came from (I1).
        lines = [
            "\n## Phase 3 colorbalancergb neutral-only defaults probe "
            f"({time.strftime('%Y-%m-%d %H:%M')}, run stamp {stamp})\n",
            "| row | oracle A (fresh enable) | sentinel requested/decoded | "
            "oracle B (post-reset) | verified |",
            "| --- | --- | --- | --- | --- |",
        ]
        failures = []
        for param in _CB_NEUTRAL_ROWS:
            a = decoded_a.get(param)
            s_req = sentinels[param]
            s_dec = decoded_sentinel[param]
            b = decoded_reset.get(param)
            tolerance = vocab.default_tolerance("colorbalancergb", param, fallback=1e-6)
            # The sentinel only needs to prove the slot is LIVE (not dead,
            # not zero-filled, not a different slot) -- 1e-4 mirrors the
            # sentinel-slot match tolerance `sentinel_slots` uses elsewhere
            # in this file, well inside any of these rows' own step.
            sentinel_ok = isinstance(s_dec, (int, float)) and abs(s_dec - s_req) <= 1e-4
            reset_ok = (isinstance(a, (int, float)) and isinstance(b, (int, float))
                        and abs(a - b) <= tolerance)
            verified = sentinel_ok and reset_ok
            lines.append(f"| colorbalancergb.{param} | {a!r} | {s_req!r}/{s_dec!r} | {b!r} | "
                         f"{'yes' if verified else 'NO'} |")
            if not verified:
                failures.append(
                    f"colorbalancergb.{param}: sentinel_ok={sentinel_ok} "
                    f"(requested {s_req!r} decoded {s_dec!r}), reset_ok={reset_ok} "
                    f"(oracle A={a!r} oracle B={b!r}, tolerance={tolerance!r})")
        _record("\n".join(lines) + "\n")

        self.assertFalse(
            failures,
            "colorbalancergb rows failed host verification (never verified by loosening "
            "tolerance -- these stay neutral-only):\n" + "\n".join(failures))


@unittest.skipUnless(RUN, REASON)
@pytest.mark.host
class Phase3LiveHostTest(unittest.TestCase):
    """§10.2 slots, §10.3 signs, preset round trip and timing, in ONE launch."""

    @classmethod
    def setUpClass(cls) -> None:
        _scratch()
        # The session config dir (conftest pins SCOUTER_CONFIG to a temp
        # file) gets the camera slug, a temp library root for the preset's
        # memory copy, and the committed E-M10 III WB table, so "warmer"
        # has a Kelvin read. Nothing here is a real user path.
        cls._env_before = {k: os.environ.get(k) for k in ("SCOUTER_JOB",)}
        config = Path(os.environ["SCOUTER_CONFIG"])
        cls.library_root = config.parent / "phase3-library"
        cls.library_root.mkdir(parents=True, exist_ok=True)
        config.write_text(
            '{"version": 1, "cameras": {"E-M10 Mark III": "em10iii"}, '
            f'"library_root": "{cls.library_root}"}}', encoding="utf-8")
        (config.parent / "wb").mkdir(exist_ok=True)
        shutil.copy(WB_TABLE, config.parent / "wb" / "em10iii.json")
        os.environ["SCOUTER_JOB"] = f"phase3-host-{os.getpid()}"
        cls.install = hs.PluginInstall()
        cls.install.install()
        cls.proc = hs.launch(SCRATCH)
        try:
            hs.wait_ready()
            from scouter.core import dtbridge
            if not settle(dtbridge.call):
                raise RuntimeError(f"control path {CONTROL_PATH} did not answer after settle")
        except BaseException:
            hs.quit_darktable(cls.proc)
            cls.install.restore()
            raise
        _record(f"\n## Phase 3 live host run ({time.strftime('%Y-%m-%d %H:%M')})\n")

    @classmethod
    def tearDownClass(cls) -> None:
        from scouter import registry
        try:
            registry.call("history.commit", "cli", image=str(SCRATCH))
        finally:
            hs.quit_darktable(cls.proc)
            cls.install.restore()
            for k, v in cls._env_before.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_1_slot_pinning_by_sentinel(self) -> None:
        from scouter import registry
        from scouter.core import history, vocab

        per_module: dict[str, int] = {}
        for s in vocab.PARAMS:
            if vocab.status(s) != "absolute_only" or s.value_source != "xmp":
                continue
            k = per_module.get(s.module, 0)
            per_module[s.module] = k + 1
            sentinel = sentinel_for(s, k)
            en = registry.call("darkroom.enable", "cli", module=s.module)
            r = registry.call("darkroom.set", "cli", module=s.module, param=s.param,
                              value=sentinel)
            if not r.ok and (r.error or {}).get("code") == "commit_timeout":
                # The slot already held this value (an earlier run's rounded
                # sentinel): darktable writes no sidecar for a no-change set.
                # One retry with a shifted sentinel.
                sentinel = sentinel + 0.5 if s.wraps else round(sentinel - 0.01, 2)
                r = registry.call("darkroom.set", "cli", module=s.module, param=s.param,
                                  value=sentinel)
            if not r.ok:
                _record(f"slot {s.module}.{s.param}: set {sentinel} FAILED enable_ok={en.ok} "
                        f"error={r.error}")
                continue
            entry = max((e for e in history.read(str(SCRATCH)).entries
                         if e.operation == s.module), key=lambda e: e.num)
            floats = entry_floats(entry.params_hex)
            slots = sentinel_slots(floats, sentinel)
            _record(f"slot {s.module}.{s.param} v{entry.modversion}: sentinel {sentinel} "
                    f"set ok={r.ok} slots={slots} of {len(floats)} "
                    f"(size {len(entry.params_hex) // 2})")
        # Every module this test touched, once, in full: what the pins came from.
        for module in per_module:
            entries = [e for e in history.read(str(SCRATCH)).entries if e.operation == module]
            if entries:
                last = max(entries, key=lambda e: e.num)
                _record(f"floats {module} v{last.modversion} num {last.num}: "
                        f"{[round(v, 4) for v in entry_floats(last.params_hex)]}")
        sidecar = Path(str(SCRATCH) + ".xmp")
        if sidecar.exists():
            shutil.copy(sidecar, hs.SCRATCH_DIR / f"phase3-slots-{os.getpid()}.xmp")

    def test_2_signs(self) -> None:
        from scouter import registry

        def stats() -> dict:
            return registry.call("scout.stats", "cli", image=str(SCRATCH), px=512).data["stats"]

        def rb(st: dict) -> float:
            ch = st["channels"]
            return ch["r"]["mean"] / max(ch["b"]["mean"], 1e-6)

        base = stats()
        registry.call("darkroom.adjust", "cli", module="exposure", param="black", delta=0.01)
        deeper = stats()
        _record(f"sign exposure.black +0.01: luminance_mean {base['luminance_mean']} -> "
                f"{deeper['luminance_mean']} (expect down)")
        registry.call("darkroom.undo_last", "cli")

        base = stats()
        t = registry.call("darkroom.set", "cli", module="temperature", param="tint", value=0.9)
        tinted = stats()
        _record(f"sign temperature.tint 0.9 (ok={t.ok}): r/b {rb(base):.4f} -> {rb(tinted):.4f} "
                f"wb_ratio_rg {base['wb_ratio_rg']} -> {tinted['wb_ratio_rg']}, "
                f"wb_ratio_bg {base['wb_ratio_bg']} -> {tinted['wb_ratio_bg']} "
                "(spec expects r/b down)")
        registry.call("darkroom.set", "cli", module="temperature", param="tint", value=1.0)

        from scouter.core import vocab
        if vocab.status(vocab.spec("colorbalancergb", "highlights_hue")) in ("live",
                                                                            "absolute_only"):
            base = stats()
            said = registry.call("darkroom.say", "cli", phrase="warm the highlights")
            warm = stats()
            _record(f"sign warm the highlights (hue 60 + chroma, ok={said.ok} "
                    f"outcome={said.outcome} error={said.error and said.error.get('code')}): "
                    f"r/b {rb(base):.4f} -> {rb(warm):.4f} (expect up)")
            if said.ok:
                registry.call("darkroom.undo_last", "cli")

    def test_3_preset_round_trip(self) -> None:
        from scouter import registry
        from scouter.core import dtbridge

        name = f"scouter-probe-{os.getpid()}"
        warmer = registry.call("darkroom.say", "cli", phrase="warmer")
        self.assertTrue(warmer.ok, (warmer.data, warmer.error))
        saved = registry.call("preset.save", "cli", name=name, confirm=True)
        self.assertTrue(saved.ok, (saved.data, saved.error))
        try:
            listed = registry.call("preset.list", "cli")
            self.assertIn(name, [p["name"] for p in listed.data["presets"]])
            registry.call("darkroom.say", "cli", phrase="cooler")
            applied = registry.call("preset.apply", "cli", name=name)
            _record(f"preset.apply open image: ok={applied.ok} "
                    f"history_end {applied.data.get('history_end_before')} -> "
                    f"{applied.data.get('history_end_after')} error={applied.error}")
            self.assertTrue(applied.ok, applied.error)
            self.assertGreater(applied.data["history_end_after"],
                               applied.data["history_end_before"])
        finally:
            deleted = dtbridge.call("style.delete",
                                    {"name": name, "prefix": "scouter-", "confirm": True})
            memory = saved.data.get("memory_path")
            if memory:
                Path(memory).unlink(missing_ok=True)
            _record(f"preset cleanup: style.delete -> {deleted}; memory copy removed: "
                    f"{memory and not Path(memory).exists()}")

    def test_4_conversation_timing(self) -> None:
        """Row 427 item 4: the DEFAULT is now trust-the-sidecar with the
        staleness guard (moves.precommit_decision), so only a move the guard
        cannot vouch for pays the ~2.7 s pre-read bounce. The forced-bounce
        loop is kept for comparison with row 415's 4.12 s."""
        from scouter import registry

        times = []
        for phrase in ("brighter", "darker", "brighter", "darker", "less contrast"):
            t0 = time.monotonic()
            r = registry.call("darkroom.say", "cli", phrase=phrase)
            times.append(time.monotonic() - t0)
            self.assertTrue(r.ok, (r.data, r.error))
            _record(f"  say {phrase!r}: {times[-1]:.2f} s, verb ms {r.ms}, "
                    f"plan {r.data.get('plan')}")
        _record(f"darkroom.say default (staleness guard) wall times (s): "
                f"{[round(t, 2) for t in times]}, median {statistics.median(times):.2f}")
        forced = []
        for phrase in ("brighter", "darker", "brighter", "darker", "less contrast"):
            t0 = time.monotonic()
            r = registry.call("darkroom.say", "cli", phrase=phrase, bounce=True)
            forced.append(time.monotonic() - t0)
        _record(f"darkroom.say bounce=True (forced pre-commit bounce) wall times (s): "
                f"{[round(t, 2) for t in forced]}, median {statistics.median(forced):.2f}")
        self.assertLess(statistics.median(times), 3.5)
