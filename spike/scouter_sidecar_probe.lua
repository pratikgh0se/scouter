-- Scouter phase 1 task 12 fix probe 2: what actually makes darktable write
-- the XMP for an image that is ALREADY in the library?
--
-- The view bounce lands reliably now (probe 1), but the sidecar did not
-- advance on the host's real config, where write_sidecar_files="on import".
-- "on import" means the XMP is written when the file is registered -- which
-- is exactly what made phase 0 / task 1 read an advancing mtime and credit
-- the bounce for it.
--
-- So this probe stats the sidecar itself (Lua has no stat; `stat -f %m` via
-- io.popen does) before and after each candidate, and dumps an inventory of
-- every sidecar/xmp-shaped name the Lua API actually exposes on this build.
local dt = require("darktable")

local OUT = os.getenv("SCOUTER_PROBE_OUT") or "/tmp/scouter-sidecar-probe.jsonl"
local XMP = os.getenv("SCOUTER_PROBE_XMP") or ""
local POLL_MS = 50

local function log_line(s)
  local f = io.open(OUT, "a")
  if f then f:write(s .. "\n"); f:close() end
  if dt.print_log then dt.print_log("scouter-sidecar-probe: " .. s) end
end

local function jstr(s)
  return '"' .. tostring(s):gsub('\\', '\\\\'):gsub('"', '\\"'):gsub('\n', ' ') .. '"'
end

local function record(name, value, note)
  log_line(string.format('{"probe":%s,"value":%s,"at":%s,"note":%s}',
    jstr(name), jstr(value), jstr(os.date("%H:%M:%S")),
    note and jstr(note) or "null"))
end

-- Sidecar mtime in seconds, as a string. "0" when there is no sidecar.
local function xmp_mtime()
  if XMP == "" then return "0" end
  local p = io.popen("stat -f %m '" .. XMP .. "' 2>/dev/null")
  if not p then return "0" end
  local v = p:read("*a") or ""
  p:close()
  return (v:gsub("%s+", "")) ~= "" and (v:gsub("%s+", "")) or "0"
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

-- Names on a table, including anything a metatable's __index exposes as a
-- plain table. Only the sidecar/xmp-shaped ones are interesting.
local function inventory(name, tbl)
  local names = {}
  local ok = pcall(function()
    for k in pairs(tbl) do names[#names + 1] = tostring(k) end
    local mt = getmetatable(tbl)
    if type(mt) == "table" and type(mt.__index) == "table" then
      for k in pairs(mt.__index) do names[#names + 1] = "mt:" .. tostring(k) end
    end
  end)
  table.sort(names)
  record("inventory_" .. name, table.concat(names, ","),
         ok and nil or "enumeration raised")
end

local function probe()
  wait_view("darkroom", 90000)
  record("view", view_name())
  record("xmp_after_launch", xmp_mtime(), "written at import time if at all")

  local image
  local v = dt.gui.views and dt.gui.views.darkroom
  if v and type(v.display_image) == "function" then
    local ok, img = pcall(v.display_image)
    if ok then image = img end
  end
  if not image then
    local ok, imgs = pcall(function() return dt.gui.action_images end)
    if ok and type(imgs) == "table" then image = imgs[1] end
  end
  record("image", image and tostring(image.filename) or "nil")

  inventory("dt", dt)
  inventory("dt_database", dt.database)
  if image then inventory("image", image) end

  -- An edit, so there is genuinely new history to write.
  pcall(dt.gui.action, "iop/exposure", 0, "enable", "on", 1)
  pcall(dt.gui.action, "iop/exposure/exposure", 0, "value", "set", 1.2)
  dt.control.sleep(1000)
  record("xmp_after_edit", xmp_mtime(), "no commit attempted yet")

  -- Candidate 1: the view bounce (the strategy phase 1 adopted).
  pcall(dt.gui.current_view, dt.gui.views.lighttable)
  local left, lms = wait_view("lighttable", 10000)
  dt.control.sleep(300)
  pcall(dt.gui.current_view, dt.gui.views.darkroom)
  local back, bms = wait_view("darkroom", 10000)
  dt.control.sleep(1500)
  record("xmp_after_view_bounce", xmp_mtime(),
         string.format("left=%s in %dms, back=%s in %dms", tostring(left), lms,
                       tostring(back), bms))

  -- Candidate 2: image:write_sidecar_file(), if this build has it.
  if image and type(image.write_sidecar_file) == "function" then
    local ok, err = pcall(function() return image:write_sidecar_file() end)
    dt.control.sleep(1000)
    record("xmp_after_image_write_sidecar_file", xmp_mtime(),
           "pcall ok=" .. tostring(ok) .. " err=" .. tostring(err))
  else
    record("xmp_after_image_write_sidecar_file", "absent",
           "image:write_sidecar_file is not a function on this build")
  end

  -- Candidate 3: dt.database.write_xmp / dt.database.write_sidecar_files.
  for _, fname in ipairs({"write_xmp", "write_sidecar_files", "write_sidecar_file"}) do
    local fn = dt.database and dt.database[fname]
    if type(fn) == "function" then
      local ok, err = pcall(fn, image)
      dt.control.sleep(1000)
      record("xmp_after_dt_database_" .. fname, xmp_mtime(),
             "pcall ok=" .. tostring(ok) .. " err=" .. tostring(err))
    else
      record("xmp_after_dt_database_" .. fname, "absent", "not a function")
    end
  end

  -- Candidate 4: the sidecar-write ACTION paths, retried with the image
  -- selected and the lighttable current -- "lib/..." actions address
  -- lighttable panels, which is where the selected-images module lives.
  if dt.gui.selection and image then pcall(dt.gui.selection, {image}) end
  pcall(dt.gui.current_view, dt.gui.views.lighttable)
  wait_view("lighttable", 10000)
  dt.control.sleep(300)
  for _, path in ipairs({
    "lib/selected image[s]/write sidecar files",
    "lib/selected images/write sidecar files",
    "lib/metadata/write sidecar files",
    "lib/image/write sidecar files",
  }) do
    local ok, res = pcall(dt.gui.action, path, 0, "button", "activate", 1)
    local val = "raised"
    if ok then
      if type(res) == "number" and res ~= res then val = "nan" else val = tostring(res) end
    end
    dt.control.sleep(800)
    record("action_from_lighttable", xmp_mtime(), path .. " -> " .. val)
  end

  -- Back to the darkroom so the session ends honestly.
  pcall(dt.gui.current_view, dt.gui.views.darkroom)
  wait_view("darkroom", 10000)
  record("final_view", view_name(), "xmp=" .. xmp_mtime())
  log_line('{"probe":"done"}')
end

dt.control.dispatch(probe)
