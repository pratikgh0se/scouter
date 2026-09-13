-- Scouter phase 1 task 12 fix probe 3: find a real commit trigger.
--
-- Probe 2 established the hard fact: with write_sidecar_files="on import"
-- (this host's setting, and darktable's default), the lighttable<->darkroom
-- view bounce does NOT write the sidecar for an image already in the library.
-- The mtime phase 0 and task 1 saw advancing was the IMPORT write, because
-- both probes ran against a throwaway configdir that had to import the ORF
-- first.
--
-- So: is there anything else? Candidates, in order of preference:
--   1. a "write sidecar files" ACTION reachable from the lighttable, where
--      that panel actually lives (task 1 only ever tried it from the
--      darkroom, where lib/ actions for lighttable panels are not present);
--   2. the dt.gui.libs inventory, in case the module exposes a method;
--   3. dt.preferences read/write of the core write_sidecar_files key -- if it
--      is readable, Scouter can at least fail with an actionable message
--      instead of a bare timeout; if it is writable, "after edit" is a real
--      commit mechanism.
--
-- Every field access on a dt image userdata is pcall-guarded: indexing a name
-- the build does not have RAISES rather than returning nil, which is what
-- aborted probe 2 halfway through.
local dt = require("darktable")

local OUT = os.getenv("SCOUTER_PROBE_OUT") or "/tmp/scouter-sidecar-probe2.jsonl"
local XMP = os.getenv("SCOUTER_PROBE_XMP") or ""
local POLL_MS = 50

local function log_line(s)
  local f = io.open(OUT, "a")
  if f then f:write(s .. "\n"); f:close() end
  if dt.print_log then dt.print_log("scouter-sidecar-probe2: " .. s) end
end

local function jstr(s)
  return '"' .. tostring(s):gsub('\\', '\\\\'):gsub('"', '\\"'):gsub('\n', ' ') .. '"'
end

local function record(name, value, note)
  log_line(string.format('{"probe":%s,"value":%s,"at":%s,"note":%s}',
    jstr(name), jstr(value), jstr(os.date("%H:%M:%S")),
    note and jstr(note) or "null"))
end

local function xmp_mtime()
  if XMP == "" then return "0" end
  local p = io.popen("stat -f %m '" .. XMP .. "' 2>/dev/null")
  if not p then return "0" end
  local v = p:read("*a") or ""
  p:close()
  v = v:gsub("%s+", "")
  return v ~= "" and v or "0"
end

local function view_name()
  local ok, v = pcall(dt.gui.current_view)
  if not ok or v == nil then return "unknown" end
  if type(v) == "string" then return v end
  return tostring(v.id or v.name or "unknown")
end

local function wait_view(target, budget_ms)
  local waited = 0
  while true do
    if view_name() == target then return true, waited end
    if waited >= budget_ms then return false, waited end
    dt.control.sleep(POLL_MS)
    waited = waited + POLL_MS
  end
end

