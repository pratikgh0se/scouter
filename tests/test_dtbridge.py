import os
import unittest
from pathlib import Path

from darktable_mcp.bridge.client import BridgeError, BridgeTimeoutError
from scouter.core import dtbridge
from scouter.errors import BridgeTimeout, DbLocked, NotInDarkroom, UsageError, WrongView
from tests.scouter_fakes import (
    FAKE_CLI_WRITES_PPM,
    FakeBridge,
    fake_cli_script,
    write_minimal_style_sidecar,
)


class ClassificationTest(unittest.TestCase):
    def tearDown(self) -> None:
        dtbridge.set_bridge(None)

    def test_timeout_becomes_bridge_timeout_with_a_fix(self) -> None:
        dtbridge.set_bridge(FakeBridge(raises={"view.current": BridgeTimeoutError("no resp")}))
        with self.assertRaises(BridgeTimeout) as ctx:
            dtbridge.current_view()
        self.assertIn("darktable", ctx.exception.fix)

    def test_locked_database_message_becomes_db_locked(self) -> None:
        dtbridge.set_bridge(
            FakeBridge(raises={"view.current": BridgeError("ERROR: database is locked")})
        )
        with self.assertRaises(DbLocked):
            dtbridge.current_view()

    def test_gui_not_ready_is_a_retryable_bridge_timeout_not_a_restart(self) -> None:
        """Row 433: the plugin refuses GUI-touching verbs until darktable's
        main loop runs (the startup gtk_wrap self-deadlock). That refusal is
        tagged gui_not_ready and its fix says "retry", never "restart
        darktable" -- darktable is fine, it is still starting."""
        dtbridge.set_bridge(
            FakeBridge(
                raises={
                    "history.commit": BridgeError(
                        "gui_not_ready: darktable's main loop has not started yet "
                        "(no pixelpipe has finished 3s after the plugin loaded; waited "
                        "9500ms). Nothing was done; retry in a few seconds."
                    )
                }
            )
        )
        with self.assertRaises(BridgeTimeout) as ctx:
            dtbridge.call("history.commit", {"image_id": 1})
        self.assertTrue(ctx.exception.detail.get("gui_not_ready"))
        self.assertIn("retry", ctx.exception.fix)
        self.assertNotIn("restart", ctx.exception.fix)

    def test_wrong_view_names_the_view_and_the_fix(self) -> None:
        dtbridge.set_bridge(FakeBridge({"view.current": {"view": "lighttable"}}))
        with self.assertRaises(WrongView) as ctx:
            dtbridge.require_darkroom()
        self.assertIn("lighttable", str(ctx.exception))
        self.assertIn("darkroom open", ctx.exception.fix)

    def test_nan_becomes_not_in_darkroom_never_a_value(self) -> None:
        dtbridge.set_bridge(
            FakeBridge({"darkroom.action": {"nan": True, "status": None, "view": "darkroom"}})
        )
        with self.assertRaises(NotInDarkroom) as ctx:
            dtbridge.action("iop/exposure/exposure", effect="set", value=1.0)
        self.assertIn("nan", str(ctx.exception))

    def test_wrong_image_loaded_becomes_not_in_darkroom_with_message_preserved(self) -> None:
        """Lua fix round 2: a loaded-image mismatch is now a raised JSON-RPC
        error ("wrong image loaded: ...") instead of {opened: False, error:
        ...}. There is no WrongImage class (spec 9's table), so it classifies
        as not_in_darkroom, with the plugin's own message kept verbatim."""
        dtbridge.set_bridge(
            FakeBridge(
                raises={
                    "darkroom.open": BridgeError("wrong image loaded: expected 12, got 7"),
                }
            )
        )
        with self.assertRaises(NotInDarkroom) as ctx:
            dtbridge.call("darkroom.open", {"image_id": 12})
        self.assertIn("wrong image loaded", str(ctx.exception))
        self.assertIn("expected 12, got 7", str(ctx.exception))
        # E12 D4: the fix text must be the wrong-image one, not the generic
        # nan-action NotInDarkroom.fix, and never bridge_timeout's "start
        # darktable" (darktable IS running).
        self.assertIn("double-click", ctx.exception.fix)
        self.assertNotIn("start darktable", ctx.exception.fix)

    def test_lua_own_position_prefix_still_classifies_as_not_in_darkroom(self) -> None:
        """E12 D4/row 407: darktable_mcp.lua's `error(...)` calls (around
        line 1218) pass no explicit level, so Lua's own `error()` prepends
        `"<source>:<line>: "` to the message -- the REAL message the E12 live
        run saw was `"darktable_mcp.lua:1218: wrong image loaded: asked for
        113, darkroom shows 92 after 15000ms"`, not the bare
        "wrong image loaded: ..." the old anchored regex assumed. That
        message fell through to the generic bridge_timeout classification,
        misreporting a same-session-import refusal (R20, an honest limit --
        darktable cannot load a frame this job imported into the SAME
        running session) as if darktable were unreachable, and costing the
        15s wait for nothing."""
        dtbridge.set_bridge(
            FakeBridge(
                raises={
                    "darkroom.open": BridgeError(
                        "darktable_mcp.lua:1218: wrong image loaded: asked for 113, "
                        "darkroom shows 92 after 15000ms"
                    ),
                }
            )
        )
        with self.assertRaises(NotInDarkroom) as ctx:
            dtbridge.call("darkroom.open", {"image_id": 113})
        self.assertNotIsInstance(ctx.exception, BridgeTimeout)
        self.assertEqual(ctx.exception.code, "not_in_darkroom")
        self.assertIn("asked for 113", str(ctx.exception))
        self.assertIn("double-click", ctx.exception.fix)
        self.assertNotIn("start darktable", ctx.exception.fix)

    def test_wrong_image_loaded_classifies_with_the_real_bridge_prefix(self) -> None:
        """Phase 3 task 8: the real plugin's error is wrapped by
        `darktable_mcp.bridge.client.Bridge.call` as `"handler raised:
        <file>:<line>: wrong image loaded: ..."`, not a bare "wrong image
        loaded: ..." string. `_WRONG_IMAGE_RE` must classify that prefixed
        form too, or `darkroom.apply_style` never becomes `not_in_darkroom`
        on a real host (spec section 9)."""
        dtbridge.set_bridge(
            FakeBridge(
                raises={
                    "darkroom.apply_style": BridgeError(
                        "handler raised: darktable_mcp.lua:1234: wrong image loaded: "
                        "darkroom shows 7, requested 12"
                    ),
                },
            )
        )
        with self.assertRaises(NotInDarkroom) as ctx:
            dtbridge.call("darkroom.apply_style", {"image_id": 12, "name": "harbour-warm"})
        self.assertIn("wrong image loaded", str(ctx.exception))
        self.assertIn("darkroom shows 7, requested 12", str(ctx.exception))

    def test_editor_benchmark_own_message_shape_still_classifies(self) -> None:
        """Row 408, B3: the editor benchmark's `darkroom_open` (task F7) saw
        the message wrapped in the MCP tool_runner's OWN "handler raised: "
        prefix, ahead of Lua's own "<source>:<line>: " prefix --
        `"handler raised: .../darktable_mcp.lua:1218: wrong image loaded: "
        "asked for 3, darkroom shows 4 after 15000ms"`. Confirms the
        unanchored `search()` (this item's E12 D4 fix) also covers this
        exact shape, and that it happens for a frame from an EARLIER
        session too -- R20 is broader than "same session"."""
        dtbridge.set_bridge(
            FakeBridge(
                raises={
                    "darkroom.open": BridgeError(
                        "handler raised: /path/to/darktable_mcp.lua:1218: wrong image "
                        "loaded: asked for 3, darkroom shows 4 after 15000ms"
                    ),
                }
            )
        )
        with self.assertRaises(NotInDarkroom) as ctx:
            dtbridge.call("darkroom.open", {"image_id": 3})
        self.assertEqual(ctx.exception.code, "not_in_darkroom")
        self.assertIn("double-click", ctx.exception.fix)

    def test_action_path_returns_the_pinned_path(self) -> None:
        self.assertEqual(dtbridge.action_path("exposure", "exposure"), "iop/exposure/exposure")
        self.assertEqual(dtbridge.action_path("sigmoid", "contrast"), "iop/sigmoid/contrast")
        self.assertEqual(
            dtbridge.action_path("exposure", "black"), "iop/exposure/black level correction"
        )

    def test_action_path_refuses_an_unpinned_pair(self) -> None:
        """Carried finding 1: the guessed `iop/<module>/<param>` fallback is
        gone, because an unrecognised path wedges darktable's Lua worker for
        the whole session."""
        with self.assertRaises(UsageError) as ctx:
            dtbridge.action_path("newmod", "newparam")
        self.assertEqual(ctx.exception.code, "usage")
        self.assertIn("newmod.newparam", ctx.exception.message)
        self.assertIn("no parameter of `newmod` is pinned", ctx.exception.fix)
        self.assertIn("exposure", ctx.exception.fix)
        self.assertEqual(ctx.exception.detail["pinned_params_for_module"], [])

    def test_action_path_names_the_modules_pinned_params(self) -> None:
        with self.assertRaises(UsageError) as ctx:
            dtbridge.action_path("exposure", "nosuchparam")
        self.assertIn("pinned parameters for `exposure`", ctx.exception.fix)
        self.assertEqual(ctx.exception.detail["pinned_params_for_module"], ["black", "exposure"])
        self.assertIn("wedges", ctx.exception.fix)

    def test_probe_bypass_is_explicit_and_test_only(self) -> None:
        """The ONE bypass, for the host probes that must try unpinned paths.

        The second half of this test is the guard that keeps it test-only:
        no module under `scouter/` may pass `probe_unpinned`.
        """
        self.assertEqual(
            dtbridge.action_path("newmod", "newparam", probe_unpinned=True),
            "iop/newmod/newparam",
        )
        root = Path(dtbridge.__file__).resolve().parent.parent
        offenders = [
            str(p.relative_to(root.parent))
            for p in sorted(root.rglob("*.py"))
            if "probe_unpinned" in p.read_text(encoding="utf-8") and p.name != "dtbridge.py"
        ]
        self.assertEqual(offenders, [], "production code must never bypass the wedge guard")

    def test_resolve_image_accepts_a_path_and_an_id(self) -> None:
        bridge = FakeBridge(
            {
                "image.lookup": {"found": True, "image_id": 7, "path": "/a/x.ORF"},
                "darkroom.open": {
                    "image_id": 7,
                    "path": "/a/x.ORF",
                    "view": "darkroom",
                    "opened": False,
                    "already_open": True,
                },
            }
        )
        dtbridge.set_bridge(bridge)
        by_path = dtbridge.resolve_image("/a/x.ORF")
        self.assertEqual((by_path.image_id, by_path.xmp_path), (7, "/a/x.ORF.xmp"))
        by_id = dtbridge.resolve_image(7)
        self.assertEqual(by_id.image_id, 7)
        # Final review, blocker 2: BOTH forms resolve through `image.lookup`,
        # which reads dt.database and touches no GUI state. `darkroom.open`
        # loads an image into the darkroom and must never be a resolution
        # mechanism -- not for an id, not for a path.
        self.assertEqual(bridge.methods_called(), ["image.lookup", "image.lookup"])
        self.assertEqual(bridge.calls[1][1], {"image_id": 7})

    def test_resolve_image_accepts_a_numeric_string_id(self) -> None:
        bridge = FakeBridge(
            {
                "image.lookup": {"found": True, "image_id": 7, "path": "/a/x.ORF"},
            }
        )
        dtbridge.set_bridge(bridge)
        self.assertEqual(dtbridge.resolve_image("7").image_id, 7)
        self.assertEqual(bridge.calls[0], ("image.lookup", {"image_id": 7}))

    def test_an_unknown_path_is_a_usage_error_unless_the_verb_may_import(self) -> None:
        bridge = FakeBridge(
            {
                "image.lookup": {"found": False, "image_id": None, "path": "/a/x.ORF"},
                "darkroom.open": {
                    "image_id": 7,
                    "path": "/a/x.ORF",
                    "view": "darkroom",
                    "opened": True,
                    "already_open": False,
                    "imported": True,
                },
            }
        )
        dtbridge.set_bridge(bridge)
        with self.assertRaises(dtbridge.ImageNotFound) as ctx:
            dtbridge.resolve_image("/a/x.ORF")
        self.assertEqual(ctx.exception.code, "usage")
        self.assertNotIn("darkroom.open", bridge.methods_called())
        # A write verb may import it -- that IS darkroom.open's job (ruling 4).
        self.assertEqual(dtbridge.resolve_image("/a/x.ORF", may_import=True).image_id, 7)
        self.assertIn("darkroom.open", bridge.methods_called())

    def test_an_unknown_id_is_never_imported_even_for_a_writer(self) -> None:
        bridge = FakeBridge(
            {
                "image.lookup": {"found": False, "image_id": 99, "path": None},
            }
        )
        dtbridge.set_bridge(bridge)
        with self.assertRaises(dtbridge.ImageNotFound):
            dtbridge.resolve_image(99, may_import=True)
        self.assertNotIn("darkroom.open", bridge.methods_called())


