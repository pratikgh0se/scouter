-- Scouter phase 1 task 1 probe: which commit trigger makes darktable write the
-- XMP without leaving the darkroom, and does a global undo action exist?
--
-- Loaded from a throwaway configdir's luarc. Writes one JSON line per probe to
-- $SCOUTER_PROBE_OUT, then exits darktable via dt.control.dispatch.
--
-- Lua has no millisecond wall clock (os.clock() is CPU time, not wall time),
-- so the view-bounce duration is measured by Python from two marker files'
-- mtimes: this probe only touches them immediately before/after the bounce.
local dt = require("darktable")

local OUT = os.getenv("SCOUTER_PROBE_OUT") or "/tmp/scouter-probe.jsonl"
local BOUNCE_BEFORE = OUT .. ".bounce_before"
local BOUNCE_AFTER = OUT .. ".bounce_after"

local function log_line(s)
  local f = io.open(OUT, "a")
  if f then f:write(s .. "\n"); f:close() end
  if dt.print_log then dt.print_log("scouter-probe: " .. s) end
end

local function touch(path)
  local f = io.open(path, "w")
  if f then f:write("x"); f:close() end
end

local function jstr(s) return '"' .. tostring(s):gsub('\\', '\\\\'):gsub('"', '\\"') .. '"' end

local function record(name, value, ms, note)
  local v = "null"
  if type(value) == "number" then
    if value ~= value then v = jstr("nan") else v = tostring(value) end
  elseif value ~= nil then v = jstr(value) end
  log_line(string.format('{"probe":%s,"value":%s,"ms":%s,"note":%s}',
    jstr(name), v, tostring(ms or 0), note and jstr(note) or "null"))
end

local function now_ms()
  return math.floor(os.clock() * 1000)
end

local function try_action(name, path, element, effect, value)
  local t0 = now_ms()
  local ok, res = pcall(dt.gui.action, path, 0, element, effect, value)
  local ms = now_ms() - t0
  if not ok then record(name, nil, ms, "raised: " .. tostring(res)); return nil end
  record(name, res, ms, path)
  return res
end

local function view_name()
  local ok, v = pcall(dt.gui.current_view)
  if not ok or v == nil then return "unknown" end
  return tostring(v.id or v.name or "unknown")
end

local function probe()
  dt.control.sleep(4000)             -- let the darkroom finish loading
  record("view_at_start", view_name())

  -- Make an edit so there is something to commit.
  try_action("enable_exposure", "iop/exposure", "enable", "on", 1)
  try_action("set_exposure_0_9", "iop/exposure/exposure", "value", "set", 0.9)

  -- C1/C2: sidecar-write actions, three spellings.
  try_action("c1_selected_images", "lib/selected image[s]/write sidecar files",
             "button", "activate", 1)
  record("view_after_c1", view_name())
  try_action("c2_metadata", "lib/metadata/write sidecar files", "button", "activate", 1)
  try_action("c2_image", "lib/image/write sidecar files", "button", "activate", 1)

  -- Bonus probe for the phase-2 note: is there a global undo action?
  try_action("undo_global", "global/undo", "button", "activate", 1)
  try_action("undo_darkroom", "views/darkroom/undo", "button", "activate", 1)

  -- C3: the proven view bounce. now_ms()/os.clock() only gives CPU time, so
  -- that figure is recorded for reference and Python recomputes the real
  -- wall-clock cost from the two marker files' mtimes.
  local t0 = now_ms()
  touch(BOUNCE_BEFORE)
  pcall(dt.gui.current_view, dt.gui.views.lighttable)
  dt.control.sleep(500)
  pcall(dt.gui.current_view, dt.gui.views.darkroom)
  touch(BOUNCE_AFTER)
  record("c3_view_bounce_cpu", 1, now_ms() - t0,
         "lighttable then darkroom (CPU time via os.clock, not wall clock)")
  dt.control.sleep(2000)
  record("view_after_c3", view_name())

  log_line('{"probe":"done"}')
end

dt.control.dispatch(probe)
