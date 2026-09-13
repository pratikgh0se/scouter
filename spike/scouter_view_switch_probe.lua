-- Scouter phase 1 task 12 fix probe: WHY do view switches issued from a
-- dt.control.dispatch coroutine appear not to land?
--
-- Loaded from a throwaway configdir's luarc. One darktable launch answers
-- every candidate cause at once, because a launch is expensive on this host:
--
--   A. bridge semantics -- switch, then read the view IMMEDIATELY (no yield).
--      This is exactly what commit_view_bounce/darkroom.open do today.
--   B. proven-path bounce -- current_view(lighttable), YIELD-poll until it
--      lands, current_view(darkroom), yield-poll until it lands.
--   C. display_image(image) called from the LIGHTTABLE: does it switch the
--      view at all, or only set which image the darkroom would show?
--   D. display_image(image) while already in the darkroom on that image.
--
-- Wall clock: Lua's os.clock() is CPU time. Every "ms" below is counted from
-- the number of dt.control.sleep(POLL_MS) yields actually performed, which is
-- real wall clock at POLL_MS resolution; os.date() is logged alongside so the
-- Python side can cross-check against the marker files' mtimes.
local dt = require("darktable")

local OUT = os.getenv("SCOUTER_PROBE_OUT") or "/tmp/scouter-view-probe.jsonl"
local POLL_MS = 50
local SETTLE_BUDGET_MS = 10000

local function log_line(s)
  local f = io.open(OUT, "a")
  if f then f:write(s .. "\n"); f:close() end
  if dt.print_log then dt.print_log("scouter-view-probe: " .. s) end
end

local function jstr(s)
  return '"' .. tostring(s):gsub('\\', '\\\\'):gsub('"', '\\"') .. '"'
end

local function record(name, value, ms, note)
  log_line(string.format(
    '{"probe":%s,"value":%s,"ms":%s,"at":%s,"note":%s}',
    jstr(name), jstr(value), tostring(ms or 0), jstr(os.date("%H:%M:%S")),
    note and jstr(note) or "null"))
end

local function view_name()
  local ok, v = pcall(dt.gui.current_view)
  if not ok or v == nil then return "unknown" end
  if type(v) == "string" then return v end
  return tostring(v.id or v.name or "unknown")
end

-- Yield-poll until the view is `target`. Returns (landed, elapsed_ms).
local function wait_view(target, budget_ms)
  local waited = 0
  while true do
    if view_name() == target then return true, waited end
    if waited >= (budget_ms or SETTLE_BUDGET_MS) then return false, waited end
    dt.control.sleep(POLL_MS)
    waited = waited + POLL_MS
  end
end

local function darkroom_image()
  local v = dt.gui.views and dt.gui.views.darkroom
  if v and type(v.display_image) == "function" then
    local ok, img = pcall(v.display_image)
    if ok and img then return img end
  end
  local ok, imgs = pcall(function() return dt.gui.action_images end)
  if ok and type(imgs) == "table" then return imgs[1] end
  return nil
end

local function probe()
  -- 1. How long until the view reports darkroom at all, without any traffic?
  local landed, ms = wait_view("darkroom", 90000)
  record("startup_view", view_name(), ms, landed and "reached darkroom" or "never reached darkroom")
  if not landed then log_line('{"probe":"done"}'); return end

  -- Is the worker-coroutine context responsive right after "darkroom"?
  -- This is Finding 2's 25s blind settle under the microscope: count how long
  -- a *yielding* loop takes to see a real dt.gui.action answer (not nan) for
  -- an absolute set, which only works with a live darkroom pipe.
  local probe_ms, got = 0, "never"
  while probe_ms <= 60000 do
    local ok, res = pcall(dt.gui.action, "iop/exposure", 0, "enable", "on", 1)
    if ok and type(res) == "number" and res == res then got = tostring(res); break end
    dt.control.sleep(200)
    probe_ms = probe_ms + 200
  end
  record("pipe_ready_after_darkroom", got, probe_ms,
         "yield-polled iop/exposure enable until it stopped returning nan")

  local image = darkroom_image()
  record("image_loaded", image and tostring(image.filename) or "nil", 0,
         image and ("id=" .. tostring(image.id)) or "darkroom_image() returned nil")

  -- Give the pipe something to commit, so the bounce is the real thing.
  pcall(dt.gui.action, "iop/exposure/exposure", 0, "value", "set", 1.2)
  dt.control.sleep(500)

  -- ---- A. bridge semantics: switch then read the view with no yield -------
  local a_ok1 = pcall(dt.gui.current_view, dt.gui.views.lighttable)
  record("A_view_immediately_after_leaving", view_name(), 0,
         "pcall ok=" .. tostring(a_ok1))
  local a_l, a_lms = wait_view("lighttable", SETTLE_BUDGET_MS)
  record("A_lighttable_after_yield", view_name(), a_lms, "landed=" .. tostring(a_l))

  local a_ok2 = false
  if image then a_ok2 = pcall(dt.gui.views.darkroom.display_image, image) end
  record("A_view_immediately_after_display_image", view_name(), 0,
         "pcall ok=" .. tostring(a_ok2))
  local a_d, a_dms = wait_view("darkroom", SETTLE_BUDGET_MS)
  record("A_darkroom_after_yield", view_name(), a_dms, "landed=" .. tostring(a_d))

  -- ---- B. proven-path bounce with yield-polling between legs --------------
  pcall(dt.gui.current_view, dt.gui.views.darkroom)
  wait_view("darkroom", SETTLE_BUDGET_MS)
  dt.control.sleep(500)

  pcall(dt.gui.current_view, dt.gui.views.lighttable)
  local b_l, b_lms = wait_view("lighttable", SETTLE_BUDGET_MS)
  record("B_leg1_lighttable", view_name(), b_lms, "landed=" .. tostring(b_l))
  pcall(dt.gui.current_view, dt.gui.views.darkroom)
  local b_d, b_dms = wait_view("darkroom", SETTLE_BUDGET_MS)
  record("B_leg2_darkroom", view_name(), b_dms, "landed=" .. tostring(b_d))

  -- ---- C. does display_image alone switch the view from the lighttable? ---
  pcall(dt.gui.current_view, dt.gui.views.lighttable)
  wait_view("lighttable", SETTLE_BUDGET_MS)
  dt.control.sleep(300)
  local c_ok = false
  if image then c_ok = pcall(dt.gui.views.darkroom.display_image, image) end
  local c_d, c_dms = wait_view("darkroom", SETTLE_BUDGET_MS)
  record("C_display_image_from_lighttable", view_name(), c_dms,
         "pcall ok=" .. tostring(c_ok) .. " landed=" .. tostring(c_d))

  -- ---- D. display_image while already in the darkroom on that image -------
  pcall(dt.gui.current_view, dt.gui.views.darkroom)
  wait_view("darkroom", SETTLE_BUDGET_MS)
  local d_ok = false
  if image then d_ok = pcall(dt.gui.views.darkroom.display_image, image) end
  local d_d, d_dms = wait_view("darkroom", SETTLE_BUDGET_MS)
  record("D_display_image_in_darkroom", view_name(), d_dms,
         "pcall ok=" .. tostring(d_ok) .. " landed=" .. tostring(d_d))

  -- Leave the session in the darkroom so the XMP flush on quit is honest.
  pcall(dt.gui.current_view, dt.gui.views.darkroom)
  wait_view("darkroom", SETTLE_BUDGET_MS)
  record("final_view", view_name(), 0, nil)
  log_line('{"probe":"done"}')
end

dt.control.dispatch(probe)