class ReadsNeverOpenTheDarkroomTest(unittest.TestCase):
    """Final review, blocker 2: a read or a dry run must not move the darkroom.

    The bridge here REFUSES `darkroom.open` outright, so any verb that still
    resolves through it fails loudly instead of silently switching the view
    out from under whoever is using darktable.
    """

    def setUp(self) -> None:
        from tests.scouter_fakes import TempCache

        self.cache = TempCache()
        self.root = self.cache.start()
        self.raw = self.root / "P6281348.ORF"
        self.raw.write_bytes(b"raw")
        # Fix round 2, blocker 1: style.extract(no_filter=True) reads the
        # sidecar too now (never_include applies to the whole active
        # history, not just a requested `modules` list).
        write_minimal_style_sidecar(self.raw)
        self.bridge = FakeBridge(
            {
                "image.lookup": {"found": True, "image_id": 12, "path": str(self.raw)},
                "darkroom.current": {
                    "image_id": 12,
                    "path": str(self.raw),
                    "view": "darkroom",
                    "source": "action_images",
                },
                "list_styles": {"styles": [], "count": 0},
            },
            raises={
                "darkroom.open": AssertionError(
                    "darkroom.open must not be called by a read verb or a dry run"
                )
            },
        )
        dtbridge.set_bridge(self.bridge)
        self._saved_cli = os.environ.get("SCOUTER_DARKTABLE_CLI")

    def tearDown(self) -> None:
        dtbridge.set_bridge(None)
        if self._saved_cli is None:
            os.environ.pop("SCOUTER_DARKTABLE_CLI", None)
        else:
            os.environ["SCOUTER_DARKTABLE_CLI"] = self._saved_cli
        self.cache.stop()

    def _use_fake_cli(self) -> None:
        os.environ["SCOUTER_DARKTABLE_CLI"] = str(fake_cli_script(self.root, FAKE_CLI_WRITES_PPM))

    def test_history_read_does_not_open(self) -> None:
        from scouter.core import history

        history.read(image=str(self.raw))
        self.assertNotIn("darkroom.open", self.bridge.methods_called())

    def test_scout_stats_and_preview_do_not_open(self) -> None:
        from scouter.core import scout

        self._use_fake_cli()
        scout.stats(image=str(self.raw), px=64)
        scout.preview(image=str(self.raw), px=64, fmt="ppm")
        self.assertNotIn("darkroom.open", self.bridge.methods_called())

    def test_style_and_export_dry_runs_do_not_open(self) -> None:
        from scouter.core import export, style

        style.extract(image=str(self.raw), name="hero", modules=[], dry_run=True, no_filter=True)
        style.apply(image=str(self.raw), name="hero", dry_run=True)
        export.run(
            images=[str(self.raw)], preset="preview", out_dir=str(self.root / "out"), dry_run=True
        )
        self.assertNotIn("darkroom.open", self.bridge.methods_called())

    def test_a_darkroom_write_dry_run_calls_nothing_at_all(self) -> None:
        from scouter.core import darkroom

        darkroom.set_param(module="exposure", param="exposure", value=1.0, dry_run=True)
        darkroom.nudge(module="exposure", param="exposure", steps=1, dry_run=True)
        self.assertEqual(self.bridge.calls, [])