local function keys_of(tbl)
  local names = {}
  pcall(function()
    for k in pairs(tbl) do names[#names + 1] = tostring(k) end
    local mt = getmetatable(tbl)
    if type(mt) == "table" and type(mt.__index) == "table" then
      for k in pairs(mt.__index) do names[#names + 1] = "mt:" .. tostring(k) end
    end
  end)
  table.sort(names)
  return table.concat(names, ",")
end

local function try_action(label, path, element, effect)
  local before = xmp_mtime()
  local ok, res = pcall(dt.gui.action, path, 0, element, effect, 1)
  local val = "raised: " .. tostring(res)
  if ok then
    if type(res) == "number" and res ~= res then val = "nan" else val = tostring(res) end
  end
  dt.control.sleep(1200)
  local after = xmp_mtime()
  record(label, after, string.format("%s [%s/%s] -> %s; xmp %s -> %s, wrote=%s",
    path, element, effect, val, before, after, tostring(after ~= before)))
  return after ~= before
end

local function probe()
  wait_view("darkroom", 90000)
  record("view", view_name())

  local image
  local v = dt.gui.views and dt.gui.views.darkroom
  if v and type(v.display_image) == "function" then
    local ok, img = pcall(v.display_image)
    if ok then image = img end
  end
  record("image", image and tostring(image.filename) or "nil",
         "xmp=" .. xmp_mtime())

  -- Read-only image facts that might name a sidecar API.
  for _, field in ipairs({"sidecar", "is_altered", "has_txt"}) do
    local ok, val = pcall(function() return image[field] end)
    record("image_field_" .. field, ok and tostring(val) or "raises",
           ok and nil or "indexing this field raises on this build")
  end

  -- Panel inventory: is there a lib that can write a sidecar?
  record("libs", keys_of(dt.gui.libs or {}))
  for _, name in ipairs({"image", "metadata", "select", "copy_history", "styles"}) do
    local lib = dt.gui.libs and dt.gui.libs[name]
    if lib then record("lib_" .. name, keys_of(lib)) end
  end

  -- Core preference reachability. `script` = "darktable" is the undocumented
  -- spelling for a core conf key; if it answers, Scouter can report the real
  -- reason a commit cannot land.
  for _, args in ipairs({
    {"darktable", "write_sidecar_files", "string"},
    {"darktable", "write_sidecar_files", "enum"},
    {"", "write_sidecar_files", "string"},
  }) do
    local ok, val = pcall(dt.preferences.read, args[1], args[2], args[3])
    record("pref_read_" .. args[1] .. "_" .. args[3],
           ok and tostring(val) or "raises", "script=" .. jstr(args[1]))
  end

  -- An edit, so there is new history that a commit would have to flush.
  pcall(dt.gui.action, "iop/exposure", 0, "enable", "on", 1)
  pcall(dt.gui.action, "iop/exposure/exposure", 0, "value", "set", 0.7)
  dt.control.sleep(1000)
  record("xmp_after_edit", xmp_mtime())

  -- Candidate 1: the sidecar-write action, from the LIGHTTABLE this time,
  -- with the image selected -- that is where the "selected image[s]" panel
  -- actually exists.
  if dt.gui.selection and image then pcall(dt.gui.selection, {image}) end
  pcall(dt.gui.current_view, dt.gui.views.lighttable)
  local left, lms = wait_view("lighttable", 10000)
  record("in_lighttable", view_name(), "landed=" .. tostring(left) .. " in " .. lms .. "ms")
  dt.control.sleep(500)
  if dt.gui.selection and image then pcall(dt.gui.selection, {image}) end

  local wrote = false
  for _, spec in ipairs({
    {"act_selected_bracket", "lib/selected image[s]/write sidecar files", "button", "activate"},
    {"act_selected_plain", "lib/selected images/write sidecar files", "button", "activate"},
    {"act_image_panel", "lib/image/write sidecar files", "button", "activate"},
    {"act_metadata_panel", "lib/metadata/write sidecar files", "button", "activate"},
    {"act_no_element", "lib/selected image[s]/write sidecar files", "", "activate"},
  }) do
    if try_action(spec[1], spec[2], spec[3], spec[4]) then wrote = true; break end
  end
  record("action_from_lighttable_wrote", tostring(wrote))

  -- Candidate 3: flip the core preference, then bounce. This configdir is a
  -- throwaway, so changing it here touches nothing the user owns.
  local ok_w, err_w = pcall(dt.preferences.write, "darktable", "write_sidecar_files",
                            "string", "after edit")
  record("pref_write", ok_w and "ok" or "raises", tostring(err_w))

  pcall(dt.gui.current_view, dt.gui.views.darkroom)
  wait_view("darkroom", 10000)
  dt.control.sleep(500)
  local before = xmp_mtime()
  pcall(dt.gui.action, "iop/exposure/exposure", 0, "value", "set", 0.35)
  dt.control.sleep(1500)
  record("xmp_after_edit_with_pref", xmp_mtime(),
         "xmp " .. before .. " -> " .. xmp_mtime())

  before = xmp_mtime()
  pcall(dt.gui.current_view, dt.gui.views.lighttable)
  wait_view("lighttable", 10000)
  dt.control.sleep(300)
  pcall(dt.gui.current_view, dt.gui.views.darkroom)
  wait_view("darkroom", 10000)
  dt.control.sleep(1500)
  record("xmp_after_bounce_with_pref", xmp_mtime(),
         "xmp " .. before .. " -> " .. xmp_mtime() .. ", wrote="
         .. tostring(xmp_mtime() ~= before))

  record("final_view", view_name())
  log_line('{"probe":"done"}')
end

dt.control.dispatch(probe)
