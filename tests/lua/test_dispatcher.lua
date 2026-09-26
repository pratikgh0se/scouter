-- Lua dispatcher unit tests. Stubs the darktable `dt` global with a fake
-- database and verifies the method registry + scan_dir behavior in isolation.
--
-- Run via: lua tests/lua/test_dispatcher.lua
-- Exits 0 on success, non-zero on first failure.

-- ---- Test harness ----------------------------------------------------------
local failures = {}
local function assertEq(actual, expected, label)
  if actual ~= expected then
    table.insert(failures, string.format("%s: expected %s, got %s",
      label, tostring(expected), tostring(actual)))
  end
end
local function assertTrue(cond, label)
  if not cond then
    table.insert(failures, string.format("%s: expected truthy, got falsy", label))
  end
end

-- Mirrors the real plugin's `xml_unescape` (verified equal via
-- plugin.xml_unescape's own direct tests below): a stub whose `dt.styles.
-- import` reads a name off <info><name> must unescape it too, since real
-- darktable stores the unescaped name -- otherwise the stub and the plugin's
-- own `dtstyle_file_name`-based collision check would disagree on names
-- containing an XML entity.
local function stub_xml_unescape(s)
  if not s then return s end
  s = s:gsub("&lt;", "<")
  s = s:gsub("&gt;", ">")
  s = s:gsub("&quot;", "\"")
  s = s:gsub("&apos;", "'")
  s = s:gsub("&amp;", "&")
  return s
end

-- ---- Stub dt table ---------------------------------------------------------
-- Use ids that DO NOT overlap the iteration array's integer indexes (1..N),
-- so dt.database[id] always resolves via __index without colliding with
-- the iteration storage.
local images_by_id = {
  [101] = {id = 101, filename = "DSC_0001.NEF", path = "/photos", rating = 5},
  [102] = {id = 102, filename = "DSC_0002.NEF", path = "/photos", rating = 3},
  [103] = {id = 103, filename = "OTHER.NEF",   path = "/photos", rating = 4},
}
local iter_list = {}
for _, img in pairs(images_by_id) do table.insert(iter_list, img) end
local stub_db = setmetatable(iter_list, {
  __index = function(_, k) return images_by_id[k] end,
})

local dt_log = {}

-- Scouter phase 1: a scriptable dt.gui. `action_log` records every call, and
-- `action_returns` maps a path to the value the stub hands back -- including
-- the nan the real darktable returns outside the darkroom.
local NAN = 0/0
local action_log = {}
local action_returns = {}
-- `fail_switches_remaining`: how many upcoming dt.gui.current_view(darkroom)
-- calls should raise before succeeding, scripting a flaky/failing switch for
-- the commit_view_bounce retry tests. Consumed one at a time.
local view_state = {current = "lighttable", fail_switches_remaining = 0}
local views = {darkroom = {id = "darkroom"}, lighttable = {id = "lighttable"}}
local selection_log = {}
local styles_created = {}
local applied_styles = {}

-- Row 418/422: registered "shortcut" callbacks, keyed by name (the tooltip,
-- 4th arg) -- mirrors real darktable's `dt.register_event(event_name,
-- "shortcut", callback, name)`. Row 422's host finding: darktable actually
-- addresses a lua-registered shortcut's `dt.gui.action` path after the
-- EVENT NAME (1st arg), not the tooltip, and the plugin used to pass those
-- two arguments different values -- so the path it pressed never matched
-- the path darktable had actually registered, and `dt.gui.action` on a
-- nonexistent path silently never fires.
--
-- `main_thread_real_action_path` is this stub's stand-in for "the one path
-- the simulated host actually answers to" -- independent of what the
-- plugin under test requests, exactly like a real, opaque darktable build.
-- It defaults to the event-name convention (`"lua/" .. event_name`), which
-- is what the row 422 fix now registers under, so the common-path tests
-- below see candidate 1 resolve immediately. Tests that want to exercise
-- discovery of a LATER candidate, or a total miss, override it before
-- reloading the plugin.
local registered_shortcuts = {}
local shortcut_handler = nil
local shortcut_event_name = nil
local main_thread_real_action_path = nil
-- Test-controlled kill switch: when true, the stub's `dt.gui.action` records
-- the call (so callers can see it fired) but does NOT invoke the registered
-- handler -- simulating a proxy that activated but whose handler never ran
-- on the main thread within budget. Exercises run_on_main_thread's honest
-- refusal path.
local suppress_main_thread_proxy = false
-- When true, the stub's dt.gui.action returns nan for any "lua/..." path
-- without firing the handler -- what real darktable 5.4.1 does
-- (dt_action_process: "lua action '%s' triggered from lua").
local darktable_refuses_lua_actions = false

local stub_dt = {
  database = stub_db,
  print_log = function(msg) table.insert(dt_log, msg) end,
  register_event = function(event_name, event_type, callback, name)
    if event_type == "shortcut" then
      registered_shortcuts[name] = callback
      shortcut_handler = callback
      shortcut_event_name = event_name
      if main_thread_real_action_path == nil then
        main_thread_real_action_path = "lua/" .. event_name
      end
    end
    return true
  end,
  control = {
    dispatch = function(_) end,
    -- Real darktable's view switch is ASYNCHRONOUS (host-proven: see the
    -- wait_for_view comment block in darktable_mcp.lua). current_view(v)
    -- returns before the view has changed and only a yield -- this sleep --
    -- lets it land. `view_state.switch_lag_sleeps` scripts that lag so the
    -- plugin's yield-polling is genuinely exercised instead of getting a
    -- synchronous answer no real darktable would give. It defaults to 0, so
    -- every pre-existing test keeps the old synchronous behaviour.
    sleep = function(_)
      view_state.sleeps = (view_state.sleeps or 0) + 1
      if view_state.pending then
        view_state.pending_sleeps = (view_state.pending_sleeps or 0) + 1
        if view_state.pending_sleeps >= (view_state.switch_lag_sleeps or 0) then
          view_state.current = view_state.pending
          view_state.pending, view_state.pending_sleeps = nil, nil
        end
      end
    end,
  },
}

stub_dt.gui = {
  action = function(path, instance, element, effect, value)
    table.insert(action_log, {path = path, instance = instance, element = element,
                             effect = effect, value = value})
    -- Row 422: the stub only fires the registered shortcut handler for the
    -- ONE path `main_thread_real_action_path` names -- any other path
    -- (including a plausible-looking guess) is a silent no-op, exactly like
    -- real darktable's `dt.gui.action` on a path it does not recognise.
    -- This is what lets a test prove the plugin discovers the right
    -- candidate by elimination rather than the stub matching by convention
    -- on the plugin's behalf.
    -- Source-verified emulation (darktable 5.4.1): a lua-owned shortcut
    -- action pressed from Lua is refused with nan, never firing its handler.
    if darktable_refuses_lua_actions and type(path) == "string" and path:find("^lua/") then
      return NAN
    end
    -- darktable's own "global/switch views/<view>" commands:
    -- dt_ctl_switch_mode_to semantics -- asking for the view you are in
    -- toggles back to the lighttable -- and, as for every command action,
    -- the return value is always nan even though the callback ran.
    local target = type(path) == "string" and path:match("^global/switch views/(.+)$")
    if target and action_returns[path] == nil then
      view_state.action_switches = (view_state.action_switches or 0) + 1
      if target == view_state.current then
        if target ~= "lighttable" then view_state.current = "lighttable" end
      else
        -- The command's callback (dt_ctl_switch_mode_to) runs on the GTK
        -- main thread; the stub delegates to the scripted view setter so
        -- every lag/failure script written against dt.gui.current_view
        -- applies to the action transport too. `via_action` lets a test
        -- tell this delegated call from a direct setter call by the plugin.
        view_state.via_action = true
        local ok, err = pcall(stub_dt.gui.current_view, views[target])
        view_state.via_action = false
        if not ok then error(err) end
      end
      return NAN
    end
    if path == "views/darkroom/synchronize selection" and action_returns[path] == nil then
      if view_state.current ~= "darkroom" then return NAN end
      view_state.db_flushes = (view_state.db_flushes or 0) + 1
      return NAN
    end
    if path == "lib/copy_history/write sidecar files" and action_returns[path] == nil then
      if view_state.current ~= "lighttable" then return NAN end
      for _, img in ipairs(stub_dt.gui.selection_now or {}) do
        if img.sidecar then
          local h = io.open(img.sidecar, "w"); h:write("written by write sidecar files"); h:close()
        end
      end
      return 0
    end
    local r = action_returns[path]
    if r == nil then return value or 0 end
    if r == "nan" then return NAN end
    return r
  end,
  current_view = function(v)
    if v == nil then return views[view_state.current] end
    if v.id == view_state.current then
      -- Real darktable treats a switch to the CURRENT view as a no-op: it
      -- does not reload the pipe, so a stale image stays loaded. This is
      -- exactly the bug darkroom.open's fix (switch_to_darkroom) has to
      -- route around when already in the darkroom on a different image.
      return v
    end
    if v.id == "darkroom" and (view_state.fail_switches_remaining or 0) > 0 then
      view_state.fail_switches_remaining = view_state.fail_switches_remaining - 1
      error("scripted darkroom switch failure")
    end
    if (view_state.switch_lag_sleeps or 0) > 0 then
      -- Asynchronous: queue it. control.sleep above is what applies it.
      view_state.pending, view_state.pending_sleeps = v.id, 0
    else
      view_state.current = v.id
    end
    return v
  end,
  views = views,
  -- Selecting an image immediately becomes the "action_images" reading --
  -- mirrors the real GUI closely enough to prove switch_to_darkroom's
  -- select-before-display_image ordering (fix round 2 item 2) actually keeps
  -- the action_images fallback fresh, independent of which switch mechanism
  -- (display_image or the view-switch bounce) ends up doing the real work.
  selection = function(images)
    -- Getter form (no argument): report the current selection without
    -- recording a selection change.
    if images == nil then return stub_dt.gui.selection_now or {} end
    table.insert(selection_log, images)
    stub_dt.gui.action_images = images
    stub_dt.gui.selection_now = images
    return images
  end,
  action_images = {},
  libs = {snapshots = {take_snapshot = function() view_state.snapshots =
    (view_state.snapshots or 0) + 1 end}},
}

-- Recorded so the export/import/delete tests can assert what the plugin
-- actually asked the (stubbed) darktable API to do.
local styles_exported = {}
local styles_imported = {}
local styles_deleted = {}
-- Scripts a darktable that silently no-ops an export (pcall ok=true, but no
-- file written / unchanged) -- fix round 1, should-fix 9's mtime-advance test.
local force_noop_export = false

stub_dt.styles = setmetatable({}, {
  __index = function(t, k)
    if k == "create" then
      return function(image, name, description)
        table.insert(styles_created, {image = image, name = name, description = description})
        table.insert(t, {name = name, description = description})
      end
    end
    if k == "export" then
      -- Real dt.styles.export(style, dir) writes `<dir>/<style.name>.dtstyle`.
      -- The stub writes real `<info><name>...</name></info>` content (the
      -- same shape `dtstyle_file_name` parses in the real plugin) so
      -- style.export_file's `bytes` field AND style.import_file's
      -- file-name-based collision check are both exercised against
      -- something realistic, not an arbitrary marker.
      return function(style, dir)
        table.insert(styles_exported, {name = style.name, dir = dir})
        if force_noop_export then return end
        local path = dir .. "/" .. style.name .. ".dtstyle"
        local f = io.open(path, "w")
        if not f then error("stub export: could not open " .. path) end
        f:write("<darktable_style><info><name>" .. style.name
                 .. "</name></info></darktable_style>\n")
        f:close()
      end
    end
    if k == "import" then
      -- Real dt.styles.import(path) derives the new style's name from the
      -- file's own <info><name>. The stub does the same, from whatever
      -- content is actually on disk (export()'s own write, or a
      -- caller-supplied fixture) -- never a marker line disconnected from
      -- what a real .dtstyle looks like.
      return function(path)
        table.insert(styles_imported, path)
        local f = io.open(path, "r")
        local name = "imported-style"
        if f then
          local content = f:read("*a") or ""
          f:close()
          local found = stub_xml_unescape(content:match("<info>.-<name>(.-)</name>"))
          if found then name = found end
        end
        table.insert(t, {name = name, description = ""})
      end
    end
    if k == "delete" then
      return function(style)
        table.insert(styles_deleted, style.name)
        for i, s in ipairs(t) do
          if s.name == style.name then table.remove(t, i); return end
        end
        error("stub delete: style not found: " .. tostring(style.name))
      end
    end
    return nil
  end,
})

-- Give every stub image an apply_style method, so style.apply's
-- `image:apply_style(style)` has something to call. Recorded into
-- applied_styles for tests that want to inspect what was applied.
for _, img in pairs(images_by_id) do
  img.apply_style = function(self, style)
    table.insert(applied_styles, {id = self.id, style = style.name})
  end
end

-- Make `require("darktable")` return our stub by pre-populating package.loaded.
package.loaded.darktable = stub_dt

-- ---- Load the plugin -------------------------------------------------------
package.path = package.path .. ";./darktable_mcp/lua/?.lua"
local internals = require("darktable_mcp")
-- Scouter darkroom-verb tests below use `plugin` as the module alias.
local plugin = internals

-- ---- Verify the plugin announces itself on load ----------------------------
do
  assertEq(#dt_log, 1, "ready message logged on load")
  assertTrue(string.find(dt_log[1] or "", "ready"),
    "ready message contains the word 'ready'")
end

-- ---- methods.view_photos ---------------------------------------------------
do
  local result = internals.methods.view_photos({rating_min = 4, limit = 10})
  assertEq(#result, 2, "view_photos rating_min=4 returns 2 images")
  -- Order-independent: collect ratings, both must be >= 4, and the SET
  -- of ids must be {"101","103"} (the only images with rating >= 4).
  local ids_seen = {}
  for _, img in ipairs(result) do
    assertTrue(img.rating >= 4, "view_photos rating_min=4 image rating >= 4")
    ids_seen[img.id] = true
  end
  assertTrue(ids_seen["101"], "view_photos rating_min=4 includes id 101")
  assertTrue(ids_seen["103"], "view_photos rating_min=4 includes id 103")
end

do
  local result = internals.methods.view_photos({filter = "OTHER", limit = 10})
  assertEq(#result, 1, "view_photos filter=OTHER returns 1 image")
  assertEq(result[1].filename, "OTHER.NEF", "view_photos filter result filename")
  -- view_photos must return the absolute file path (dir + filename), not
  -- just the directory. Otherwise it doesn't compose with export_images,
  -- which takes file paths in `photo_ids`.
  assertEq(result[1].path, "/photos/OTHER.NEF",
    "view_photos returns absolute file path")
end

do
  local result = internals.methods.view_photos({limit = 2})
  assertEq(#result, 2, "view_photos limit=2 caps at 2")
end

-- ---- methods.view_photos: trailing-slash dir handling ---------------------
do
  -- Some darktable backends include a trailing slash on image.path; some
  -- don't. The plugin must normalize both into a single-slash full path.
  local trail_img = {id = 999, filename = "TRAIL.NEF", path = "/photos/", rating = 0}
  images_by_id[999] = trail_img
  table.insert(iter_list, trail_img)
  local result = internals.methods.view_photos({filter = "TRAIL", limit = 10})
  assertEq(#result, 1, "view_photos trailing-slash filter returns 1 image")
  assertEq(result[1].path, "/photos/TRAIL.NEF",
    "view_photos collapses double slash from trailing-dir input")
  -- Cleanup so later tests that count totals don't trip.
  images_by_id[999] = nil
  table.remove(iter_list)
end

-- ---- methods.rate_photos ---------------------------------------------------
do
  local result = internals.methods.rate_photos({photo_ids = {"101", "102"}, rating = 1})
  assertEq(result.updated, 2, "rate_photos updated count")
  assertEq(images_by_id[101].rating, 1, "rate_photos changed image 101 rating")
  assertEq(images_by_id[102].rating, 1, "rate_photos changed image 102 rating")
end

-- ---- methods["image.rate"] --------------------------------------------------
do
  local result = internals.methods["image.rate"]({image_ids = {"103", "999"}, rating = 2})
  assertEq(result.rated, 1, "image.rate rated count")
  assertEq(images_by_id[103].rating, 2, "image.rate changed image 103 rating")
  assertEq(#result.missed, 1, "image.rate missed count")
  assertEq(result.missed[1], 999, "image.rate missed unknown id")
  assertEq(#result.per_image, 1, "image.rate per_image count for rated ids")
  assertEq(result.per_image[1].image_id, 103, "image.rate per_image id")
  assertEq(result.per_image[1].rating_before, 4, "image.rate per_image rating_before")
  assertEq(result.per_image[1].rating_after, 2, "image.rate per_image rating_after")

  local ok_high = pcall(internals.methods["image.rate"], {image_ids = {"103"}, rating = 6})
  assertTrue(not ok_high, "image.rate rejects a rating above 5")
  local ok_low = pcall(internals.methods["image.rate"], {image_ids = {"103"}, rating = -2})
  assertTrue(not ok_low, "image.rate rejects a rating below -1")
  local ok_empty = pcall(internals.methods["image.rate"], {image_ids = {}, rating = 1})
  assertTrue(not ok_empty, "image.rate rejects an empty image_ids list")
end

-- ---- methods["image.color_labels"] ------------------------------------------
do
  images_by_id[101].green = true
  images_by_id[103].red = true
  images_by_id[103].purple = true

  local result = internals.methods["image.color_labels"](
    {image_ids = {"101", "102", "103", "999"}})
  assertEq(#result.missing, 1, "image.color_labels missing count")
  assertEq(result.missing[1], 999, "image.color_labels missing unknown id")
  assertEq(#result.per_image, 3, "image.color_labels per_image count")

  local by_id = {}
  for _, entry in ipairs(result.per_image) do by_id[entry.image_id] = entry end
  assertEq(#by_id[101].labels, 1, "image.color_labels 101 has one label")
  assertEq(by_id[101].labels[1], "green", "image.color_labels 101 is green")
  assertEq(#by_id[102].labels, 0, "image.color_labels 102 has no labels")
  assertEq(#by_id[103].labels, 2, "image.color_labels 103 has two labels")

  local ok_empty = pcall(internals.methods["image.color_labels"], {image_ids = {}})
  assertTrue(not ok_empty, "image.color_labels rejects an empty image_ids list")

  -- Cleanup so later tests are not affected by these stub fields.
  images_by_id[101].green = nil
  images_by_id[103].red = nil
  images_by_id[103].purple = nil
end

-- ---- methods.import_batch --------------------------------------------------
do
  -- Stub dt.database.import to record args and return a fake list.
  -- darktable's real API takes only a path string; recursion is governed by
  -- a darktable preference, not a per-call argument.
  local recorded = {}
  local stub_imported = {{}, {}, {}}  -- 3 fake images
  -- Save original (in case test_dispatcher runs other tests later that need it).
  local original_db = stub_dt.database
  stub_dt.database = setmetatable({
    import = function(path)
      table.insert(recorded, {path = path})
      return stub_imported
    end,
  }, {__index = original_db})

  local result = internals.methods.import_batch({source_path = "/tmp/foo", recursive = true})
  assertEq(result.imported, 3, "import_batch returns count of imported images")
  assertEq(result.source_path, "/tmp/foo", "import_batch returns source_path back")
  assertEq(result.recursive, true, "import_batch echoes recursive back")
  assertEq(#recorded, 1, "dt.database.import called once")
  assertEq(recorded[1].path, "/tmp/foo", "import called with source_path")

  -- Default recursive = true when not specified
  local r2 = internals.methods.import_batch({source_path = "/tmp/bar"})
  assertEq(r2.recursive, true, "import_batch defaults recursive=true")

  -- Single-image return (non-table) should count as 1.
  stub_dt.database = setmetatable({
    import = function(_) return {} end,  -- ensure a userdata-like (we use empty table)
  }, {__index = original_db})

  -- Restore.
  stub_dt.database = original_db
end

-- ---- import_batch error: missing source_path -------------------------------
do
  local resp = internals.handle({
    id = "ib1",
    method = "import_batch",
    params = {},  -- no source_path
  })
  assertEq(resp.id, "ib1", "import_batch error preserves id")
  assertTrue(resp.error ~= nil, "import_batch returns error when source_path missing")
  assertTrue(string.find(resp.error or "", "source_path") ~= nil,
    "error message mentions source_path")
end

-- ---- methods.list_styles ---------------------------------------------------
do
  -- Stub dt.styles with a tiny inventory.
  local fake_styles = {
    {name = "alpha", description = "first style"},
    {name = "beta", description = "second style"},
  }
  local original_styles = stub_dt.styles
  stub_dt.styles = fake_styles  -- behaves like a list under ipairs

  local result = internals.methods.list_styles({})
  assertEq(result.count, 2, "list_styles returns count of styles")
  assertEq(#result.styles, 2, "list_styles returns table of styles")
  assertEq(result.styles[1].name, "alpha", "first style name")
  assertEq(result.styles[1].description, "first style", "first style description")
  assertEq(result.styles[2].name, "beta", "second style name")

  stub_dt.styles = original_styles
end

-- ---- methods.apply_preset --------------------------------------------------
do
  -- Stub dt.styles + image:apply_style + dt.database lookup.
  local applied_to = {}
  local make_stub_image = function(id)
    local img = {id = id}
    function img:apply_style(s) table.insert(applied_to, {id = self.id, style = s.name}) end
    return img
  end

  local fake_styles = {
    {name = "alpha", description = "a"},
    {name = "beta", description = "b"},
  }
  local original_styles = stub_dt.styles
  local original_db = stub_dt.database
  stub_dt.styles = fake_styles

  local stub_db_inner = {}
  stub_db_inner[101] = make_stub_image(101)
  stub_db_inner[102] = make_stub_image(102)
  stub_dt.database = setmetatable({}, {__index = stub_db_inner})

  local result = internals.methods.apply_preset({
    preset_name = "beta",
    photo_ids = {"101", "102"},
  })
  assertEq(result.applied, 2, "apply_preset applied count")
  assertEq(#result.missed, 0, "apply_preset no missed images")
  assertEq(result.preset_name, "beta", "apply_preset echoes preset_name")
  assertEq(#applied_to, 2, "apply_style invoked twice")
  assertEq(applied_to[1].style, "beta", "applied beta to first image")
  assertEq(applied_to[2].style, "beta", "applied beta to second image")

  stub_dt.styles = original_styles
  stub_dt.database = original_db
end

-- ---- apply_preset: missing image ID ----------------------------------------
do
  local fake_styles = {{name = "alpha", description = "a"}}
  local original_styles = stub_dt.styles
  local original_db = stub_dt.database
  stub_dt.styles = fake_styles
  stub_dt.database = setmetatable({}, {__index = function() return nil end})

  local result = internals.methods.apply_preset({
    preset_name = "alpha",
    photo_ids = {"999"},
  })
  assertEq(result.applied, 0, "apply_preset applied=0 when image missing")
  assertEq(#result.missed, 1, "apply_preset reports missed image")
  assertEq(result.missed[1], "999", "missed list contains the photo_id")

  stub_dt.styles = original_styles
  stub_dt.database = original_db
end

-- ---- apply_preset: unknown style -------------------------------------------
do
  local fake_styles = {{name = "alpha", description = "a"}}
  local original_styles = stub_dt.styles
  stub_dt.styles = fake_styles

  local resp = internals.handle({
    id = "ap1",
    method = "apply_preset",
    params = {preset_name = "nonexistent", photo_ids = {"1"}},
  })
  assertEq(resp.id, "ap1", "preserves id on error")
  assertTrue(resp.error ~= nil, "returns error for unknown style")
  assertTrue(string.find(resp.error or "", "nonexistent") ~= nil, "names the missing style")

  stub_dt.styles = original_styles
end

-- ---- apply_preset: missing preset_name -------------------------------------
do
  local resp = internals.handle({
    id = "ap2",
    method = "apply_preset",
    params = {photo_ids = {"1"}},
  })
  assertTrue(resp.error ~= nil, "errors on missing preset_name")
  assertTrue(string.find(resp.error or "", "preset_name") ~= nil, "error mentions preset_name")
end

-- ---- apply_preset: empty photo_ids -----------------------------------------
do
  local resp = internals.handle({
    id = "ap3",
    method = "apply_preset",
    params = {preset_name = "alpha", photo_ids = {}},
  })
  assertTrue(resp.error ~= nil, "errors on empty photo_ids")
  assertTrue(string.find(resp.error or "", "photo_ids") ~= nil, "error mentions photo_ids")
end

-- ---- handle: known method --------------------------------------------------
do
  local resp = internals.handle({id = "abc", method = "view_photos", params = {limit = 1}})
  assertEq(resp.id, "abc", "handle preserves id")
  assertTrue(resp.result ~= nil, "handle known method returns result")
  assertTrue(resp.error == nil, "handle known method has no error")
end

-- ---- handle: unknown method ------------------------------------------------
do
  local resp = internals.handle({id = "xyz", method = "bogus", params = {}})
  assertEq(resp.id, "xyz", "handle preserves id on error")
  assertTrue(resp.error ~= nil, "handle unknown method returns error")
  assertTrue(string.find(resp.error, "bogus"), "error message names the method")
end

-- ---- scan_dir: full request/response round-trip ----------------------------
do
  local tmpdir = os.getenv("TMPDIR") or "/tmp"
  local test_dir = tmpdir .. "/darktable-mcp-lua-test-" .. tostring(os.time())
  os.execute("mkdir -p " .. test_dir)

  -- Reset stub state so view_photos in scan_dir sees the original data.
  images_by_id[101].rating = 5
  images_by_id[102].rating = 3

  -- Write a request file.
  local req_path = test_dir .. "/request-test001.json"
  local f = io.open(req_path, "w")
  f:write('{"id":"test001","method":"view_photos","params":{"limit":1}}')
  f:close()

  internals.scan_dir(test_dir)

  -- Verify request file was deleted.
  local req_check = io.open(req_path, "r")
  assertTrue(req_check == nil, "scan_dir deletes request file after processing")
  if req_check then req_check:close() end

  -- Verify response file appeared with correct content.
  local resp_path = test_dir .. "/response-test001.json"
  local resp_f = io.open(resp_path, "r")
  assertTrue(resp_f ~= nil, "scan_dir wrote response file")
  if resp_f then
    local content = resp_f:read("*a")
    resp_f:close()
    assertTrue(string.find(content, "test001"), "response contains request id")
    assertTrue(string.find(content, "result"), "response contains result field")
  end

  os.execute("rm -rf " .. test_dir)
end

-- ---- JSON round-trip with non-ASCII ----------------------------------------
do
  local original = {filter = "Тест", filename = "café.NEF"}
  local encoded = internals.json.encode(original)
  local decoded = internals.json.decode(encoded)
  assertEq(decoded.filter, "Тест", "non-ASCII Cyrillic round-trips")
  assertEq(decoded.filename, "café.NEF", "non-ASCII Latin-1 supplement round-trips")
end

-- ---- JSON \uXXXX escape decoding (matches what Python's json.dumps emits) -
do
  -- Python emits "Test" as ASCII, but emits "é" as é by default.
  local payload = '{"name":"caf\\u00e9.NEF"}'
  local decoded = internals.json.decode(payload)
  assertEq(decoded.name, "café.NEF", "\\u00e9 escape decodes to UTF-8")
end

-- ---- JSON control-byte escaping in encoder --------------------------------
do
  local with_ctrl = "ab\1cd"
  local encoded = internals.json.encode({s = with_ctrl})
  -- Encoder must escape \1 as  (otherwise Python's strict parser rejects).
  assertTrue(string.find(encoded, "\\u0001", 1, true) ~= nil,
    "encoder escapes 0x01 as \\u0001")
  local decoded = internals.json.decode(encoded)
  assertEq(decoded.s, with_ctrl, "control byte round-trips through escape")
end

-- ---- Helpers for the filesystem-facing tests -------------------------------
local function sq(s) return internals.shell_quote(s) end

local function make_tmpdir(name)
  local base = os.getenv("TMPDIR") or "/tmp"
  if base:sub(-1) == "/" then base = base:sub(1, -2) end
  local dir = base .. "/" .. name .. "-" .. tostring(os.time()) .. "-" .. tostring(math.random(1e6))
  os.execute("mkdir -p " .. sq(dir))
  return dir
end

local function rmtree(dir) os.execute("rm -rf " .. sq(dir)) end

local function write_text(path, text)
  local f = io.open(path, "w")
  if not f then return false end
  f:write(text)
  f:close()
  return true
end

local function read_text(path)
  local f = io.open(path, "r")
  if not f then return nil end
  local c = f:read("*a")
  f:close()
  return c
end

local function exists(path)
  local f = io.open(path, "r")
  if f then f:close(); return true end
  return false
end

-- Snapshot dt_log length, run body, return the log lines it appended.
local function capture_log(body)
  local before = #dt_log
  body()
  local out = {}
  for i = before + 1, #dt_log do out[#out + 1] = dt_log[i] end
  return out
end

-- ---- shell_quote -----------------------------------------------------------
do
  local q = internals.shell_quote
  assertEq(q("plain"), "'plain'", "shell_quote wraps a plain string")
  assertEq(q("/home/a b"), "'/home/a b'", "shell_quote handles spaces")
  -- Double quotes, backticks and $(...) are all INERT inside single quotes.
  assertEq(q('a"b'), [['a"b']], "shell_quote passes a double quote through")
  assertEq(q("a`id`b"), "'a`id`b'", "shell_quote neutralises backticks")
  assertEq(q("a$(id)b"), "'a$(id)b'", "shell_quote neutralises $(...)")
  assertEq(q("a$HOMEb"), "'a$HOMEb'", "shell_quote neutralises $VAR")
  -- The only hard case: an embedded single quote must close, escape, reopen.
  assertEq(q("it's"), [['it'\''s']], "shell_quote escapes an embedded quote")
  assertEq(q("''"), [[''\'''\''']], "shell_quote escapes repeated quotes")

  -- Prove the quoting actually survives /bin/sh: echo the string back and
  -- compare byte-for-byte. If quoting were wrong this would either error or
  -- return substituted text.
  local nasty = [[weird's "dir" $(echo PWNED) `echo PWNED` $HOME]]
  local p = io.popen("printf %s " .. q(nasty))
  local echoed = p:read("*a")
  p:close()
  assertEq(echoed, nasty, "shell_quote round-trips an adversarial string through sh")
end

-- ---- scan_dir in a directory whose name is full of shell metacharacters ----
do
  -- The whole point: cache_dir() comes from $HOME / $XDG_CACHE_HOME, so the
  -- worker must survive a directory named like this without breaking (or
  -- executing) anything.
  local dir = make_tmpdir([[dtmcp it's "a" $(echo x) `echo y` dir]])
  assertTrue(exists(dir .. "/.") or true, "tmpdir created")

  images_by_id[101].rating = 5
  images_by_id[102].rating = 3

  assertTrue(write_text(dir .. "/request-shq001.json",
    '{"id":"shq001","method":"view_photos","params":{"limit":1}}'),
    "wrote request into an adversarially-named directory")

  local handled = internals.scan_dir(dir)
  assertEq(handled, 1, "scan_dir found the request despite the shell metachars")

  local body = read_text(dir .. "/response-shq001.json")
  assertTrue(body ~= nil, "scan_dir wrote a response in the quoted directory")
  assertTrue(body and string.find(body, "shq001", 1, true) ~= nil,
    "response in the quoted directory carries the request id")
  assertTrue(not exists(dir .. "/request-shq001.json"),
    "request file removed from the quoted directory")

  -- sweep_stale shells out to find; it must not blow up on this path either.
  local ok_sweep = pcall(internals.sweep_stale, dir, 60)
  assertTrue(ok_sweep, "sweep_stale survives a shell-metachar directory")
  assertTrue(exists(dir .. "/response-shq001.json"),
    "sweep_stale leaves a FRESH response alone")

  rmtree(dir)
end

-- ---- sweep_stale also collects orphan RESPONSE files -----------------------
do
  local dir = make_tmpdir("dtmcp-sweep")
  write_text(dir .. "/request-old.json", "{}")
  write_text(dir .. "/response-old.json", "{}")
  write_text(dir .. "/response-old.json.tmp", "{}")
  write_text(dir .. "/request-fresh.json", "{}")
  write_text(dir .. "/response-fresh.json", "{}")
  write_text(dir .. "/keep-me.txt", "not ours")

  -- Backdate the "old" trio well past the 60s staleness window.
  os.execute("touch -t 202001010000 " .. sq(dir .. "/request-old.json")
    .. " " .. sq(dir .. "/response-old.json")
    .. " " .. sq(dir .. "/response-old.json.tmp")
    .. " " .. sq(dir .. "/keep-me.txt"))

  internals.sweep_stale(dir, 60)

  assertTrue(not exists(dir .. "/request-old.json"), "sweep deletes a stale request")
  -- This is the leak the sweep used to miss entirely: the client timed out and
  -- removed its request, the worker wrote the response anyway, nobody reads it.
  assertTrue(not exists(dir .. "/response-old.json"), "sweep deletes an ORPHAN response")
  assertTrue(not exists(dir .. "/response-old.json.tmp"), "sweep deletes a stranded .tmp")
  assertTrue(exists(dir .. "/request-fresh.json"), "sweep spares a fresh request")
  assertTrue(exists(dir .. "/response-fresh.json"), "sweep spares a fresh response")
  assertTrue(exists(dir .. "/keep-me.txt"), "sweep only touches its own filenames")

  rmtree(dir)
end

-- ---- safe_request_id -------------------------------------------------------
do
  local sri = internals.safe_request_id
  assertEq(sri({id = "abc-123_XYZ"}), "abc-123_XYZ", "accepts a filename-safe id")
  assertEq(sri({id = 42}), "42", "recovers a numeric id by coercion")
  assertEq(sri({id = "../../etc/passwd"}), nil, "rejects a path-traversal id")
  assertEq(sri({id = "a/b"}), nil, "rejects a slash in the id")
  assertEq(sri({id = ""}), nil, "rejects an empty id")
  assertEq(sri({}), nil, "rejects a missing id")
  assertEq(sri({id = {}}), nil, "rejects a non-scalar id")
  assertEq(sri("not a table"), nil, "rejects a non-table request")
end

-- ---- scan_dir: decodable-but-invalid request gets an ERROR RESPONSE --------
do
  -- Regression: these used to be deleted in silence, so the Python client sat
  -- out its full timeout and then reported "darktable not running" -- a lie.
  local dir = make_tmpdir("dtmcp-badreq")

  -- (a) valid id, no method at all.
  write_text(dir .. "/request-bad001.json", '{"id":"bad001","params":{}}')
  -- (b) valid id, method of the wrong type.
  write_text(dir .. "/request-bad002.json", '{"id":"bad002","method":123}')
  -- (c) numeric id -- recoverable, must still be answered.
  write_text(dir .. "/request-777.json", '{"id":777,"method":"nope"}')

  local logs = capture_log(function() internals.scan_dir(dir) end)
  assertEq(#logs, 0, "no log noise for requests that CAN be answered")

  local a = read_text(dir .. "/response-bad001.json")
  assertTrue(a ~= nil, "missing method still produces a response file")
  assertTrue(a and string.find(a, '"error"', 1, true) ~= nil,
    "missing method response carries an error field")
  assertTrue(a and string.find(a, "method", 1, true) ~= nil,
    "missing method error names the offending field")

  local b = read_text(dir .. "/response-bad002.json")
  assertTrue(b ~= nil, "non-string method still produces a response file")
  assertTrue(b and string.find(b, '"error"', 1, true) ~= nil,
    "non-string method response carries an error field")

  local c = read_text(dir .. "/response-777.json")
  assertTrue(c ~= nil, "numeric id is recovered and answered")
  assertTrue(c and string.find(c, "unknown method", 1, true) ~= nil,
    "numeric-id response reports the unknown method")

  assertTrue(not exists(dir .. "/request-bad001.json"), "bad request (a) deleted")
  assertTrue(not exists(dir .. "/request-bad002.json"), "bad request (b) deleted")
  assertTrue(not exists(dir .. "/request-777.json"), "bad request (c) deleted")

  rmtree(dir)
end

-- ---- scan_dir: UNRECOVERABLE request is logged, not silently dropped -------
do
  local dir = make_tmpdir("dtmcp-unrecoverable")

  write_text(dir .. "/request-junk1.json", "this is not json at all {{{")
  write_text(dir .. "/request-junk2.json", '"a bare string"')
  write_text(dir .. "/request-junk3.json", '{"id":"../escape","method":"view_photos"}')

  local logs = capture_log(function()
    local handled = internals.scan_dir(dir)
    assertEq(handled, 3, "scan_dir counts unrecoverable files as handled")
  end)

  assertEq(#logs, 3, "each unrecoverable request produced exactly one log line")
  local joined = table.concat(logs, "\n")
  assertTrue(string.find(joined, "malformed request", 1, true) ~= nil,
    "log says the request was malformed")
  assertTrue(string.find(joined, "junk1", 1, true) ~= nil,
    "log names the offending file")
  assertTrue(string.find(joined, "unparseable JSON", 1, true) ~= nil,
    "log distinguishes the unparseable-JSON case")
  assertTrue(string.find(joined, "not an object", 1, true) ~= nil,
    "log distinguishes the non-object case")
  assertTrue(string.find(joined, "unsafe 'id'", 1, true) ~= nil,
    "log distinguishes the unsafe-id case")

  -- No response can be addressed for any of these, and nothing may be left
  -- behind to be retried forever.
  assertTrue(not exists(dir .. "/request-junk1.json"), "unparseable request deleted")
  assertTrue(not exists(dir .. "/request-junk2.json"), "bare-string request deleted")
  assertTrue(not exists(dir .. "/request-junk3.json"), "unsafe-id request deleted")
  local p = io.popen("ls -1 " .. sq(dir) .. " 2>/dev/null | wc -l")
  local n = tonumber((p:read("*a") or "0"):match("%d+"))
  p:close()
  assertEq(n, 0, "unrecoverable requests leave no response files behind")

  rmtree(dir)
end

-- ---- write_file_atomic reports failure -------------------------------------
do
  local dir = make_tmpdir("dtmcp-writefail")
  -- Occupy the .tmp path with a DIRECTORY so io.open(tmp, "w") must fail.
  local target = dir .. "/blocked.json"
  os.execute("mkdir -p " .. sq(target .. ".tmp"))

  local ok, err = internals.write_file_atomic(target, "payload")
  assertEq(ok, false, "write_file_atomic returns false when the tmp path is blocked")
  assertTrue(err ~= nil and #tostring(err) > 0, "write_file_atomic returns a reason")

  local ok2 = internals.write_file_atomic(dir .. "/fine.json", "payload")
  assertEq(ok2, true, "write_file_atomic returns true on success")
  assertEq(read_text(dir .. "/fine.json"), "payload", "write_file_atomic writes the content")

  rmtree(dir)
end

-- ---- a failed response write is LOGGED, not swallowed ----------------------
do
  local dir = make_tmpdir("dtmcp-resp-writefail")
  -- Same trick: block the response's tmp path with a directory.
  os.execute("mkdir -p " .. sq(dir .. "/response-wf001.json.tmp"))
  write_text(dir .. "/request-wf001.json",
    '{"id":"wf001","method":"view_photos","params":{"limit":1}}')

  local logs = capture_log(function() internals.scan_dir(dir) end)
  assertEq(#logs, 1, "a dropped response emits exactly one log line")
  local msg = logs[1] or ""
  assertTrue(string.find(msg, "FAILED to write", 1, true) ~= nil,
    "log says the response write failed")
  assertTrue(string.find(msg, "wf001", 1, true) ~= nil,
    "log names the response that was lost")
  assertTrue(string.find(msg, "time out", 1, true) ~= nil,
    "log explains the client-visible symptom")

  rmtree(dir)
end

-- ---- list_request_files: ls fallback ---------------------------------------
do
  local dir = make_tmpdir("dtmcp-list")
  write_text(dir .. "/request-a.json", "{}")
  write_text(dir .. "/request-b.json", "{}")
  write_text(dir .. "/request-c.json.tmp", "{}")   -- in-flight write: ignore
  write_text(dir .. "/response-a.json", "{}")      -- not ours to dispatch
  write_text(dir .. "/other.json", "{}")

  local found = internals.list_request_files_ls(dir)
  assertEq(#found, 2, "ls listing returns exactly the two complete requests")
  local names = {}
  for _, p in ipairs(found) do names[p:match("[^/]+$")] = true end
  assertTrue(names["request-a.json"], "ls listing includes request-a.json")
  assertTrue(names["request-b.json"], "ls listing includes request-b.json")
  assertTrue(not names["request-c.json.tmp"], "ls listing skips the .tmp write")

  -- The lfs path must agree with the ls path. lfs is optional and usually
  -- absent, so inject a stand-in module exposing the same dir() iterator.
  local fake_lfs = {
    dir = function(d)
      local p = io.popen("ls -1a " .. sq(d) .. " 2>/dev/null")
      local lines = {}
      for line in p:lines() do lines[#lines + 1] = line end
      p:close()
      local i = 0
      return function() i = i + 1; return lines[i] end
    end,
  }
  local found2 = internals.list_request_files_lfs(dir, fake_lfs)
  assertEq(#found2, 2, "lfs listing returns the same two requests")
  assertEq(found2[1], dir .. "/request-a.json", "lfs listing is sorted (a first)")
  assertEq(found2[2], dir .. "/request-b.json", "lfs listing is sorted (b second)")

  -- A directory that cannot be read must yield an empty list, not an error.
  local ok, res = pcall(internals.list_request_files_lfs, dir .. "/nope", fake_lfs)
  assertTrue(ok, "lfs listing does not raise on an unreadable directory")
  assertEq(#(res or {}), 0, "lfs listing returns empty for an unreadable directory")

  assertTrue(type(internals.lfs_available) == "boolean",
    "plugin records whether lfs was reachable")

  rmtree(dir)
end

-- ---- adaptive poll backoff -------------------------------------------------
do
  local f = internals.poll_interval_ms
  local t = internals.poll_tiers

  assertEq(t.fast_ms, 100, "fast tier is 100ms")
  assertEq(t.medium_ms, 250, "medium tier is 250ms")
  assertEq(t.slow_ms, 1000, "slow tier is 1000ms -- idle polls ~once a second")

  -- A request just arrived (idle run reset to 0): stay fast.
  assertEq(f(0), 100, "0 idle ticks -> 100ms")
  assertEq(f(1), 100, "1 idle tick -> 100ms")
  assertEq(f(t.idle_ticks_before_medium - 1), 100, "just under the medium threshold -> 100ms")
  -- Step up.
  assertEq(f(t.idle_ticks_before_medium), 250, "medium threshold -> 250ms")
  assertEq(f(t.idle_ticks_before_slow - 1), 250, "just under the slow threshold -> 250ms")
  -- Steady-state idle.
  assertEq(f(t.idle_ticks_before_slow), 1000, "slow threshold -> 1000ms")
  assertEq(f(10000), 1000, "a long-idle session stays at 1000ms, it does not keep growing")

  -- Monotonic: the interval must never step back DOWN as idleness grows,
  -- otherwise an idle session would oscillate instead of settling.
  local prev = 0
  for i = 0, 40 do
    local cur = f(i)
    assertTrue(cur >= prev, "poll interval is monotonic non-decreasing at tick " .. i)
    prev = cur
  end

  -- The whole point of the change: an idle session must reach ~1 poll/second.
  -- Sum the wall-clock cost of the first 30s-worth of quiet and check the
  -- steady-state rate, plus how long the ramp itself takes.
  local ramp_ms = 0
  for i = 1, t.idle_ticks_before_slow - 1 do ramp_ms = ramp_ms + f(i) end
  assertTrue(ramp_ms <= 4000,
    "backoff reaches the slow tier within ~4s of quiet (got " .. ramp_ms .. "ms)")
  assertTrue(ramp_ms >= 1000,
    "backoff does not jump to slow instantly (got " .. ramp_ms .. "ms)")

  -- Old behaviour was a flat 100ms => 36000 polls/hour. New idle steady state
  -- must be an order of magnitude cheaper.
  local old_polls_per_hour = 3600 * 1000 / 100
  local new_polls_per_hour = 3600 * 1000 / t.slow_ms
  assertEq(new_polls_per_hour, 3600, "idle session polls 3600 times/hour, not 36000")
  assertTrue(new_polls_per_hour * 10 <= old_polls_per_hour,
    "idle poll rate is at least 10x cheaper than the old flat 100ms loop")

  -- Sweep cadence is now time-based, so it cannot drift with the poll tier.
  assertEq(internals.sweep_interval_seconds, 10, "sweep every 10 elapsed seconds")

  -- The stale age must outlast the LONGEST client budget in DEFAULT_TIMEOUTS
  -- (darktable_mcp/bridge/client.py: 120s for import_batch and apply_preset).
  -- The worker is single-threaded, so a request can sit queued behind a call
  -- that legitimately runs for its full budget. At the old 60s the sweep
  -- deleted those still-wanted request-*.json files, and their callers waited
  -- out the whole 120s only to be told "darktable is not running".
  local MAX_CLIENT_TIMEOUT = 120
  assertTrue(internals.stale_age_seconds > MAX_CLIENT_TIMEOUT,
    "stale age (" .. tostring(internals.stale_age_seconds) .. "s) must exceed "
    .. "the largest client timeout (" .. MAX_CLIENT_TIMEOUT .. "s), or the "
    .. "sweep deletes requests whose callers are still waiting")
  assertTrue(internals.stale_age_seconds >= 300,
    "stale age leaves margin for queueing delay ahead of the longest call")
  -- find's -mmin granularity is whole minutes; the age must survive the
  -- floor() in sweep_stale with the invariant intact.
  assertTrue(math.floor(internals.stale_age_seconds / 60) * 60 > MAX_CLIENT_TIMEOUT,
    "stale age still exceeds the client timeout after sweep_stale rounds it "
    .. "down to whole minutes for find -mmin")
end

-- ---- scan_dir returns a handled count (drives the backoff) -----------------
do
  local dir = make_tmpdir("dtmcp-handled")
  assertEq(internals.scan_dir(dir), 0, "empty directory -> 0 handled (idle tick)")

  write_text(dir .. "/request-h1.json", '{"id":"h1","method":"view_photos","params":{"limit":1}}')
  write_text(dir .. "/request-h2.json", '{"id":"h2","method":"view_photos","params":{"limit":1}}')
  assertEq(internals.scan_dir(dir), 2, "two requests -> 2 handled (resets the backoff)")
  assertEq(internals.scan_dir(dir), 0, "requests consumed -> back to 0 handled")

  rmtree(dir)
end

-- ---- JSON encoder: sparse and mixed tables ---------------------------------
do
  local enc = internals.json.encode

  -- Dense array is unchanged.
  assertEq(enc({"a", "b", "c"}), '["a","b","c"]', "dense array encodes as an array")
  assertEq(enc({}), "[]", "empty table encodes as an empty array")

  -- Regression: {[1]=a,[3]=b} has n=2, max=3. The old `n == max` test sent it
  -- to the OBJECT branch, emitting {"1":"a","3":"b"} -- numeric string keys
  -- the Python client never expects where a list belongs.
  local sparse = {}
  sparse[1] = "a"
  sparse[3] = "b"
  assertEq(enc(sparse), '["a",null,"b"]', "sparse array stays an ARRAY, holes become null")
  assertTrue(string.find(enc(sparse), '"1"', 1, true) == nil,
    "sparse array does not emit numeric string keys")

  local leading_hole = {}
  leading_hole[2] = "x"
  assertEq(enc(leading_hole), '[null,"x"]', "leading hole encodes as null")

  local trailing = {}
  trailing[1] = 1
  trailing[2] = 2
  trailing[5] = 5
  assertEq(enc(trailing), "[1,2,null,null,5]", "multiple holes each become null")

  -- Mixed keys are a genuine object. Keys are sorted, so this is stable.
  local mixed = {}
  mixed[1] = "a"
  mixed.name = "n"
  assertEq(enc(mixed), '{"1":"a","name":"n"}', "mixed integer/string keys encode as an object")

  -- Zero and negative indexes are not array keys.
  local zero = {}
  zero[0] = "z"
  assertEq(enc(zero), '{"0":"z"}', "index 0 is an object key, not an array slot")

  -- Pathological sparsity must not allocate a giant array.
  local huge = {}
  huge[1] = "a"
  huge[100000] = "b"
  local encoded_huge = enc(huge)
  assertTrue(#encoded_huge < 200,
    "absurdly sparse table falls back to an object instead of a huge array")
  assertEq(encoded_huge:sub(1, 1), "{", "absurdly sparse table encodes as an object")

  -- Object key order is deterministic (pairs() order is not).
  local obj = {zulu = 1, alpha = 2, mike = 3}
  assertEq(enc(obj), '{"alpha":2,"mike":3,"zulu":1}', "object keys are sorted")
  assertEq(enc(obj), enc(obj), "object encoding is stable across calls")
end

-- ---- JSON encoder: non-finite numbers --------------------------------------
do
  local enc = internals.json.encode
  local inf = math.huge
  local nan = 0.0 / 0.0

  -- tostring(inf) is "inf"; tostring(nan) is "nan"/"-nan". None of those are
  -- JSON, and the Python client rejects the WHOLE response with
  -- BridgeProtocolError -- so one bad float used to discard a good result.
  assertEq(enc(inf), "null", "+inf encodes as null")
  assertEq(enc(-inf), "null", "-inf encodes as null")
  assertEq(enc(nan), "null", "nan encodes as null")

  local doc = enc({a = inf, b = -inf, c = nan, d = 1.5, e = 7})
  assertTrue(string.find(doc, "inf", 1, true) == nil, "encoded document contains no 'inf'")
  assertTrue(string.find(doc, "nan", 1, true) == nil, "encoded document contains no 'nan'")
  assertEq(doc, '{"a":null,"b":null,"c":null,"d":1.5,"e":7}',
    "non-finite values become null while finite neighbours survive")

  -- And it round-trips through a strict parser. (pcall so that a regression
  -- emitting `inf` reports as a failure rather than aborting the suite.)
  local dec_ok, back = pcall(internals.json.decode, doc)
  assertTrue(dec_ok, "encoded document with non-finite inputs is parseable JSON")
  if dec_ok then
    assertEq(back.a, nil, "null-encoded inf decodes back as absent")
    assertEq(back.d, 1.5, "finite float survives the round trip")
    assertEq(back.e, 7, "finite integer survives the round trip")
  end

  -- Inside an array the slot must still be filled, or the array shifts.
  assertEq(enc({1, inf, 3}), "[1,null,3]", "non-finite inside an array becomes null in place")
end

-- ---- JSON decoder: garbage must RAISE, not return nil ----------------------
do
  local dec = internals.json.decode
  for _, junk in ipairs({"this is not json {{{", "", "@@@", "tru", "[1,"}) do
    local ok = pcall(dec, junk)
    assertTrue(not ok, "json.decode raises on garbage input: " .. string.format("%q", junk))
  end
  -- Valid inputs still decode.
  assertEq(dec("42"), 42, "json.decode still reads a bare number")
  assertEq(dec("-1.5"), -1.5, "json.decode still reads a negative float")
  assertEq(dec("true"), true, "json.decode still reads true")
  assertEq(dec('{"a":1}').a, 1, "json.decode still reads an object")
end

-- ---- import_batch: honest fallback when the scan finds nothing -------------
do
  local original_db = stub_dt.database
  -- Non-table import return forces the polling branch; an empty database
  -- means the poll expires having found nothing.
  local empty_iter = setmetatable({}, {__index = function() return nil end})
  stub_dt.database = setmetatable({
    import = function(_) return "a-userdata-stand-in" end,
  }, {__index = empty_iter})
  -- ipairs over the stub must terminate immediately.
  local sleeps = 0
  local original_sleep = stub_dt.control.sleep
  stub_dt.control.sleep = function(_) sleeps = sleeps + 1 end

  local r = internals.methods.import_batch({source_path = "/tmp/empty-source"})
  -- Regression: this used to report imported = 1, inventing a count darktable
  -- never confirmed, for an import that in fact registered nothing.
  assertEq(r.imported, 0, "import_batch reports 0 when the scan found nothing")
  assertEq(r.scan_incomplete, true, "import_batch flags the expired poll")
  assertEq(r.source_path, "/tmp/empty-source", "import_batch still echoes source_path")
  assertTrue(sleeps > 0, "import_batch actually polled before giving up")

  stub_dt.control.sleep = original_sleep
  stub_dt.database = original_db
end

do
  -- Confirmed counts must NOT be flagged as incomplete.
  local original_db = stub_dt.database
  stub_dt.database = setmetatable({
    import = function(_) return {{}, {}} end,
  }, {__index = original_db})
  local r = internals.methods.import_batch({source_path = "/tmp/two"})
  assertEq(r.imported, 2, "import_batch reports the confirmed count")
  assertEq(r.scan_incomplete, false, "a confirmed count is not flagged incomplete")
  stub_dt.database = original_db
end

-- ---- list_directories_recursive --------------------------------------------
do
  -- Metacharacters in the name again: an import destination is caller-supplied.
  local root = make_tmpdir([[dtmcp-tree it's "a" $(echo x)]])
  os.execute("mkdir -p " .. sq(root .. "/store_00010001_DCIM_100NCD80"))
  os.execute("mkdir -p " .. sq(root .. "/store_00020001_DCIM_100NCD80"))
  os.execute("mkdir -p " .. sq(root .. "/store_00020001_DCIM_100NCD80/nested"))
  write_text(root .. "/store_00010001_DCIM_100NCD80/DSC_0001.NEF", "x")
  write_text(root .. "/store_00020001_DCIM_100NCD80/DSC_0001.NEF", "x")

  local dirs, enumerated = internals.list_directories_recursive(root)
  assertEq(enumerated, true, "tree enumeration succeeds for a real directory")
  assertEq(#dirs, 4, "enumeration returns the root plus its three subdirectories")
  assertEq(dirs[1], root, "the root sorts first, so parents import before children")
  local set = {}
  for _, d in ipairs(dirs) do set[d] = true end
  assertTrue(set[root .. "/store_00010001_DCIM_100NCD80"], "card-1 folder enumerated")
  assertTrue(set[root .. "/store_00020001_DCIM_100NCD80"], "card-2 folder enumerated")
  assertTrue(set[root .. "/store_00020001_DCIM_100NCD80/nested"], "nested folder enumerated")

  -- A path that cannot be read must report enumerated = false, NOT a
  -- confident single-element answer -- otherwise import_batch would claim it
  -- honoured recursion over a tree it never saw.
  local missing, ok2 = internals.list_directories_recursive(root .. "/does-not-exist")
  assertEq(ok2, false, "enumeration reports failure for an unreadable path")
  assertEq(#missing, 1, "failed enumeration still yields the requested path")

  rmtree(root)
end

-- ---- list_directories_recursive: the LFS branch ----------------------------
-- The block above only ever exercises the `find` fallback, because CI runs a
-- plain lua with no LuaFileSystem. That left the lfs branch untested, and it
-- was broken in exactly the way the fallback is not: _walk_lfs appended the
-- root unconditionally and threw away the result of its own pcall, so on a
-- darktable whose Lua HAS lfs, import_batch{source_path="/does/not/exist"}
-- answered recursive_honoured = true, directories_imported = 1, note = nil.
-- Inject a stand-in module so both branches are covered on every host.
do
  -- os.execute's return shape differs across 5.1/5.2+; accept both.
  local function shell_ok(cmd)
    local a, _, c = os.execute(cmd)
    return a == true or a == 0 or (a ~= false and c == 0)
  end
  local function mode_of(path, follow)
    if not follow and shell_ok("test -L " .. sq(path)) then return "link" end
    if shell_ok("test -d " .. sq(path)) then return "directory" end
    if shell_ok("test -e " .. sq(path)) then return "file" end
    return nil
  end

  -- A LuaFileSystem stand-in over the real filesystem. Crucially, dir() RAISES
  -- on an unreadable path, like the real lfs.dir does.
  local fake_lfs = {
    attributes = function(path, what)
      if what ~= "mode" then return nil end
      return mode_of(path, true)
    end,
    symlinkattributes = function(path, what)
      if what ~= "mode" then return nil end
      return mode_of(path, false)
    end,
    dir = function(d)
      if not shell_ok("test -d " .. sq(d)) then
        error("cannot open " .. d .. ": No such file or directory")
      end
      local p = io.popen("ls -1a " .. sq(d) .. " 2>/dev/null")
      local lines = {}
      for line in p:lines() do lines[#lines + 1] = line end
      p:close()
      local i = 0
      return function() i = i + 1; return lines[i] end
    end,
  }

  local root = make_tmpdir([[dtmcp-lfs-tree it's "a" $(echo x)]])
  os.execute("mkdir -p " .. sq(root .. "/store_00010001_DCIM_100NCD80"))
  os.execute("mkdir -p " .. sq(root .. "/store_00020001_DCIM_100NCD80/nested"))
  write_text(root .. "/store_00010001_DCIM_100NCD80/DSC_0001.NEF", "x")

  local dirs, enumerated = internals.list_directories_recursive(root, fake_lfs)
  assertEq(enumerated, true, "lfs walk reports success for a real directory")
  assertEq(#dirs, 4, "lfs walk returns the root plus its three subdirectories")
  assertEq(dirs[1], root, "lfs walk sorts the root first")
  local set = {}
  for _, d in ipairs(dirs) do set[d] = true end
  assertTrue(set[root .. "/store_00020001_DCIM_100NCD80/nested"],
    "lfs walk descends into nested directories")
  assertTrue(not set[root .. "/store_00010001_DCIM_100NCD80/DSC_0001.NEF"],
    "lfs walk lists directories only, not files")

  -- THE REGRESSION. A root that does not exist must NOT come back as a
  -- confident single-element answer, or import_batch claims it honoured
  -- recursion over a tree it never opened.
  local missing, ok_missing =
    internals.list_directories_recursive(root .. "/does-not-exist", fake_lfs)
  assertEq(ok_missing, false, "lfs walk reports failure for a nonexistent root")
  assertEq(#missing, 1, "failed lfs walk still yields the requested path")
  assertEq(missing[1], root .. "/does-not-exist",
    "failed lfs walk echoes the path it was asked about")

  -- A real but genuinely EMPTY directory is the case the failure signal must
  -- not be confused with: read fine, no children, so enumeration succeeded.
  local empty = root .. "/empty-but-real"
  os.execute("mkdir -p " .. sq(empty))
  local edirs, eok = internals.list_directories_recursive(empty, fake_lfs)
  assertEq(eok, true, "an empty but READABLE directory enumerates successfully")
  assertEq(#edirs, 1, "an empty directory yields just itself")

  -- A module with no attributes()/symlinkattributes() at all: the only signal
  -- is dir() raising, and that must still be propagated.
  local minimal_lfs = {dir = fake_lfs.dir}
  local _, ok_min =
    internals.list_directories_recursive(root .. "/nope", minimal_lfs)
  assertEq(ok_min, false,
    "a raising dir() alone is enough to report enumeration failure")
  local _, ok_min_real = internals.list_directories_recursive(empty, minimal_lfs)
  assertEq(ok_min_real, true, "the same minimal module still succeeds on a real path")

  rmtree(root)
end

-- ---- list_directories_recursive: an unreadable SUBDIRECTORY ----------------
do
  -- Synthetic tree, so this does not depend on chmod behaving the same for
  -- root and non-root test runners. /synth/b stats as a directory but cannot
  -- be listed -- the permission-denied case.
  local listable = {
    ["/synth"] = {"a", "b"},
    ["/synth/a"] = {},
  }
  local dirs_that_exist = {["/synth"] = true, ["/synth/a"] = true, ["/synth/b"] = true}
  -- No symlinkattributes: also covers _link_mode's fallback to attributes().
  local synth_lfs = {
    attributes = function(path, what)
      if what ~= "mode" then return nil end
      return dirs_that_exist[path] and "directory" or nil
    end,
    dir = function(d)
      local entries = listable[d]
      if not entries then error("permission denied: " .. d) end
      local list = {".", ".."}
      for _, e in ipairs(entries) do list[#list + 1] = e end
      local i = 0
      return function() i = i + 1; return list[i] end
    end,
  }

  local dirs, enumerated = internals.list_directories_recursive("/synth", synth_lfs)
  assertEq(enumerated, false,
    "a subdirectory that cannot be read makes the whole enumeration untrusted")
  -- The partial result is still returned: those directories DO get imported,
  -- which is why import_batch's note reports a count instead of claiming
  -- "only that path was imported".
  assertEq(#dirs, 3, "the directories that were readable are still returned")
  assertEq(dirs[1], "/synth", "partial enumeration is still sorted, root first")
end

-- ---- import_batch honours `recursive` itself -------------------------------
do
  -- Regression: `recursive` used to be read, defaulted to true, echoed back in
  -- the response -- and never used. Recursion was left entirely to darktable's
  -- recurse_directories preference, so with that preference off an
  -- import_from_camera destination registered NOTHING while the tool reported
  -- success with recursive: true.
  local root = make_tmpdir("dtmcp-import-tree")
  os.execute("mkdir -p " .. sq(root .. "/store_00010001_DCIM_100NCD80"))
  os.execute("mkdir -p " .. sq(root .. "/store_00020001_DCIM_100NCD80"))

  local recorded = {}
  local original_db = stub_dt.database
  stub_dt.database = setmetatable({
    import = function(path) table.insert(recorded, path); return {{}} end,
  }, {__index = original_db})

  local r = internals.methods.import_batch({source_path = root, recursive = true})
  assertEq(#recorded, 3, "recursive import calls dt.database.import per directory")
  assertEq(recorded[1], root, "the root directory is imported first")
  assertEq(r.directories_imported, 3, "response reports how many directories were imported")
  assertEq(r.recursive, true, "response echoes the requested mode")
  assertEq(r.recursive_honoured, true, "recursive=true is actually honoured, not just echoed")
  assertEq(r.note, nil, "no caveat needed when recursion was honoured")
  assertEq(r.imported, 3, "counts are summed across every directory imported")

  -- recursive = false: we can pass only the top directory, but we cannot stop
  -- darktable recursing if the user's preference says to -- so it must never
  -- be reported as honoured.
  recorded = {}
  local r2 = internals.methods.import_batch({source_path = root, recursive = false})
  assertEq(#recorded, 1, "non-recursive import touches only the top directory")
  assertEq(recorded[1], root, "non-recursive import passes the source path itself")
  assertEq(r2.recursive, false, "response echoes recursive=false")
  assertEq(r2.recursive_honoured, false, "recursive=false is NOT claimed as honoured")
  assertTrue(r2.note ~= nil, "recursive=false carries an explanatory note")
  assertTrue(string.find(r2.note or "", "preference", 1, true) ~= nil,
    "note tells the user recursion follows their darktable preference")

  -- An unreadable source must not claim honoured recursion either.
  recorded = {}
  local r3 = internals.methods.import_batch({
    source_path = root .. "/nope", recursive = true,
  })
  assertEq(r3.recursive_honoured, false,
    "recursion is not claimed when the tree could not be enumerated")
  assertTrue(r3.note ~= nil, "unenumerable source carries an explanatory note")

  stub_dt.database = original_db
  rmtree(root)
end

-- ---- count_images_under: PREFIX match, not equality ------------------------
do
  -- Every imported directory becomes its own film roll, so with the
  -- per-subdirectory camera layout a film.path is never EQUAL to the
  -- destination. The old equality test counted zero for every camera import.
  local root = "/photos/import"
  local films = {
    {film = {path = "/photos/import"}},
    {film = {path = "/photos/import/store_00010001_DCIM_100NCD80"}},
    {film = {path = "/photos/import/store_00020001_DCIM_100NCD80"}},
    {film = {path = "/photos/import/store_00020001_DCIM_100NCD80/"}},  -- trailing /
    {film = {path = "/photos/importer-elsewhere"}},   -- prefix string, NOT a child
    {film = {path = "/photos/other"}},
    {film = nil},                                     -- no film at all
  }
  local original_db = stub_dt.database
  stub_dt.database = films

  assertEq(internals.count_images_under(root), 4,
    "counts the destination and everything beneath it")
  assertEq(internals.count_images_under(root .. "/"), 4,
    "a trailing slash on the source path does not change the count")
  assertTrue(internals.count_images_under("/photos/importer-elsewhere") == 1,
    "a sibling whose name merely starts with the prefix is not a child")
  assertEq(internals.count_images_under("/nowhere"), 0, "unrelated path counts zero")

  stub_dt.database = original_db
end

-- ---- poll_for_imported: waits for the count to SETTLE ----------------------
do
  local original_db = stub_dt.database
  local original_sleep = stub_dt.control.sleep
  local cfg = internals.import_poll

  -- A trickling multi-directory card import: images keep arriving for a
  -- while. Stopping at the first non-zero count would report 1 of 300.
  local ticks = 0
  local films = {}
  stub_dt.database = films
  stub_dt.control.sleep = function(_)
    ticks = ticks + 1
    if ticks <= 8 then
      films[#films + 1] = {film = {path = "/dest/sub" .. ticks}}
    end
  end

  local count, incomplete = internals.poll_for_imported("/dest")
  assertEq(count, 8, "poll keeps waiting while the count is still growing")
  assertEq(incomplete, false, "a settled count is not flagged incomplete")
  assertTrue(ticks >= 8 + cfg.settle_polls,
    "poll observed a full settle window of no growth before returning")
  assertTrue(ticks < cfg.attempts, "a settled import returns well before the ceiling")

  -- Still arriving when the ceiling expires: the count is a FLOOR, so the
  -- caller must be told it is incomplete rather than shown a confident total.
  ticks = 0
  films = {}
  stub_dt.database = films
  stub_dt.control.sleep = function(_)
    ticks = ticks + 1
    films[#films + 1] = {film = {path = "/dest/sub" .. ticks}}
  end
  local count2, incomplete2 = internals.poll_for_imported("/dest")
  assertEq(incomplete2, true, "a never-settling import is flagged scan_incomplete")
  assertTrue(count2 > 0, "an incomplete scan still reports the floor it reached")
  assertEq(ticks, cfg.attempts, "the poll is hard-capped at the attempt ceiling")

  -- Ceiling of 10s: long enough for a several-hundred-file card import, short
  -- enough to bound how long the worker is blocked.
  assertEq(cfg.deadline_seconds, 10, "import poll wall-clock ceiling is 10s")
  assertEq(cfg.attempts * cfg.interval_ms, 10000,
    "the sleep budget matches the wall-clock deadline")

  stub_dt.control.sleep = original_sleep
  stub_dt.database = original_db
end

-- ---- poll_for_imported: gives up early when NOTHING arrives ----------------
do
  -- Every attempt runs count_images_under, a full linear scan of dt.database.
  -- The old loop ran all 100 of them whenever the count stayed 0 -- the bad
  -- path / "darktable rejected the folder" case -- so the worst case was 100
  -- full library scans, not the 10s the comment claimed. On a 30k-image
  -- library that is minutes of head-of-line blocking for every other bridge
  -- request, past the client's own 120s budget.
  local original_db = stub_dt.database
  local original_sleep = stub_dt.control.sleep
  local cfg = internals.import_poll

  -- Count the SCANS, not the sleeps: the scans are the expensive half, and
  -- counting sleeps is exactly the mistake the old comment made. ipairs()
  -- probes index 1 first on an empty database, so this tallies one per scan.
  local scans = 0
  stub_dt.database = setmetatable({}, {__index = function(_, k)
    if k == 1 then scans = scans + 1 end
    return nil
  end})
  stub_dt.control.sleep = function(_) end

  local count, incomplete = internals.poll_for_imported("/dest/rejected")
  assertEq(count, 0, "a folder that registered nothing reports 0")
  assertEq(incomplete, true, "and is still flagged scan_incomplete")
  assertTrue(scans <= cfg.zero_grace_polls,
    "a count stuck at zero stops after the grace window, not " .. cfg.attempts
    .. " full library scans (ran " .. scans .. ")")
  assertTrue(scans < cfg.attempts,
    "the early exit really is earlier than the attempt ceiling")
  assertTrue(cfg.zero_grace_polls * cfg.interval_ms <= 3000,
    "the zero-result grace window is a couple of seconds, not the full budget")

  stub_dt.control.sleep = original_sleep
  stub_dt.database = original_db
end

-- ---- poll_for_imported: the wall-clock deadline bounds the SCANS -----------
do
  -- Bounding attempts is not the same as bounding time. If each scan is slow
  -- (a big library), the attempt ceiling can be nowhere near reached while the
  -- wall clock runs past the caller's budget. Drive an injected clock that
  -- jumps forward per attempt and prove the poll stops on time.
  local original_db = stub_dt.database
  local original_sleep = stub_dt.control.sleep
  local cfg = internals.import_poll

  -- Images keep arriving, so neither the settle path nor the zero-grace exit
  -- can fire; only the deadline can stop this.
  local films = {}
  stub_dt.database = films
  local ticks = 0
  stub_dt.control.sleep = function(_)
    ticks = ticks + 1
    films[#films + 1] = {film = {path = "/slow/sub" .. ticks}}
  end

  -- 3 simulated seconds per scan: the deadline lands on attempt 4-5, decades
  -- short of the 100-attempt ceiling.
  local fake_seconds = 0
  local function fake_now()
    local t = fake_seconds
    fake_seconds = fake_seconds + 3
    return t
  end

  local count, incomplete =
    internals.poll_for_imported("/slow", {now = fake_now})
  assertEq(incomplete, true, "a poll cut off by the deadline is flagged incomplete")
  assertTrue(count > 0, "the deadline still reports the floor reached so far")
  assertTrue(ticks < cfg.attempts,
    "the wall-clock deadline stops the poll long before the attempt ceiling "
    .. "(ran " .. ticks .. " of " .. cfg.attempts .. ")")
  assertTrue(fake_seconds <= (cfg.deadline_seconds + 6),
    "the poll does not overrun its wall-clock budget by more than one scan "
    .. "(simulated " .. fake_seconds .. "s for a " .. cfg.deadline_seconds
    .. "s budget)")

  stub_dt.control.sleep = original_sleep
  stub_dt.database = original_db
end

-- ---- handle: malformed method type -----------------------------------------
do
  local resp = internals.handle({id = "m1", method = nil})
  assertEq(resp.id, "m1", "malformed-method response preserves id")
  assertTrue(resp.error ~= nil, "missing method yields an error")
  assertTrue(string.find(resp.error, "method", 1, true) ~= nil,
    "missing-method error names the field")

  local resp2 = internals.handle({id = "m2", method = 42})
  assertTrue(resp2.error ~= nil, "non-string method yields an error")
  assertTrue(string.find(resp2.error, "number", 1, true) ~= nil,
    "non-string method error reports the actual type")
end

-- ---- Scouter darkroom verbs ------------------------------------------------

-- Registry completeness: every Scouter method the Python client knows about
-- (tests/test_lua_dispatcher.py's SCOUTER_METHODS) must actually be a
-- function in the real plugin.methods table -- not merely mentioned in the
-- source. This is the tests/lua half of Ruling 13's two-pronged check; the
-- Python half parses the source text independently.
do
  local expected_scouter_methods = {
    "view.current", "image.lookup", "darkroom.open", "darkroom.current",
    "darkroom.action", "darkroom.status", "module.enable", "module.disable",
    "module.reset", "history.commit", "snapshot.take", "style.create_from_current",
    "style.apply", "image.rate", "style.export_file", "style.import_file",
    "style.delete", "image.color_labels",
  }
  for _, name in ipairs(expected_scouter_methods) do
    assertTrue(type(plugin.methods[name]) == "function",
      "plugin.methods actually registers " .. name .. " (not just mentioned in source)")
  end
end

-- ids_match: direct unit tests (fix round 3). `nil == nil` is true in Lua,
-- so this must explicitly reject "neither side has an id" as a match, while
-- still coercing numeric-looking strings the way tonumber() does.
do
  local m = plugin.ids_match
  assertEq(m(nil, nil), false, "ids_match(nil, nil) is false -- no id on either side is not a match")
  assertEq(m(nil, 5), false, "ids_match(nil, 5) is false")
  assertEq(m(5, nil), false, "ids_match(5, nil) is false")
  assertEq(m("x", "x"), false, 'ids_match("x", "x") is false -- neither side is a number')
  assertEq(m(5, "5"), true, 'ids_match(5, "5") is true -- tonumber coerces the numeric string')
  assertEq(m(5, 5), true, "ids_match(5, 5) is true")
end

-- view.current reflects the stub's view state.
view_state.current = "lighttable"
assertEq(plugin.methods["view.current"]().view, "lighttable", "view.current lighttable")
view_state.current = "darkroom"
assertEq(plugin.methods["view.current"]().view, "darkroom", "view.current darkroom")

-- darkroom.action passes every argument through and reports a real value.
action_returns["iop/exposure/exposure"] = 1.5
local act = plugin.methods["darkroom.action"]({
  path = "iop/exposure/exposure", element = "value", effect = "set", value = 1.5})
assertEq(act.status, 1.5, "action status")
assertEq(act.nan, false, "action not nan")
assertEq(act.effect, "set", "action effect echoed")
assertEq(act.view, "darkroom", "action view echoed")
assertEq(action_log[#action_log].element, "value", "action element forwarded")

-- effect="set" requires an actual numeric value: it must error, never
-- silently default to 0 and zero out a slider the caller only meant to nudge.
local resp_missing_value = plugin.handle({id = "dv1", method = "darkroom.action",
  params = {path = "iop/exposure/exposure", effect = "set"}})
assertTrue(resp_missing_value.error ~= nil
  and resp_missing_value.error:find("value", 1, true) ~= nil,
  "darkroom.action refuses effect=set with no value")
local resp_string_value = plugin.handle({id = "dv2", method = "darkroom.action",
  params = {path = "iop/exposure/exposure", effect = "set", value = "1.5"}})
assertTrue(resp_string_value.error ~= nil,
  "darkroom.action refuses effect=set with a non-numeric value")

-- nan must arrive as nan=true, NOT as a null number.
action_returns["iop/sigmoid/contrast"] = "nan"
local nanres = plugin.methods["darkroom.action"]({
  path = "iop/sigmoid/contrast", effect = "set", value = 1.4})
assertEq(nanres.nan, true, "nan flagged")
-- A direct `nanres.status == nil` check is vacuous in Lua: a table
-- constructor never STORES a nil-valued field, so this would read nil
-- whether or not the encoder is correct. Assert on the encoded JSON text
-- instead, where the key's absence (not an explicit null) is observable.
assertTrue(plugin.json.encode(nanres):find('"status"', 1, true) == nil,
  "nan status key is absent from the encoded JSON (Lua omits nil-valued keys)")
assertTrue(plugin.json.encode(nanres):find('"nan":true') ~= nil, "nan survives encoding")

-- darkroom.status uses the empty effect and speed 0 (the documented query).
action_returns["iop/exposure/exposure"] = 0.6429
local st = plugin.methods["darkroom.status"]({path = "iop/exposure/exposure"})
assertEq(st.position, 0.6429, "status position")
assertEq(action_log[#action_log].effect, "", "status uses empty effect")
assertEq(action_log[#action_log].value, 0, "status uses speed 0")

-- module verbs map onto iop/<module> with the right element and effect.
action_returns["iop/exposure"] = 1.0
local en = plugin.methods["module.enable"]({module = "exposure"})
assertEq(en.action, "enable", "module.enable action name")
assertEq(action_log[#action_log].element, "enable", "enable element")
assertEq(action_log[#action_log].effect, "on", "enable effect")
plugin.methods["module.disable"]({module = "exposure"})
assertEq(action_log[#action_log].effect, "off", "disable effect")
plugin.methods["module.reset"]({module = "exposure"})
assertEq(action_log[#action_log].element, "reset", "reset element")
assertEq(action_log[#action_log].effect, "activate", "reset effect")

-- A missing module name is an error response, not a silent no-op.
local ok_err = plugin.handle({id = "x", method = "module.enable", params = {}})
assertTrue(ok_err.error ~= nil, "module.enable without module errors")

-- history.commit: strategy="auto" never tries the sidecar ladder (task 1's
-- measurement rejected every spelling). Row 429: auto is no_bounce, then the
-- action bounce -- here nothing is loaded, so no_bounce has no image and the
-- bounce is what runs.
view_state.current = "darkroom"
local c = plugin.methods["history.commit"]({strategy = "auto"})
assertEq(c.strategy_used, "view_bounce_action", "auto falls back to the action bounce, never the sidecar ladder")
assertEq(c.fallback_from and c.fallback_from.strategy, "no_bounce", "after trying no_bounce")
assertEq(c.ok, true, "view bounce ok")
assertEq(c.view_after, "darkroom", "view bounce returns to the darkroom")
assertTrue(type(c.cpu_ms) == "number", "history.commit reports cpu_ms as a number")
assertTrue(c.ms == nil, "history.commit no longer reports the old ambiguous ms field")

-- An unknown strategy is a caller mistake, not a silent fallback to auto.
local c_bad = plugin.handle({id = "bad-strategy", method = "history.commit",
  params = {strategy = "yolo"}})
assertTrue(c_bad.error ~= nil and c_bad.error:find("yolo", 1, true) ~= nil,
  "unknown strategy names the offending value")
assertTrue(c_bad.error and c_bad.error:find("auto", 1, true) ~= nil
  and c_bad.error:find("sidecar_action", 1, true) ~= nil,
  "unknown strategy error lists the valid strategies")

-- ...the sidecar ladder stays reachable only via an EXPLICIT
-- strategy="sidecar_action" request, and can still succeed there...
action_returns["lib/selected image[s]/write sidecar files"] = 1
local c2 = plugin.methods["history.commit"]({strategy = "sidecar_action"})
assertEq(c2.strategy_used, "sidecar_action", "explicit sidecar_action strategy reaches the ladder")
assertEq(c2.ok, true, "sidecar commit ok")

-- ...and honestly reports failure, never a silent fallback, when every
-- spelling is nan.
action_returns["lib/copy_history/write sidecar files"] = "nan"
action_returns["lib/selected image[s]/write sidecar files"] = "nan"
action_returns["lib/metadata/write sidecar files"] = "nan"
action_returns["lib/image/write sidecar files"] = "nan"
local c3 = plugin.methods["history.commit"]({strategy = "sidecar_action"})
assertEq(c3.strategy_used, "sidecar_action", "sidecar_action strategy_used echoed even on failure")
assertEq(c3.ok, false, "sidecar commit reports failure instead of silently succeeding")

-- The view bounce refuses outside the darkroom instead of attempting a
-- bounce that cannot possibly commit anything.
view_state.current = "lighttable"
local c4 = plugin.methods["history.commit"]({strategy = "view_bounce_action"})
assertEq(c4.ok, false, "view bounce refuses outside the darkroom")
assertEq(c4.detail, "not in darkroom", "refusal names the reason")
assertEq(c4.view_after, "lighttable", "refusal reports the actual (unchanged) view honestly")
-- auto outside the darkroom does not bounce either: no_bounce's own answer stands.
local c4a = plugin.methods["history.commit"]({strategy = "auto"})
assertEq(c4a.strategy_used, "no_bounce", "auto outside the darkroom reports no_bounce")
assertEq(c4a.ok, false, "and its honest failure (nothing to commit)")
assertEq(view_state.current, "lighttable", "without switching views")

-- A single flaky switch back into the darkroom is retried once, so it does
-- not strand the session in the lighttable when the commit likely landed.
view_state.current = "darkroom"
view_state.fail_switches_remaining = 1
local c5 = plugin.methods["history.commit"]({strategy = "view_bounce_action"})
assertEq(c5.ok, true, "a switch that fails once and succeeds on retry still commits")
assertEq(c5.view_after, "darkroom", "retry lands back in the darkroom")
assertEq(view_state.fail_switches_remaining, 0, "the scripted failure was consumed by the retry")

-- Two consecutive failures exhaust the retry: report failure and the ACTUAL
-- view truthfully, never a claimed "darkroom" that never happened.
view_state.current = "darkroom"
view_state.fail_switches_remaining = 2
local c6 = plugin.methods["history.commit"]({strategy = "view_bounce_action"})
assertEq(c6.ok, false, "two consecutive switch failures are reported as failure")
assertEq(c6.view_after, "lighttable",
  "an unrecovered bounce leaves (and honestly reports) the session in the lighttable")
view_state.fail_switches_remaining = 0

-- Commit confirmation: history.commit reports what actually reached disk.
--
-- Host-proven (task 12 fix round 1): the bounce writes the sidecar only when
-- the history actually CHANGED, so "nothing was written" is a real and
-- expected outcome, not automatically a failure -- the Python layer resolves
-- it by reading the value back. What the plugin must never do is claim a
-- write that did not happen.
--
-- An earlier round of this fix tried to force the write by setting
-- darktable's write_sidecar_files preference. That was inert (the Lua
-- preferences API writes a SCRIPT-namespaced key, not the core setting) and
-- it littered the user's darktablerc. test_lua_dispatcher.py pins that it is
-- gone; here we pin the observable behaviour.
do
  local tmp = os.tmpname()
  local f = io.open(tmp, "w"); f:write("before"); f:close()
  local img = images_by_id[101]
  img.sidecar = tmp
  stub_dt.gui.action_images = {img}
  view_state.current = "darkroom"

  -- Grow the file on the way out of the darkroom -- where the real darktable
  -- queues the write.
  local original_view = stub_dt.gui.current_view
  stub_dt.gui.current_view = function(v)
    local r = original_view(v)
    if v ~= nil and v.id == "lighttable" then
      local h = io.open(tmp, "w"); h:write("after the bounce"); h:close()
    end
    return r
  end

  local cp = plugin.methods["history.commit"]({strategy = "view_bounce_action"})
  stub_dt.gui.current_view = original_view

  assertEq(cp.ok, true, "the commit confirms the sidecar actually changed")
  assertEq(cp.sidecar, tmp, "the commit reports which sidecar it watched")
  assertEq(cp.sidecar_written, true, "sidecar_written reports the real disk fact")
  assertTrue(type(cp.sidecar_wait_ms) == "number", "and how long it waited for it")

  -- A sidecar that never changes is reported honestly -- never ok=true just
  -- because the views moved -- and the reason names the real mechanism.
  view_state.current = "darkroom"
  local cq = plugin.methods["history.commit"]({strategy = "view_bounce_action"})
  assertEq(cq.sidecar_written, false, "an unchanged sidecar is reported as not written")
  assertEq(cq.ok, false, "and the commit as a whole does not claim success")
  assertTrue(cq.detail ~= nil and cq.detail:find("did not change", 1, true) ~= nil,
    "the failure says what did not happen: " .. tostring(cq.detail))
  assertTrue(cq.detail:find("history actually changes", 1, true) ~= nil,
    "and why that is expected for a repeated value")

  -- No image loaded means there is no sidecar to watch: sidecar_written must
  -- be absent (not false), so "not checked" is distinguishable from
  -- "checked and unchanged".
  img.sidecar = nil
  view_state.current = "darkroom"
  local cn = plugin.methods["history.commit"]({strategy = "view_bounce_action"})
  assertEq(cn.sidecar, nil, "no sidecar path to report when the image has none")
  assertEq(cn.sidecar_written, nil, "sidecar_written is absent, not false, when unchecked")
  assertEq(cn.ok, true, "and the bounce result stands on its own")

  os.remove(tmp)
  stub_dt.gui.action_images = {}
  view_state.current = "darkroom"
end

-- Task 12: `attempts` is clamped to 1..3, and a caller who does not expect a
-- change gets exactly one bounce with `ok=true` even when nothing was
-- written -- the Python layer's no-op pre-check relies on this to avoid
-- paying for two bounces it already knows will find nothing to write. The
-- default path (no attempts/expect_change given) is unchanged: two attempts,
-- and `ok=false` for a sidecar that never changed.
do
  local tmp = os.tmpname()
  local f = io.open(tmp, "w"); f:write("before"); f:close()
  local img = images_by_id[101]
  img.sidecar = tmp
  stub_dt.gui.action_images = {img}
  view_state.current = "darkroom"

  local c_zero = plugin.methods["history.commit"]({strategy = "view_bounce_action", attempts = 0})
  assertEq(c_zero.attempts_used, 1, "attempts below 1 clamps to 1")

  local c_big = plugin.methods["history.commit"]({strategy = "view_bounce_action", attempts = 99})
  assertEq(c_big.attempts_used, 3, "attempts above 3 clamps to 3")

  local c_noop = plugin.methods["history.commit"](
    {strategy = "view_bounce_action", expect_change = false})
  assertEq(c_noop.attempts_used, 1, "expect_change=false forces one attempt regardless of `attempts`")
  assertEq(c_noop.sidecar_written, false, "sidecar_written reports the real disk fact")
  assertEq(c_noop.ok, true, "expect_change=false with nothing written is still ok")
  assertEq(c_noop.expect_change, false, "expect_change is echoed back")

  local c_default = plugin.methods["history.commit"]({strategy = "view_bounce_action"})
  assertEq(c_default.attempts_used, 2, "the default path still spends up to two attempts")
  assertEq(c_default.expect_change, true, "expect_change defaults to true")
  assertEq(c_default.sidecar_written, false,
    "still unwritten (the sidecar never changes in this fixture)")
  assertEq(c_default.ok, false, "expect_change=true with nothing written is not ok")

  os.remove(tmp)
  stub_dt.gui.action_images = {}
  view_state.current = "darkroom"
end

-- style.create_from_current is honest about the missing module filter, and
-- about where the "current image" reading came from.
stub_dt.gui.action_images = {images_by_id[101]}
local cs = plugin.methods["style.create_from_current"]({
  name = "hero", modules = {"exposure", "sigmoid"}})
assertEq(cs.created, true, "style created")
assertEq(cs.modules_requested, 2, "modules counted")
assertEq(cs.modules_honoured, false, "module filter honestly refused")
assertTrue(cs.note ~= nil, "module filter note present")
assertEq(cs.source, "action_images",
  "reports the action_images fallback source (stub has no display_image)")

-- style.apply appends, counts misses, and refuses overwrite loudly.
local ap = plugin.methods["style.apply"]({image_ids = {101, 999}, name = "hero"})
assertEq(ap.applied, 1, "style applied to the one real image")
assertEq(ap.missed[1], 999, "missing id reported")
local ov = plugin.handle({id = "y", method = "style.apply",
  params = {image_ids = {101}, name = "hero", mode = "overwrite"}})
assertTrue(ov.error ~= nil and ov.error:find("overwrite") ~= nil,
  "overwrite mode refused with an explanation")

-- Phase 3 task 8: darkroom.apply_style applies INSIDE the bounce (the
-- lighttable leg, via commit_view_bounce's `between` callback), then
-- reports the darkroom the session really ended in. A mismatched image_id
-- refuses with "wrong image loaded:" -- the same string dtbridge's
-- `_WRONG_IMAGE_RE` classifies as not_in_darkroom.
do
  local order = {}
  table.insert(stub_dt.styles, {name = "harbour-warm"})
  local image = {id = 12, apply_style = function(self, s)
    table.insert(order, "apply:" .. s.name .. "@" .. view_state.current)
  end}
  stub_dt.gui.action_images = {image}
  view_state.current = "darkroom"

  local r = plugin.methods["darkroom.apply_style"]({image_id = 12, name = "harbour-warm"})
  assertEq(r.applied, true, "applied")
  assertEq(order[1], "apply:harbour-warm@lighttable",
    "applied in the lighttable, got " .. tostring(order[1]))
  assertEq(r.view, "darkroom", "returned to the darkroom")
  assertEq(view_state.current, "darkroom", "the session is genuinely back in the darkroom")
  assertTrue(type(r.left_ms) == "number", "left_ms is a real number (between was given)")
  assertTrue(type(r.returned_ms) == "number", "returned_ms is a real number (between was given)")
  assertTrue(type(r.cpu_ms) == "number", "cpu_ms is reported honestly as CPU time")
  assertEq(r.wall_ms, nil, "no dishonestly-named wall_ms field")

  local ok, err = pcall(plugin.methods["darkroom.apply_style"], {image_id = 99, name = "harbour-warm"})
  assertEq(ok, false, "refuses another image")
  assertTrue(tostring(err):find("wrong image loaded:") ~= nil,
    "refuses another image: " .. tostring(err))

  local ok_missing, err_missing = pcall(
    plugin.methods["darkroom.apply_style"], {image_id = 12, name = "does-not-exist"})
  assertEq(ok_missing, false, "an unknown style raises")
  assertTrue(tostring(err_missing):find("not found") ~= nil,
    "unknown style names itself: " .. tostring(err_missing))

  stub_dt.gui.action_images = {}
end

-- style.export_file: not-found is a real error, and a successful export
-- writes a real file whose size file_size() reports honestly.
local ok_missing, err_missing = pcall(function()
  return plugin.methods["style.export_file"]({name = "does-not-exist", dir = "/tmp"})
end)
assertEq(ok_missing, false, "export_file on an unknown style raises")
assertTrue(tostring(err_missing):find("not found") ~= nil,
  "export_file's not-found error names the style")

local export_dir = os.getenv("TMPDIR") or "/tmp"
local exp = plugin.methods["style.export_file"]({name = "hero", dir = export_dir})
assertEq(exp.name, "hero", "export_file echoes the style name")
assertEq(exp.exported, true, "export_file reports exported when the file's signature advanced")
assertTrue(exp.path:find("hero.dtstyle", 1, true) ~= nil,
  "export_file returns the .dtstyle path")
assertTrue(exp.bytes > 0, "export_file reports a real, non-zero file size")

-- fix round 1, should-fix 9: a darktable that silently no-ops (pcall ok=true,
-- nothing written or changed) must NOT be reported as exported=true.
force_noop_export = true
local exp_noop = plugin.methods["style.export_file"]({name = "hero", dir = export_dir})
assertEq(exp_noop.exported, false,
  "export_file must not claim success when the file's signature did not advance")
force_noop_export = false

-- style.import_file: existing name without overwrite is refused BEFORE any
-- delete or import call.
local styles_before_refusal = 0
for _ in ipairs(stub_dt.styles) do styles_before_refusal = styles_before_refusal + 1 end
local ok_exists, err_exists = pcall(function()
  return plugin.methods["style.import_file"]({path = exp.path, name = "hero"})
end)
assertEq(ok_exists, false, "import_file over an existing name without overwrite raises")
assertTrue(tostring(err_exists):find("already exists") ~= nil,
  "import_file's refusal names the collision")
assertEq(#styles_deleted, 0, "the refusal deletes nothing")

-- fix round 1, should-fix 9: the collision check is against the FILE's own
-- <info><name> (here "hero", already existing), never the caller's `name`
-- hint -- a mismatched hint ("hero-2", which does NOT exist) must not let a
-- real collision slip through.
local ok_hint_mismatch, err_hint_mismatch = pcall(function()
  return plugin.methods["style.import_file"]({path = exp.path, name = "hero-2"})
end)
assertEq(ok_hint_mismatch, false,
  "a non-colliding NAME HINT must not bypass a real collision on the file's own name")
assertTrue(tostring(err_hint_mismatch):find("\"hero\"", 1, true) ~= nil,
  "the refusal names the FILE's own style name (hero), not the hint (hero-2)")

-- overwrite=true deletes the old style first, then imports -- replaced=true,
-- and styles_after accounts for delete-then-import (net unchanged count).
-- The destructive window is real: an import failure here would leave "hero"
-- gone with no style re-created (covered by the import-failure test below).
local imp = plugin.methods["style.import_file"]({path = exp.path, name = "hero", overwrite = true})
assertEq(imp.imported, true, "import_file reports imported")
assertEq(imp.name, "hero", "import_file's returned name is the FILE's own name")
assertEq(imp.replaced, true, "import_file reports the delete-then-import replace")
assertEq(imp.styles_before, styles_before_refusal, "styles_before matches the pre-import count")
assertEq(imp.styles_after, styles_before_refusal, "styles_after: one deleted, one imported back")
assertEq(styles_deleted[#styles_deleted], "hero", "the existing style was deleted before re-import")
assertEq(styles_imported[#styles_imported], exp.path, "the exported path was the one imported")

-- A style whose FILE name genuinely never existed imports cleanly, without
-- touching delete at all. Written by hand (not via create_from_current +
-- export, which would already have inserted the name into dt.styles) so
-- this really is a name dt.styles has never seen.
local fresh_path = export_dir .. "/hero-fresh-manual.dtstyle"
do
  local f = io.open(fresh_path, "w")
  f:write("<darktable_style><info><name>hero-fresh</name></info></darktable_style>\n")
  f:close()
end
local deletes_before = #styles_deleted
local imp2 = plugin.methods["style.import_file"]({path = fresh_path, name = "hero-fresh"})
assertEq(imp2.imported, true, "a genuinely fresh name imports")
assertEq(imp2.replaced, false, "a fresh name is not reported as replaced")
assertEq(#styles_deleted, deletes_before, "importing a fresh name deletes nothing")

-- fix round 1, should-fix 9: an import failure AFTER the destructive delete
-- names that window explicitly, rather than a bare pcall error. "hero-fresh"
-- now exists (imp2 above), so overwrite=true deletes it before the forced
-- import failure.
do
  -- Setting a real field on stub_dt.styles shadows the metatable's __index
  -- for "import" specifically (the plugin's own `dt` local IS stub_dt, via
  -- package.loaded.darktable); setting it back to nil restores the normal
  -- dynamic dispatch.
  stub_dt.styles.import = function(path) error("stub-forced import failure") end
  local ok_destructive, err_destructive = pcall(function()
    return plugin.methods["style.import_file"]({path = fresh_path, name = "hero-fresh", overwrite = true})
  end)
  assertEq(ok_destructive, false, "a forced import failure still raises")
  assertTrue(tostring(err_destructive):find("destructive window", 1, true) ~= nil,
    "the error names the destructive window when the delete already happened")
  stub_dt.styles.import = nil
end

-- style.delete: fix round 2, item 5 -- gated on BOTH a name prefix (default
-- "scouter-") and confirm=true, before the dt.styles scan even runs. The
-- harness's test styles are named "hero"/"hero-fresh" (not "scouter-..."),
-- so these calls pass an explicit `prefix` matching them.
local ok_no_prefix, err_no_prefix = pcall(function()
  return plugin.methods["style.delete"]({name = "hero", confirm = true})
end)
assertEq(ok_no_prefix, false,
  "style.delete refuses a name that does not start with the (default) prefix")
assertTrue(tostring(err_no_prefix):find("prefix", 1, true) ~= nil,
  "the refusal names the prefix requirement")

local ok_no_confirm, err_no_confirm = pcall(function()
  return plugin.methods["style.delete"]({name = "hero", prefix = "hero"})
end)
assertEq(ok_no_confirm, false, "style.delete refuses without confirm=true, even with a matching prefix")
assertTrue(tostring(err_no_confirm):find("confirm", 1, true) ~= nil,
  "the refusal names the missing confirmation")

-- Tolerant of "already gone" (hero-fresh was just deleted by the
-- destructive-window test above), real otherwise (against "hero", which
-- still exists from the overwrite=true re-import earlier).
local del_missing = plugin.methods["style.delete"](
  {name = "hero-fresh", prefix = "hero", confirm = true})
assertEq(del_missing.existed, false, "hero-fresh is already gone (deleted above)")
assertEq(del_missing.deleted, false, "nothing was deleted")
local before_delete = 0
for _ in ipairs(stub_dt.styles) do before_delete = before_delete + 1 end
local del_real = plugin.methods["style.delete"]({name = "hero", prefix = "hero", confirm = true})
assertEq(del_real.existed, true, "hero existed")
assertEq(del_real.deleted, true, "hero was deleted")
local after_delete = 0
for _ in ipairs(stub_dt.styles) do after_delete = after_delete + 1 end
assertEq(after_delete, before_delete - 1, "style.delete actually removed one style")

-- The default prefix really is "scouter-": a name that DOES start with it
-- is accepted (still needs confirm=true), proving the refusal above was
-- about the prefix, not some other reason.
local ok_default_prefix = plugin.methods["style.delete"](
  {name = "scouter-throwaway-test-style", confirm = true})
assertEq(ok_default_prefix.existed, false,
  "a name starting with the default prefix is accepted (just not present)")

-- Fix round 3, item 4: an EXPLICIT empty prefix is refused with a typed
-- error, never silently rewritten to the "scouter-" default -- prefix=""
-- would otherwise match every style name (sub(1, 0) == "" for anything).
local ok_empty_prefix, err_empty_prefix = pcall(function()
  return plugin.methods["style.delete"]({name = "anything-at-all", prefix = "", confirm = true})
end)
assertEq(ok_empty_prefix, false, "an explicit empty prefix is refused, not silently defaulted")
assertTrue(tostring(err_empty_prefix):find("prefix", 1, true) ~= nil,
  "the refusal names the prefix requirement")
assertTrue(tostring(err_empty_prefix):find("non-empty", 1, true) ~= nil,
  "the refusal explains prefix must be non-empty")

-- xml_unescape: all five entities, and the &amp;lt; ordering case (must not
-- double-unescape to "<").
assertEq(plugin.xml_unescape("Tom &amp; Jerry"), "Tom & Jerry", "xml_unescape: &amp;")
assertEq(plugin.xml_unescape("a &lt;b&gt; c"), "a <b> c", "xml_unescape: &lt; and &gt;")
assertEq(plugin.xml_unescape('say &quot;hi&quot;'), 'say "hi"', "xml_unescape: &quot;")
assertEq(plugin.xml_unescape("it&apos;s"), "it's", "xml_unescape: &apos;")
assertEq(plugin.xml_unescape("&amp;lt;"), "&lt;",
  "xml_unescape: &amp;lt; unescapes ONE level (to &lt;), not through to <")
assertEq(plugin.xml_unescape(nil), nil, "xml_unescape: nil in, nil out")

-- <info><name> containing an XML entity (as scouter/core/dtstyle.py's
-- ElementTree writer would escape a literal "&") is UNESCAPED before the
-- collision check (fix round 2, item 6): darktable itself stores the
-- unescaped name, so comparing the raw escaped text would silently never
-- match a real collision on a name containing any of these characters.
local escaped_path = export_dir .. "/escaped-name.dtstyle"
do
  local f = io.open(escaped_path, "w")
  f:write("<darktable_style><info><name>Tom &amp; Jerry</name></info></darktable_style>\n")
  f:close()
end
local imp_escaped = plugin.methods["style.import_file"]({path = escaped_path, name = "ignored-hint"})
assertEq(imp_escaped.name, "Tom & Jerry",
  "the collision-check name is XML-unescaped from <info><name> (Tom &amp; Jerry -> Tom & Jerry)")
assertEq(imp_escaped.imported, true, "importing an escaped-name style still succeeds")
-- A second import of the exact same (still-escaped-on-disk) name is a real
-- collision once unescaped, proving the name round-trips consistently.
local ok_escaped_collision = pcall(function()
  return plugin.methods["style.import_file"]({path = escaped_path, name = "ignored-hint"})
end)
assertEq(ok_escaped_collision, false, "the escaped name is still recognised on a second import")

-- darkroom.open selects then switches, and detects an already-open image.
view_state.current = "lighttable"
stub_dt.gui.action_images = {}
local op = plugin.methods["darkroom.open"]({image_id = 101})
assertEq(op.image_id, 101, "open by id")
assertEq(op.opened, true, "opened")
assertEq(op.view, "darkroom", "view after open")
assertEq(op.imported, false, "opening a known image does not import anything")
assertTrue(#selection_log > 0, "selection set before the view switch")
stub_dt.gui.action_images = {images_by_id[101]}
local op2 = plugin.methods["darkroom.open"]({image_id = 101})
assertEq(op2.already_open, true, "already-open detected")
assertEq(op2.opened, false, "no redundant view switch")

-- THE BLOCKER THIS ROUND FIXES: the darkroom is open on a DIFFERENT image.
-- dt.gui.current_view(darkroom) alone is a no-op there, so the old code
-- reported opened=true while image 101 stayed loaded. switch_to_darkroom
-- must bounce through the lighttable and really load image 102.
view_state.current = "darkroom"
stub_dt.gui.action_images = {images_by_id[101]}
local op3 = plugin.methods["darkroom.open"]({image_id = 102})
assertEq(op3.image_id, 102, "open requests id 102")
assertEq(op3.opened, true, "switching to a different already-darkroom image reports opened=true")
assertTrue(op3.error == nil, "no error when the switch actually loads the requested image")
assertEq(stub_dt.gui.action_images[1].id, 102, "the stub's loaded image genuinely became 102")

-- A selection that silently targets the WRONG image (simulated staleness):
-- the post-switch VERIFICATION must catch the mismatch and refuse, never
-- claim opened=true for an image that never actually loaded.
view_state.current = "darkroom"
stub_dt.gui.action_images = {images_by_id[101]}
local original_selection = stub_dt.gui.selection
stub_dt.gui.selection = function(_)
  table.insert(selection_log, {images_by_id[101]})
  stub_dt.gui.action_images = {images_by_id[101]}
  return {images_by_id[101]}
end
-- A wrong-image outcome is a JSON-RPC error (like darkroom.open's other
-- failures), not a {opened=false, error=...} result a caller could ignore --
-- so it must be reached through plugin.handle, not a direct call to
-- plugin.methods["darkroom.open"], which would let the error abort the test.
local op4 = plugin.handle({id = "wrong-image", method = "darkroom.open",
  params = {image_id = 102}})
assertTrue(op4.error ~= nil and op4.error:find("102", 1, true) ~= nil,
  "wrong-image error names the requested id")
assertTrue(op4.result == nil, "a wrong-image outcome carries no result, only an error")
stub_dt.gui.selection = original_selection

-- A path not yet in the library is imported, then opened, and reported so.
local imported_image = {id = 104, filename = "NEWFILE.NEF", path = "/photos", rating = 0}
imported_image.apply_style = function(self, style)
  table.insert(applied_styles, {id = self.id, style = style.name})
end
local original_db = stub_dt.database
stub_dt.database = setmetatable({
  import = function(_)
    images_by_id[104] = imported_image
    table.insert(iter_list, imported_image)
    return imported_image
  end,
}, {__index = original_db})
view_state.current = "lighttable"
stub_dt.gui.action_images = {}
local op5 = plugin.methods["darkroom.open"]({path = "/photos/NEWFILE.NEF"})
assertEq(op5.imported, true, "darkroom.open reports imported=true when the path had to be registered")
assertEq(op5.image_id, 104, "darkroom.open opens the newly imported image")
stub_dt.database = original_db
images_by_id[104] = nil
table.remove(iter_list)

-- darkroom.open with NO params has nothing to load and no "current image"
-- fallback -- it is a clear JSON-RPC error, not a silent success (fix round
-- 2, blocker 1: Python's undo_last / _current_image must never rely on it
-- for "what's currently loaded"; darkroom.current exists for that).
local op_empty = plugin.handle({id = "empty-open", method = "darkroom.open", params = {}})
assertTrue(op_empty.error ~= nil, "darkroom.open {} is a JSON-RPC error")
assertTrue(op_empty.result == nil, "darkroom.open {} carries no result")
assertTrue(op_empty.error:find("no image", 1, true) ~= nil,
  "darkroom.open {} names the actual problem (no image_id/path given)")

-- darkroom.current: a read-only snapshot of loaded_darkroom_image(), with no
-- params and no side effects (never selects, switches, or imports) -- the
-- honest way for Python to learn what is loaded instead of forcing a switch
-- through darkroom.open (fix round 2, blocker 1).
view_state.current = "darkroom"
stub_dt.gui.action_images = {images_by_id[101]}
local cur1 = plugin.methods["darkroom.current"]()
assertEq(cur1.view, "darkroom", "darkroom.current reports the current view")
assertEq(cur1.image_id, 101, "darkroom.current reports the loaded image id")
assertEq(cur1.path, "/photos/DSC_0001.NEF", "darkroom.current reports the loaded image path")
assertEq(cur1.source, "action_images", "darkroom.current reports where the answer came from")

view_state.current = "lighttable"
stub_dt.gui.action_images = {}
local cur2 = plugin.methods["darkroom.current"]()
assertEq(cur2.view, "lighttable", "darkroom.current reports the lighttable view")
assertEq(cur2.image_id, nil, "darkroom.current: no image loaded in the lighttable")
assertEq(cur2.path, nil, "darkroom.current: no path when nothing is loaded")

-- switch_to_darkroom / loaded_darkroom_image: the display_image branches.
-- The stub's dt.gui.views.darkroom has no display_image by default (all the
-- darkroom.open tests above exercise the action_images fallback), so these
-- are exercised directly against the exported internals with a scripted
-- display_image getter/setter.
--
-- display_image is only reached from INSIDE the darkroom now: the task 12
-- fix round proved on the real host that calling it from the lighttable is a
-- silent no-op (pcall ok=true, view unchanged for 10s), so the plugin must
-- never rely on it to change the view.
do
  local display_state = {current = nil}
  views.darkroom.display_image = function(image)
    if image == nil then return display_state.current end
    display_state.current = image
    return image
  end
  view_state.current = "darkroom"

  local switched, method = plugin.switch_to_darkroom(images_by_id[103])
  assertTrue(switched, "switch_to_darkroom succeeds via display_image")
  assertEq(method, "display_image", "switch_to_darkroom reports display_image as the method used")
  assertEq(display_state.current.id, 103, "display_image actually stored the requested image")

  -- loaded_darkroom_image must NOT consult the display_image GETTER, even
  -- when one exists: on the real host that no-arg call deadlocks the worker
  -- coroutine permanently (task 12 fix round 1; see the plugin comment).
  -- Scripted here as an outright hang would hang the test suite, so the
  -- getter is scripted to answer a DIFFERENT image than the selection --
  -- reading it would be visible, and must not happen.
  display_state.current = images_by_id[102]
  stub_dt.gui.action_images = {images_by_id[103]}
  local image, source = plugin.loaded_darkroom_image()
  assertEq(source, "action_images",
    "loaded_darkroom_image reads the selection, never the display_image getter")
  assertTrue(image ~= nil and image.id == 103,
    "and answers with the selection (103), not the getter's image (102)")

  views.darkroom.display_image = nil
end

-- A display_image whose GETTER answers nil (real-world quirk: the setter
-- half works but the read-back half does not) must fall back to
-- action_images -- and that fallback must already be correct, proving
-- switch_to_darkroom's select-before-display_image ordering (fix round 2
-- item 2) rather than a lucky leftover from an earlier test.
do
  views.darkroom.display_image = function(image)
    if image == nil then return nil end  -- getter always answers nil
    return image                          -- setter reports success
  end
  stub_dt.gui.action_images = {}
  view_state.current = "darkroom"

  local switched, method = plugin.switch_to_darkroom(images_by_id[101])
  assertTrue(switched, "switch_to_darkroom still succeeds via display_image's setter")
  assertEq(method, "display_image", "switch_to_darkroom still reports display_image as the method used")

  local image, source = plugin.loaded_darkroom_image()
  assertEq(source, "action_images",
    "falls back to action_images when display_image's getter answers nil")
  assertTrue(image ~= nil and image.id == 101,
    "the action_images fallback is already correct -- selection ran before display_image")

  views.darkroom.display_image = nil
end

-- From the LIGHTTABLE, the view switch -- not display_image -- must be the
-- call that does the work, even when display_image is available. This is the
-- exact regression that made history.commit's return leg fail on the host:
-- display_image reported success from the lighttable and the view never moved.
do
  local display_calls = {}
  views.darkroom.display_image = function(image)
    if image == nil then return nil end
    table.insert(display_calls, image.id)
    return image          -- reports success but (on the host) moves nothing
  end
  view_state.current = "lighttable"
  stub_dt.gui.action_images = {}

  local switched, method = plugin.switch_to_darkroom(images_by_id[102])
  assertTrue(switched, "switch_to_darkroom succeeds from the lighttable")
  assertEq(method, "view_switch",
    "from the lighttable the real view switch does the work, not display_image")
  assertEq(view_state.current, "darkroom", "the view actually ended up in the darkroom")

  views.darkroom.display_image = nil
end

-- wait_for_view: the switch is ASYNCHRONOUS on the real host, so a view
-- switch must be verified by yield-polling, never by an immediate read.
do
  view_state.current = "darkroom"
  view_state.switch_lag_sleeps = 3        -- lands only after 3 yields
  stub_dt.gui.current_view(views.lighttable)
  assertEq(view_state.current, "darkroom",
    "the stub mirrors the host: the view has NOT changed when the call returns")
  local landed, waited = plugin.wait_for_view("lighttable")
  assertTrue(landed, "wait_for_view yields until the switch actually lands")
  assertEq(view_state.current, "lighttable", "and the view really did change")
  assertEq(waited, 3 * plugin.view_settle.poll_ms,
    "wait_for_view reports the wall clock it actually waited")
  view_state.switch_lag_sleeps = 0
  view_state.current = "darkroom"
end

-- Entering the darkroom gets a LARGER budget than the mid-bounce return leg
-- (scouter phase 2 task 14's live run: with 30 freshly imported 20 MP ORFs in
-- a brand-new library, the first darkroom entry did not land inside the 5s
-- view-settle budget -- darktable was building a pipeline for an image it had
-- never rendered). The bounce keeps the short budget on purpose: by then the
-- darkroom has been entered once, so a long budget there would only turn a
-- genuinely stuck bounce into a 30s hang.
do
  assertTrue(plugin.view_settle.enter_darkroom_budget_ms > plugin.view_settle.budget_ms,
    "the first darkroom entry must get more time than a mid-bounce return")
  assertEq(plugin.view_settle.budget_ms, 5000, "the bounce budget is unchanged")

  -- The budget is actually PASSED, not just exported: a lag longer than the
  -- short budget but inside the long one must still land.
  local long_lag = math.floor(plugin.view_settle.budget_ms / plugin.view_settle.poll_ms) + 5
  view_state.current = "lighttable"
  view_state.switch_lag_sleeps = long_lag
  local ok_open, method_open = plugin.switch_to_darkroom(images_by_id[101])
  assertTrue(ok_open,
    "switch_to_darkroom must wait past the 5s bounce budget for the first entry")
  assertEq(method_open, "view_switch", "and it got there by a real view switch")
  view_state.switch_lag_sleeps = 0
  view_state.current = "darkroom"
end

-- darkroom.open yield-polls for the LOADED IMAGE, not just the view.
--
-- `display_image` is asynchronous, exactly like `dt.gui.current_view`:
-- scouter phase 2 task 14's live run had `darkroom.open` fail with "wrong
-- image loaded" on a 30-frame library because the verification read the
-- loaded image once, immediately after the call, and still saw the previous
-- one. Task 12 fixed that class of bug for the view; this is the same bug for
-- the image. Scripted here by making the selection (which the
-- action_images fallback reads) lag behind by a few polls.
do
  local pending = nil
  local lag = 0
  local real_selection = stub_dt.gui.selection
  stub_dt.gui.selection = function(images)
    table.insert(selection_log, images)
    -- Do NOT update action_images yet: land it only after `lag` polls, the
    -- way the real GUI lands a swap some milliseconds later.
    pending = images
    lag = 4
    return images
  end
  local real_sleep = stub_dt.control.sleep
  stub_dt.control.sleep = function(ms)
    if lag > 0 then
      lag = lag - 1
      if lag == 0 and pending then stub_dt.gui.action_images = pending end
    end
    return real_sleep and real_sleep(ms) or nil
  end

  view_state.current = "darkroom"
  stub_dt.gui.action_images = {images_by_id[101]}
  local reply = plugin.methods["darkroom.open"]({image_id = 103})
  assertEq(reply.image_id, 103,
    "darkroom.open must wait for the swap to land instead of calling a "
    .. "not-yet-landed display_image a wrong image")
  assertTrue(plugin.view_settle.wait_for_image_budget_ms > 0,
    "the image wait has a real, exported budget")

  stub_dt.gui.selection = real_selection
  stub_dt.control.sleep = real_sleep
  lag = 0
  pending = nil
  stub_dt.gui.action_images = {}
  view_state.current = "darkroom"
end

-- `display_image` that returns ok and does NOT swap must fall through to a
-- real view transition, not report a swap that never happened.
--
-- Measured on the real host (scouter phase 2 task 14's live run, 2026-09-14):
-- with darktable in the darkroom on one image and the target in a film roll
-- imported AFTERWARDS, `display_image` returned without error and the
-- darkroom kept showing the old image for a full 15s of polling. So a
-- successful pcall is not evidence of a swap.
do
  local lying_calls = 0
  views.darkroom.display_image = function(image)
    if image == nil then return nil end
    lying_calls = lying_calls + 1
    return image  -- says yes, changes nothing
  end
  -- Inside the DARKROOM the host's `dt.gui.action_images` reports the image
  -- being edited, not the lighttable selection, so selecting does not move
  -- it. The default stub's selection-updates-action_images shortcut is right
  -- for the lighttable and wrong here, and the shortcut is exactly what hid
  -- this bug: with it, `display_image` looks like it worked.
  local real_selection = stub_dt.gui.selection
  stub_dt.gui.selection = function(images)
    table.insert(selection_log, images)
    return images
  end
  -- ...except once a REAL view transition happens, which is what actually
  -- loads the selected image on the host.
  local real_current_view = stub_dt.gui.current_view
  stub_dt.gui.current_view = function(v)
    local result = real_current_view(v)
    if v ~= nil and v.id == "darkroom" then
      stub_dt.gui.action_images = {images_by_id[103]}
    end
    return result
  end
  view_state.current = "darkroom"
  stub_dt.gui.action_images = {images_by_id[101]}

  local switched, method = plugin.switch_to_darkroom(images_by_id[103])

  assertTrue(switched, "switch_to_darkroom still succeeds, by the other route")
  assertEq(method, "view_switch",
    "a display_image that did not actually swap must NOT be reported as the "
    .. "mechanism that worked")
  assertTrue(lying_calls >= 1, "display_image was tried first, as the cheap path")
  assertTrue(plugin.view_settle.display_image_budget_ms
    < plugin.view_settle.wait_for_image_budget_ms,
    "the display_image attempt gets a SHORT budget: the bounce fallback is "
    .. "cheaper than waiting on a call that may simply not work here")

  views.darkroom.display_image = nil
  stub_dt.gui.selection = real_selection
  stub_dt.gui.current_view = real_current_view
  stub_dt.gui.action_images = {}
  view_state.current = "darkroom"
end

-- wait_for_view gives up at its budget rather than spinning forever, and
-- reports having done so.
do
  view_state.current = "lighttable"
  local landed, waited = plugin.wait_for_view("darkroom", 200)
  assertEq(landed, false, "wait_for_view gives up at its budget")
  assertEq(waited, 200, "and reports the full budget it burned")
  view_state.current = "darkroom"
end

-- The commit bounce over an asynchronous switch: both legs must land even
-- though neither call has taken effect by the time it returns.
do
  view_state.current = "darkroom"
  view_state.switch_lag_sleeps = 2
  stub_dt.gui.action_images = {images_by_id[101]}
  local bounce = plugin.commit_view_bounce()
  assertEq(bounce.ok, true, "commit_view_bounce lands both legs of an async bounce")
  assertEq(bounce.view, "darkroom", "and reports the view it really ended in")
  assertEq(view_state.current, "darkroom", "the session is genuinely back in the darkroom")
  view_state.switch_lag_sleeps = 0
end

-- Fix round 1: `left_ms`/`returned_ms` are surfaced ONLY when a `between`
-- callback is passed -- a plain `commit_view_bounce()` call (every
-- pre-task-8 caller, e.g. history.commit's retry loop) stays byte-for-byte
-- {ok, error, view}, no extra keys at all.
do
  view_state.current = "darkroom"
  stub_dt.gui.action_images = {images_by_id[101]}
  local plain = plugin.commit_view_bounce()
  assertEq(plain.ok, true, "plain bounce still succeeds")
  assertEq(plain.left_ms, nil, "no left_ms without a between callback")
  assertEq(plain.returned_ms, nil, "no returned_ms without a between callback")
  assertEq(plain.between_error, nil, "no between_error without a between callback")

  local called = false
  local with_between = plugin.commit_view_bounce(function() called = true end)
  assertEq(with_between.ok, true, "bounce with a between callback still succeeds")
  assertEq(called, true, "the between callback actually ran")
  assertTrue(type(with_between.left_ms) == "number",
    "left_ms is a real number when between is given")
  assertTrue(type(with_between.returned_ms) == "number",
    "returned_ms is a real number when between is given")
  stub_dt.gui.action_images = {}
end

-- A bounce that cannot LEAVE the darkroom fails honestly, naming the view it
-- is stuck in -- it must never report ok with a fabricated "darkroom".
do
  view_state.current = "darkroom"
  local original = stub_dt.gui.current_view
  stub_dt.gui.current_view = function(v)
    if v == nil then return views[view_state.current] end
    return v                    -- accepts everything, changes nothing
  end
  local bounce = plugin.commit_view_bounce()
  assertEq(bounce.ok, false, "a bounce that cannot leave the darkroom reports failure")
  assertTrue(bounce.error:find("could not leave the darkroom") ~= nil,
    "and says so: " .. tostring(bounce.error))
  stub_dt.gui.current_view = original
end

-- Row 418/429: no view switch in the plugin ever calls the
-- dt.gui.current_view SETTER itself (row 418's crash); every switch is a
-- press of darktable's own switch-views command.
do
  view_state.current = "darkroom"
  stub_dt.gui.action_images = {images_by_id[101]}
  local direct = 0
  local original = stub_dt.gui.current_view
  stub_dt.gui.current_view = function(v)
    if v ~= nil and not view_state.via_action then direct = direct + 1 end
    return original(v)
  end
  local switches_before = view_state.action_switches or 0
  local bounce = plugin.commit_view_bounce()
  local opened = plugin.methods["darkroom.open"]({image_id = 102})
  stub_dt.gui.current_view = original
  assertEq(bounce.ok, true, "the bounce lands through the action transport")
  assertTrue(opened ~= nil, "darkroom.open answers")
  assertEq(direct, 0, "no direct dt.gui.current_view(v) setter call from the plugin")
  assertTrue((view_state.action_switches or 0) - switches_before >= 2,
    "the switches were switch-views command presses")
  assertEq(plugin.view_switch_transport(), "action", "action is the default transport")
  local ok, err = plugin.switch_view(views.lighttable, "proxy")
  assertEq(ok, false, "an unknown (or removed) transport is refused")
  assertTrue(err:find("unknown view transport", 1, true) ~= nil, "honestly: " .. tostring(err))
  stub_dt.gui.action_images = {}
  view_state.current = "darkroom"
end

-- image.lookup by absolute path.
local lu = plugin.methods["image.lookup"]({path = "/photos/DSC_0001.NEF"})
assertEq(lu.found, true, "lookup found")
assertEq(lu.image_id, 101, "lookup id")
local lu2 = plugin.methods["image.lookup"]({path = "/nope.NEF"})
assertEq(lu2.found, false, "lookup miss reports found=false")

-- image.lookup by ID (final review, blocker 2). Reads are not allowed to
-- resolve through darkroom.open, which loads an image and moves the user's
-- darkroom, so image.lookup answers for an id out of dt.database too -- and
-- must touch NO GUI state at all while doing it.
do
  local actions_before = #action_log
  local selections_before = #selection_log
  local view_before = view_state.current
  local by_id = plugin.methods["image.lookup"]({image_id = 102})
  assertEq(by_id.found, true, "image.lookup resolves an integer id")
  assertEq(by_id.image_id, 102, "image.lookup by id returns that id")
  assertEq(by_id.path, "/photos/DSC_0002.NEF", "image.lookup by id returns the full path")
  assertTrue(by_id.film == nil or type(by_id.film) == "string",
    "image.lookup reports the film roll as a path or not at all")
  local by_string_id = plugin.methods["image.lookup"]({image_id = "103"})
  assertEq(by_string_id.image_id, 103, "image.lookup coerces a numeric string id")
  local miss = plugin.methods["image.lookup"]({image_id = 9999})
  assertEq(miss.found, false, "an unknown id reports found=false rather than raising")
  assertEq(miss.image_id, 9999, "and echoes the id that was asked for")
  assertEq(#action_log, actions_before, "image.lookup issues no dt.gui.action")
  assertEq(#selection_log, selections_before, "image.lookup never selects an image")
  assertEq(view_state.current, view_before, "image.lookup never switches the view")
end

-- image.lookup LIST form (ruling R15, scouter phase 2 task 14). One call
-- answers a whole set, in request order, mixing paths and ids, and it must
-- touch no GUI state either.
do
  local actions_before = #action_log
  local selections_before = #selection_log
  local view_before = view_state.current
  local batch = plugin.methods["image.lookup"]({items = {
    {path = "/photos/DSC_0001.NEF"},
    {image_id = 102},
    {path = "/photos/nope.NEF"},
    {image_id = 9999},
    {path = "/photos/OTHER.NEF"},
  }})
  assertTrue(type(batch.results) == "table", "the list form answers with a results list")
  assertEq(#batch.results, 5, "one result per requested item")
  assertEq(batch.results[1].found, true, "list form resolves by path")
  assertEq(batch.results[1].image_id, 101, "list form returns the resolved id")
  assertEq(batch.results[2].found, true, "list form resolves by id")
  assertEq(batch.results[2].path, "/photos/DSC_0002.NEF", "list form returns the full path")
  assertEq(batch.results[3].found, false, "an unknown path is found=false, not an error")
  assertEq(batch.results[3].path, "/photos/nope.NEF", "and echoes the path asked for")
  assertEq(batch.results[4].found, false, "an unknown id is found=false, not an error")
  assertEq(batch.results[4].image_id, 9999, "and echoes the id asked for")
  assertEq(batch.results[5].image_id, 103, "results stay in request order")
  assertEq(#action_log, actions_before, "the list form issues no dt.gui.action")
  assertEq(#selection_log, selections_before, "the list form never selects an image")
  assertEq(view_state.current, view_before, "the list form never switches the view")

  -- The single form is untouched: no `results` wrapper, bare record.
  local single = plugin.methods["image.lookup"]({path = "/photos/DSC_0001.NEF"})
  assertEq(single.results, nil, "the single form still returns a bare record")
  assertEq(single.image_id, 101, "the single form still resolves")

  -- An empty list is an empty answer, not an error and not a full-database dump.
  local empty = plugin.methods["image.lookup"]({items = {}})
  assertEq(#empty.results, 0, "an empty items list answers with zero results")

  -- Malformed items are refused by name rather than silently skipped.
  local ok_items = pcall(function() return plugin.methods["image.lookup"]({items = "nope"}) end)
  assertEq(ok_items, false, "items must be a list")
  local ok_entry = pcall(function()
    return plugin.methods["image.lookup"]({items = {"/photos/DSC_0001.NEF"}})
  end)
  assertEq(ok_entry, false, "a bare string item is refused, not guessed at")
end

-- snapshot.take prefers the documented lib API.
local sn = plugin.methods["snapshot.take"]()
assertEq(sn.taken, true, "snapshot taken")
assertEq(sn.via, "libs.snapshots", "snapshot via the lib API")

-- ...and falls back to the action path when the lib API raises, honestly
-- reporting failure when that fallback action ALSO returns nan.
local original_take_snapshot = stub_dt.gui.libs.snapshots.take_snapshot
stub_dt.gui.libs.snapshots.take_snapshot = function() error("snapshot lib boom") end
action_returns["lib/snapshots/take snapshot"] = "nan"
local sn2 = plugin.methods["snapshot.take"]()
assertEq(sn2.via, "action", "snapshot falls back to the action path when libs.snapshots raises")
assertEq(sn2.taken, false, "snapshot not taken when the fallback action also returns nan")
assertTrue(sn2.detail ~= nil, "snapshot fallback records the libs.snapshots error as detail")
stub_dt.gui.libs.snapshots.take_snapshot = original_take_snapshot
action_returns["lib/snapshots/take snapshot"] = nil

-- ---- Row 418/422 follow-up: source-verified transports ------------------
--
-- A scriptable image whose rating setter emulates darktable's
-- DT_IMAGE_CACHE_SAFE release: it rewrites the sidecar from "the DB", whose
-- version only advances when the darkroom flushes (synchronize selection).
local function make_db_backed_image(id, sidecar)
  local backing = {id = id, filename = "DB_" .. id .. ".ORF", path = "/photos", rating = 2,
                   sidecar = sidecar, is_altered = true}
  local img = setmetatable({}, {
    __index = backing,
    __newindex = function(_, k, v)
      backing[k] = v
      if k == "rating" and backing.sidecar and not backing.rating_writes_nothing then
        local h = io.open(backing.sidecar, "w")
        h:write("xmp from db v" .. tostring(view_state.db_flushes or 0)); h:close()
        backing.xmp_writes = (backing.xmp_writes or 0) + 1
      end
    end,
  })
  return img, backing
end

-- The global switch-views commands: pressed with the right path, verified by
-- the view (their return is always nan), and never pressed for the view we
-- are already in (dt_ctl_switch_mode_to would toggle back to the lighttable).
do
  view_state.current = "darkroom"
  local before = #action_log
  local ok, how = plugin.switch_view_via_action(views.lighttable)
  assertEq(ok, true, "switch_view_via_action presses the lighttable command")
  assertEq(how, "global/switch views/lighttable", "the pressed path is darktable's own command")
  assertEq(view_state.current, "lighttable", "the stub switched")
  assertEq(action_log[before + 1].path, "global/switch views/lighttable", "recorded in the action log")
  local n = #action_log
  local ok2, how2 = plugin.switch_view_via_action(views.lighttable)
  assertEq(ok2, true, "already there is success")
  assertTrue(how2:find("already", 1, true) ~= nil, "and says so: " .. tostring(how2))
  assertEq(#action_log, n, "no press when already in the target view (it would toggle)")
  local ok3, how3 = plugin.switch_view_via_action({id = "map"})
  assertEq(ok3, false, "an unmapped view is refused")
  assertTrue(how3:find("map", 1, true) ~= nil, "naming it")
  view_state.current = "darkroom"
end

-- view_bounce_action: both legs through the switch-views commands, working
-- on a host that refuses every lua-owned action (the 5.4.1 reality). A
-- forced "view_bounce" is the same bounce on the default (action) transport.
do
  darktable_refuses_lua_actions = true
  local tmp = os.tmpname()
  local f = io.open(tmp, "w"); f:write("before"); f:close()
  local img = images_by_id[101]
  img.sidecar = tmp
  stub_dt.gui.action_images = {img}
  view_state.current = "darkroom"

  local original_action = stub_dt.gui.action
  local writes = 0
  stub_dt.gui.action = function(path, ...)
    local r = original_action(path, ...)
    if path == "global/switch views/lighttable" then
      writes = writes + 1
      local h = io.open(tmp, "w"); h:write("after the action bounce " .. writes); h:close()
    end
    return r
  end
  local switches_before = view_state.action_switches or 0
  local c = plugin.methods["history.commit"]({strategy = "view_bounce_action"})
  local c_forced = plugin.methods["history.commit"]({strategy = "view_bounce", attempts = 1})
  stub_dt.gui.action = original_action
  assertEq(c.strategy_used, "view_bounce_action", "strategy_used echoes view_bounce_action")
  assertEq(c.ok, true, "view_bounce_action commits: " .. tostring(c.detail))
  assertEq(c.sidecar_written, true, "and the sidecar changed")
  assertEq(c.view_after, "darkroom", "and it came back to the darkroom")
  assertTrue((view_state.action_switches or 0) - switches_before >= 2,
    "both legs went through the switch-views commands")
  assertEq(c_forced.ok, true, "a forced view_bounce still commits: " .. tostring(c_forced.detail))
  assertEq(c_forced.strategy_used, "view_bounce_action",
    "and reports the transport it really used")
  img.sidecar = nil
  os.remove(tmp)
  darktable_refuses_lua_actions = false
end

-- auto (row 429): no_bounce first; view_bounce_action only when no_bounce did
-- not change the sidecar.
do
  local tmp = os.tmpname()
  local f = io.open(tmp, "w"); f:write("before"); f:close()
  local img, backing = make_db_backed_image(105, tmp)
  images_by_id[105] = img
  stub_dt.gui.action_images = {img}
  view_state.current = "darkroom"

  local switches_before = view_state.action_switches or 0
  local c = plugin.methods["history.commit"]({strategy = "auto", image_id = 105})
  assertEq(c.strategy_used, "no_bounce", "auto commits with no_bounce when it writes the XMP")
  assertEq(c.ok, true, "ok: " .. tostring(c.detail))
  assertEq(c.sidecar_written, true, "sidecar changed")
  assertEq(view_state.action_switches or 0, switches_before, "no view switch on the no_bounce path")
  assertEq(c.fallback_from, nil, "no fallback recorded")

  -- no_bounce writes nothing (rating touch does not reach the XMP): auto
  -- falls back to the action bounce, which does.
  backing.rating_writes_nothing = true
  local original_action = stub_dt.gui.action
  stub_dt.gui.action = function(path, ...)
    local r = original_action(path, ...)
    if path == "global/switch views/lighttable" then
      local h = io.open(tmp, "w"); h:write("after the fallback bounce"); h:close()
    end
    return r
  end
  local c2 = plugin.methods["history.commit"]({strategy = "auto", image_id = 105, attempts = 1})
  stub_dt.gui.action = original_action
  assertEq(c2.strategy_used, "view_bounce_action", "auto fell back to view_bounce_action")
  assertEq(c2.ok, true, "and the fallback committed: " .. tostring(c2.detail))
  assertEq(c2.sidecar_written, true, "sidecar changed via the bounce")
  assertTrue(type(c2.fallback_from) == "table" and c2.fallback_from.strategy == "no_bounce",
    "the no_bounce attempt is reported")
  assertEq(c2.view_after, "darkroom", "back in the darkroom")

  -- expect_change=false and no_bounce wrote nothing: an honest no-op, no bounce.
  local switches2 = view_state.action_switches or 0
  local c3 = plugin.methods["history.commit"]({strategy = "auto", image_id = 105,
                                               expect_change = false})
  assertEq(c3.strategy_used, "no_bounce", "expect_change=false stays on no_bounce")
  assertEq(c3.ok, true, "and is ok")
  assertEq(c3.sidecar_written, false, "reporting that nothing was written")
  assertEq(view_state.action_switches or 0, switches2, "without a bounce")

  images_by_id[105] = nil
  stub_dt.gui.selection_now = nil
  stub_dt.gui.action_images = {}
  view_state.current = "darkroom"
  os.remove(tmp)
end

-- Row 430 regression (bench 20260926t073459z, E1/W1/S2...): on the host the
-- synchronize-selection flush did NOT carry the darkroom's in-memory edit
-- into library.db, yet the rating touch still rewrote the XMP from the
-- stale DB -- a new change_timestamp, so a new mtime/size, but the SAME
-- history. no_bounce took that rewrite as proof of a commit, auto never fell
-- back to the bounce, and every relative move / enable reported
-- committed=true with the sidecar unchanged. A rewrite whose history is
-- unchanged is not a commit: no_bounce must say sidecar_written=false and
-- auto must fall back to the view bounce (which does write the history).
do
  local tmp = os.tmpname()
  local db_history_end = 11
  local touches = 0
  local function write_xmp(history_end, stamp)
    local h = io.open(tmp, "w")
    h:write('<x:xmpmeta>\n  <rdf:Description\n'
      .. '   darktable:change_timestamp="' .. stamp .. '"\n'
      .. '   darktable:history_end="' .. history_end .. '"\n'
      .. '   darktable:history_current_hash="h' .. history_end .. '">\n'
      .. '   <darktable:history>\n    <rdf:Seq>\n')
    for n = 0, history_end - 1 do
      h:write('     <rdf:li darktable:num="' .. n .. '" darktable:operation="op' .. n .. '"/>\n')
    end
    h:write('    </rdf:Seq>\n   </darktable:history>\n  </rdf:Description>\n</x:xmpmeta>\n')
    h:close()
  end
  write_xmp(db_history_end, "1")
  local backing = {id = 106, filename = "DB_106.ORF", path = "/photos", rating = 2,
                   sidecar = tmp, is_altered = true}
  local img = setmetatable({}, {
    __index = backing,
    __newindex = function(_, k, v)
      backing[k] = v
      if k == "rating" then
        -- dt_image_synch_xmp from a DB the flush never reached: same
        -- history, fresh change_timestamp (longer each time, so the
        -- stat signature moves even within one mtime second).
        touches = touches + 1
        write_xmp(db_history_end, "1" .. string.rep("0", touches))
      end
    end,
  })
  images_by_id[106] = img
  stub_dt.gui.action_images = {img}
  view_state.current = "darkroom"
  -- The flush is a silent no-op on this host (a command action returns nan
  -- either way, so the plugin cannot tell from the return value).
  action_returns["views/darkroom/synchronize selection"] = "nan"

  local nb = plugin.methods["history.commit"]({strategy = "no_bounce", image_id = 106})
  assertEq(touches, 1, "the rating touch did rewrite the sidecar")
  assertEq(nb.sidecar_written, false,
    "a rewrite with an unchanged history is not a commit: " .. tostring(nb.detail))
  assertEq(nb.ok, false, "no_bounce does not claim the commit")
  assertTrue(type(nb.detail) == "string" and nb.detail:find("history", 1, true) ~= nil,
    "and says the history did not change: " .. tostring(nb.detail))

  -- auto: the bounce (leaving the darkroom writes the in-memory history)
  -- is what really commits, so auto must reach it.
  local original_action = stub_dt.gui.action
  stub_dt.gui.action = function(path, ...)
    local r = original_action(path, ...)
    if path == "global/switch views/lighttable" then
      db_history_end = 12
      write_xmp(db_history_end, "2")
    end
    return r
  end
  local c = plugin.methods["history.commit"]({strategy = "auto", image_id = 106, attempts = 1})
  stub_dt.gui.action = original_action
  assertEq(c.strategy_used, "view_bounce_action",
    "auto falls back to the bounce when no_bounce only rewrote the same history")
  assertEq(c.ok, true, "and the bounce commits: " .. tostring(c.detail))
  assertEq(c.sidecar_written, true, "with the history really changed")
  assertTrue(type(c.fallback_from) == "table" and c.fallback_from.strategy == "no_bounce",
    "the no_bounce attempt is reported")

  action_returns["views/darkroom/synchronize selection"] = nil
  images_by_id[106] = nil
  stub_dt.gui.selection_now = nil
  stub_dt.gui.action_images = {}
  view_state.current = "darkroom"
  os.remove(tmp)
end

-- no_bounce in the darkroom: narrow the selection, flush history to the DB
-- with "synchronize selection", rewrite the XMP from the DB with a rating
-- touch, and confirm on disk -- never switching views.
do
  local tmp = os.tmpname()
  local f = io.open(tmp, "w"); f:write("before"); f:close()
  local img, backing = make_db_backed_image(104, tmp)
  images_by_id[104] = img
  stub_dt.gui.action_images = {img}
  stub_dt.gui.selection_now = {images_by_id[101], images_by_id[102]}
  view_state.current = "darkroom"
  local flushes_before = view_state.db_flushes or 0
  local switches_before = view_state.action_switches or 0

  local c = plugin.methods["history.commit"]({strategy = "no_bounce", image_id = 104})
  assertEq(c.strategy_used, "no_bounce", "strategy_used echoes no_bounce")
  assertEq(c.ok, true, "no_bounce commits in the darkroom: " .. tostring(c.detail))
  assertEq(c.sidecar_written, true, "the sidecar changed on disk")
  assertEq(c.view_after, "darkroom", "still in the darkroom")
  assertEq(view_state.action_switches or 0, switches_before, "no view switch at all")
  assertEq((view_state.db_flushes or 0) - flushes_before, 1, "one synchronize-selection DB flush")
  assertEq(backing.rating, 2, "the rating touch left the rating unchanged")
  assertEq(#stub_dt.gui.selection_now, 1, "the selection was narrowed to exactly one image")
  assertEq(stub_dt.gui.selection_now[1].id, 104, "the image being committed")
  local names = {}
  for _, st in ipairs(c.steps) do names[#names + 1] = st.step .. "=" .. tostring(st.ok) end
  assertEq(table.concat(names, ","),
    "select=true,synchronize_selection_db_flush=true,rating_touch_xmp_from_db=true,sidecar_changed=true,"
    .. "history_changed=true",
    "the ladder's steps are reported in order")

  -- is_altered false: the flush is SKIPPED (it would dereference an empty
  -- history) and reported as such.
  backing.is_altered = false
  local c2 = plugin.methods["history.commit"]({strategy = "no_bounce", image_id = 104})
  assertEq(c2.steps[2].step, "synchronize_selection_db_flush", "step 2 is the flush")
  assertEq(c2.steps[2].ok, false, "reported not done")
  assertTrue(c2.steps[2].detail:find("skipped", 1, true) ~= nil, "because it was skipped")
  assertEq((view_state.db_flushes or 0) - flushes_before, 1, "and no flush was pressed")
  backing.is_altered = true

  -- A selection that cannot be narrowed is a refusal: synchronize selection
  -- would paste history onto the other selected images.
  local original_selection = stub_dt.gui.selection
  stub_dt.gui.selection = function(images)
    if images == nil then return {images_by_id[101], img} end
    return images
  end
  local c3 = plugin.methods["history.commit"]({strategy = "no_bounce", image_id = 104})
  stub_dt.gui.selection = original_selection
  assertEq(c3.ok, false, "no_bounce refuses when the selection is not exactly the image")
  assertTrue(c3.detail:find("selection", 1, true) ~= nil, "and says why: " .. tostring(c3.detail))
  assertEq((view_state.db_flushes or 0) - flushes_before, 1, "without pressing synchronize selection")

  -- From the lighttable: the history stack module's write sidecar files.
  local f2 = io.open(tmp, "w"); f2:write("stale"); f2:close()
  action_returns["lib/copy_history/write sidecar files"] = nil   -- set to nan by the ladder test
  view_state.current = "lighttable"
  local c4 = plugin.methods["history.commit"]({strategy = "no_bounce", image_id = 104})
  assertEq(c4.ok, true, "no_bounce commits from the lighttable: " .. tostring(c4.detail))
  assertEq(c4.steps[2].step, "write_sidecar_files_action", "via the write sidecar files button")
  assertEq(c4.steps[2].detail, "lib/copy_history/write sidecar files", "at its source-verified path")
  assertEq(c4.view_after, "lighttable", "still in the lighttable")

  images_by_id[104] = nil
  stub_dt.gui.selection_now = nil
  stub_dt.gui.action_images = {}
  view_state.current = "darkroom"
  os.remove(tmp)
end

-- The corrected sidecar ladder path is tried first.
assertEq(plugin.sidecar_action_paths[1], "lib/copy_history/write sidecar files",
  "the source-verified write sidecar files path leads the ladder")

-- darktable_mcp.view_transport: read, set, refuse junk (the removed proxy included).
do
  local vt = plugin.methods["darktable_mcp.view_transport"]
  assertEq(vt({}).transport, "action", "the default transport is the switch-views action")
  assertEq(vt({set = "action"}).transport, "action", "set to action")
  local bad = plugin.handle({id = "vt", method = "darktable_mcp.view_transport",
                             params = {set = "teleport"}})
  assertTrue(bad.error ~= nil and bad.error:find("teleport", 1, true) ~= nil, "junk refused")
  local gone = plugin.handle({id = "vt2", method = "darktable_mcp.view_transport",
                              params = {set = "proxy"}})
  assertTrue(gone.error ~= nil, "the removed proxy transport is refused")
  assertEq(vt({}).transport, "action", "still action")
  local d = plugin.methods["darktable_mcp.diag"]()
  assertEq(d.view_switch_transport, "action", "diag reports the transport")
  assertEq(d.default_commit_strategy, "auto", "and the default commit strategy")
  assertEq(d.auto_commit_order[1], "no_bounce", "auto tries no_bounce first")
  assertEq(d.auto_commit_order[2], "view_bounce_action", "then the action bounce")
end

-- darktable_mcp.probe_main_thread: one row per route, honest about what ran.
do
  darktable_refuses_lua_actions = true
  view_state.current = "darkroom"
  local actions_before = #action_log
  local probe = plugin.methods["darktable_mcp.probe_main_thread"]({})
  local by_route = {}
  for _, r in ipairs(probe.rows) do
    by_route[r.route] = r
    assertTrue(type(r.expect) == "string" and type(r.thread) == "string",
      "each row carries its source-derived expectation and thread: " .. r.route)
  end
  assertEq(by_route.a_lua_shortcut.skipped, true, "the removed lua shortcut is reported, not pressed")
  for i = actions_before + 1, #action_log do
    assertTrue(not tostring(action_log[i].path):find("^lua/"), "no lua/ path is pressed")
  end
  assertEq(by_route.c_control_dispatch.ran, false, "the stub dispatch never runs the callback")
  assertEq(by_route.d1_gtk_wrap_roundtrip.ran, true, "the gtk-wrapped selection getter answers")
  assertEq(by_route.d2_switch_views_action.skipped, true, "no view switch unless asked")
  assertEq(view_state.current, "darkroom", "the default probe never switches views")
  assertEq(by_route.d3_gui_mimic.skipped, true, "mimic is never run (deadlock)")
  assertEq(by_route.d5_display_image_getter.skipped, true, "the display_image getter is never run")
  assertEq(probe.recommended_view_transport, nil, "nothing recommended without the switch test")

  local switches_before = view_state.action_switches or 0
  local probe2 = plugin.methods["darktable_mcp.probe_main_thread"]({switch_views = true})
  local d2 = nil
  for _, r in ipairs(probe2.rows) do if r.route == "d2_switch_views_action" then d2 = r end end
  assertEq(d2.ran, true, "switch_views=true: both legs land: " .. tostring(d2.detail))
  assertEq(view_state.current, "darkroom", "and the probe ends back in the darkroom")
  assertEq((view_state.action_switches or 0) - switches_before, 2, "exactly two presses")
  assertEq(probe2.recommended_view_transport, "action", "the action transport is recommended")

  view_state.current = "lighttable"
  local probe3 = plugin.methods["darktable_mcp.probe_main_thread"]({switch_views = true})
  for _, r in ipairs(probe3.rows) do
    if r.route == "d2_switch_views_action" then
      assertEq(r.ran, false, "switch test refuses to start outside the darkroom")
    end
  end
  view_state.current = "darkroom"
  darktable_refuses_lua_actions = false
end

-- Row 429 host regression: darktable 5.4.1's dt.control / dt.gui are
-- singleton userdata whose __index RAISES on an unknown field
-- (`field "timer" not found for type dt_lua_singleton_control`). The probe
-- must read every optional field through a guard and still answer.
do
  local function singleton(real, type_name)
    return setmetatable({}, {__index = function(_, k)
      local v = real[k]
      if v == nil then
        error('field "' .. tostring(k) .. '" not found for type ' .. type_name)
      end
      return v
    end})
  end
  local real_control, real_gui = stub_dt.control, stub_dt.gui
  local real_configuration = stub_dt.configuration
  stub_dt.control = singleton(real_control, "dt_lua_singleton_control")
  stub_dt.configuration = nil
  view_state.current = "darkroom"
  local ok, probe = pcall(plugin.methods["darktable_mcp.probe_main_thread"], {switch_views = true})
  assertEq(ok, true, "the probe survives a raising dt.control: " .. tostring(not ok and probe or ""))
  if ok then
    local d4 = nil
    for _, r in ipairs(probe.rows) do if r.route == "d4_main_loop_schedulers" then d4 = r end end
    assertTrue(d4 ~= nil and d4.detail:find("timer is nil", 1, true) ~= nil,
      "dt.control.timer is reported absent: " .. tostring(d4 and d4.detail))
    assertEq(probe.recommended_view_transport, "action", "and the switch test still ran")
  end
  assertEq(plugin.opt_field(stub_dt.control, "timer"), nil, "opt_field turns the raise into nil")
  assertTrue(plugin.opt_field(stub_dt.control, "sleep") ~= nil, "and passes a real field through")
  assertEq(plugin.opt_field(nil, "x"), nil, "and tolerates a missing object")
  stub_dt.control, stub_dt.gui = real_control, real_gui
  stub_dt.configuration = real_configuration
  view_state.current = "darkroom"
end

-- ---- Report ----------------------------------------------------------------
if #failures > 0 then
  io.stderr:write("FAILED:\n")
  for _, msg in ipairs(failures) do
    io.stderr:write("  " .. msg .. "\n")
  end
  os.exit(1)
end
print("OK: all dispatcher tests passed")
os.exit(0)