class ResolveImagesBatchTest(unittest.TestCase):
    """Ruling R15: `image.lookup` takes a list, so a set resolves in ONE
    bridge call instead of one per frame (the Lua handler's path match walks
    all of `dt.database`, so it was also one full library scan per frame)."""

    def tearDown(self) -> None:
        dtbridge.set_bridge(None)

    def _listy_bridge(self, table: dict[str, int]) -> FakeBridge:
        def one(params: dict) -> dict:
            if params.get("image_id") is not None:
                iid = int(params["image_id"])
                path = next((p for p, i in table.items() if i == iid), None)
                return {"found": path is not None, "image_id": iid, "path": path}
            path = params.get("path")
            iid = table.get(path)
            return {"found": iid is not None, "image_id": iid, "path": path}

        def lookup(params: dict) -> dict:
            if params.get("items") is not None:
                return {"results": [one(item) for item in params["items"]]}
            return one(params)

        bridge = FakeBridge({"image.lookup": lookup})
        dtbridge.set_bridge(bridge)
        return bridge

    def test_a_whole_set_costs_one_call_and_keeps_request_order(self) -> None:
        bridge = self._listy_bridge({"/a/1.orf": 11, "/a/2.orf": 12, "/a/3.orf": 13})
        out = dtbridge.resolve_images(["/a/3.orf", 12, "/a/1.orf"])
        self.assertEqual(bridge.count("image.lookup"), 1)
        self.assertEqual([r.image_id for r in out], [13, 12, 11])
        self.assertEqual(out[0].xmp_path, "/a/3.orf.xmp")

    def test_a_miss_is_returned_not_raised_so_one_bad_frame_never_kills_the_set(self) -> None:
        self._listy_bridge({"/a/1.orf": 11})
        out = dtbridge.resolve_images(["/a/1.orf", "/a/gone.orf", 99])
        self.assertEqual(out[0].image_id, 11)
        self.assertIsInstance(out[1], dtbridge.ImageNotFound)
        self.assertIn("/a/gone.orf", str(out[1]))
        self.assertEqual(out[1].detail["path"], "/a/gone.orf")
        self.assertIsInstance(out[2], dtbridge.ImageNotFound)
        self.assertEqual(out[2].detail["image_id"], 99)

    def test_an_empty_set_makes_no_bridge_call_at_all(self) -> None:
        bridge = self._listy_bridge({})
        self.assertEqual(dtbridge.resolve_images([]), [])
        self.assertEqual(bridge.calls, [])

    def test_a_plugin_too_old_for_the_list_form_degrades_to_one_call_each(self) -> None:
        """A stale installed plugin answers without `results`. That must cost
        speed, never correctness -- so the fallback is one single-form call
        per image, exactly what `resolve_image` has always done."""
        table = {"/a/1.orf": 11, "/a/2.orf": 12}

        def legacy(params: dict) -> dict:
            path = params.get("path")
            iid = table.get(path)
            return {"found": iid is not None, "image_id": iid, "path": path}

        bridge = FakeBridge({"image.lookup": legacy})
        dtbridge.set_bridge(bridge)
        out = dtbridge.resolve_images(["/a/1.orf", "/a/2.orf"])
        self.assertEqual([r.image_id for r in out], [11, 12])
        # 1 rejected batch attempt + 2 single-form calls.
        self.assertEqual(bridge.count("image.lookup"), 3)

    def test_the_style_set_resolver_uses_the_list_form(self) -> None:
        """The consumer ruling R15 was raised for: `style.apply_set`'s
        `_resolve_set` must resolve its candidates in one call."""
        from scouter.core import style

        bridge = self._listy_bridge({"/a/1.orf": 11, "/a/2.orf": 12, "/a/3.orf": 13})
        resolved, unresolved = style._resolve_set(
            None, ["/a/1.orf", "/a/2.orf", "/a/3.orf"], ["/a/3.orf"]
        )
        self.assertEqual([r.image_id for r in resolved], [11, 12])
        self.assertEqual(unresolved, [])
        # One call for the candidates, one for the --exclude list.
        self.assertEqual(bridge.count("image.lookup"), 2)


class GuiGateWaitFitsClientTimeoutTest(unittest.TestCase):
    """Row 433 review: the plugin's GUI-ready gate wait for each verb must stay
    well inside the Python client's timeout for that verb, or the typed
    gui_not_ready answer arrives after the client gave up (a generic
    bridge_timeout and a stale response file)."""

    def test_every_gated_verb_waits_at_most_half_its_client_timeout(self) -> None:
        import re

        from darktable_mcp.bridge.client import DEFAULT_TIMEOUTS, FALLBACK_TIMEOUT

        lua = (
            Path(__file__).resolve().parents[1] / "darktable_mcp" / "lua" / "darktable_mcp.lua"
        ).read_text()
        default_ms = int(re.search(r"^local GUI_READY_WAIT_MS = (\d+)", lua, re.M).group(1))
        table = re.search(r"^local GUI_GATE_WAIT_MS = \{(.*?)^\}", lua, re.M | re.S).group(1)
        overrides = {m: int(v) for m, v in re.findall(r'\["([^"]+)"\] = (\d+)', table)}
        exempt_block = re.search(r"^local GUI_GATE_EXEMPT = \{(.*?)^\}", lua, re.M | re.S).group(1)
        exempt = set(re.findall(r'\["([^"]+)"\]', exempt_block))
        methods = set(re.findall(r'^methods\["([^"]+)"\]', lua, re.M)) | set(
            re.findall(r"^methods\.([A-Za-z_]+)", lua, re.M)
        )
        self.assertIn("darkroom.status", methods)
        self.assertTrue(set(overrides) <= methods, set(overrides) - methods)
        for method in sorted(methods - exempt):
            wait_s = overrides.get(method, default_ms) / 1000
            client_s = DEFAULT_TIMEOUTS.get(method, FALLBACK_TIMEOUT)
            with self.subTest(method=method):
                self.assertLessEqual(
                    wait_s, client_s / 2, f"{method}: gate {wait_s}s vs client {client_s}s"
                )


if __name__ == "__main__":
    unittest.main()
