-- darktable_mcp: long-running plugin that exposes view_photos and
-- rate_photos to the Python MCP server via file-based JSON requests.
--
-- Loaded via `require "darktable_mcp"` from ~/.config/darktable/luarc.
-- Spawns a worker via dt.control.dispatch that polls
-- ~/.cache/darktable-mcp/ for request-*.json files, dispatches them to the
-- method registry, and writes response-<uuid>.json. See:
--   docs/superpowers/specs/2026-04-27-ipc-bridge-mvp-design.md
--
-- The poll cadence is ADAPTIVE (see POLL_*_MS below): 100ms while requests
-- are arriving, backing off to ~1s when the session is idle. The original
-- flat 100ms poll forked an `ls` subprocess ten times a second for the whole
-- lifetime of the user's darktable session, idle or not.

local dt = require("darktable")

-- LuaFileSystem, if the host darktable's Lua has it, lets us list the cache
-- directory in-process and skip the `ls` subprocess entirely. It is NOT part
-- of darktable's guaranteed Lua environment, so this must stay optional.
local lfs_available, lfs = pcall(require, "lfs")
if not lfs_available or type(lfs) ~= "table" or type(lfs.dir) ~= "function" then
  lfs_available, lfs = false, nil
end

-- print_log is the only diagnostic channel a loaded plugin has. Guard it so a
-- stripped-down dt table (or a very old darktable) cannot crash the worker.
local function log(msg)
  if dt and dt.print_log then dt.print_log(msg) end
end

-- ---- Shell + filesystem helpers --------------------------------------------

-- Quote an arbitrary string for safe interpolation into a /bin/sh command.
--
-- Paths here come from $XDG_CACHE_HOME / $HOME and from caller-supplied
-- import paths -- all outside our control. Interpolating one raw into a shell
-- string (the old `'ls -1 "' .. dir .. '"'`) breaks on a path containing a
-- double quote and EXECUTES attacker-chosen text on a path containing a
-- backtick or $(...): double quotes do not suppress command substitution.
--
-- Single quotes do suppress everything. The only byte that cannot appear
-- inside '...' is a single quote itself, encoded as the classic '\'' dance:
-- close the quote, emit an escaped quote, reopen.
local function shell_quote(s)
  return "'" .. tostring(s):gsub("'", "'\\''") .. "'"
end

-- How deep a recursive import will walk. Bounds the work on a mistyped path
-- and cannot loop: neither `find` nor the lfs walk follows symlinked dirs.
local IMPORT_MAX_DEPTH = 8

-- Mode of `path` without following a symlink, so a symlinked directory is
-- skipped rather than descended into (that is what could loop). Returns nil
-- when the filesystem module cannot answer at all.
local function _link_mode(fs, path)
  local stat = fs.symlinkattributes or fs.attributes
  if type(stat) ~= "function" then return nil end
  local ok, mode = pcall(stat, path, "mode")
  if not ok then return nil end
  return mode
end

-- Walk `dir` with a LuaFileSystem-shaped module, appending it and every
-- subdirectory to `out`.
--
-- Returns TRUE only if every directory it touched was actually read. This
-- distinction is the whole point: the previous version appended `dir` to `out`
-- as its very first statement and wrapped the listing in a bare `pcall` whose
-- result it discarded, so a root that does not exist (or cannot be read)
-- produced a one-element `out` that was indistinguishable from a real, empty
-- leaf directory. list_directories_recursive's `#out == 0` failure check was
-- therefore unreachable whenever lfs was present, and import_batch answered
-- `recursive_honoured = true, directories_imported = 1` for a path it had
-- never managed to look at.
local _walk_lfs
_walk_lfs = function(fs, dir, out, depth)
  -- Stat the root BEFORE claiming it. lfs.dir() raises on a missing path, but
  -- some stand-ins simply yield nothing, so check both: an explicit stat when
  -- the module offers one, and the listing itself failing.
  local stat = fs.attributes or fs.symlinkattributes
  if type(stat) == "function" then
    local ok, mode = pcall(stat, dir, "mode")
    if not ok or mode ~= "directory" then return false end
  end

  out[#out + 1] = dir
  if depth >= IMPORT_MAX_DEPTH then return true end

  local entries = {}
  local listed = pcall(function()
    for entry in fs.dir(dir) do
      if entry ~= "." and entry ~= ".." then entries[#entries + 1] = entry end
    end
  end)
  if not listed then return false end

  -- A subdirectory we could not read means the tree we return is a subset of
  -- the real one, so propagate that upward instead of silently truncating.
  local complete = true
  for _, entry in ipairs(entries) do
    local child = dir .. "/" .. entry
    if _link_mode(fs, child) == "directory" then
      if not _walk_lfs(fs, child, out, depth + 1) then complete = false end
    end
  end
  return complete
end

-- Return `dir` plus every subdirectory beneath it, and whether the
-- enumeration actually succeeded. `false` means we could not read the whole
-- tree, so the result is a guess or a subset -- callers must not claim
-- recursion was honoured in that case.
--
-- `fs` is an injection point for the tests: pass a LuaFileSystem-shaped table
-- to exercise the lfs branch on a host (like CI's plain lua5.4) that has no
-- real lfs. Production callers pass nothing and get the real module or the
-- `find` fallback.
local function list_directories_recursive(dir, fs)
  fs = fs or (lfs_available and lfs or nil)
  local out, ok = {}, false
  if fs then
    ok = _walk_lfs(fs, dir, out, 0)
  else
    -- `find` does not follow symlinks without -L, so this cannot loop.
    local p = io.popen("find " .. shell_quote(dir)
      .. " -maxdepth " .. IMPORT_MAX_DEPTH .. " -type d 2>/dev/null")
    if p then
      for line in p:lines() do out[#out + 1] = line end
      p:close()
    end
    -- `find` prints nothing (and exits non-zero) for a path it cannot stat,
    -- so an empty listing IS the failure signal here.
    ok = #out > 0
  end
  if #out == 0 then out = {dir} end
  table.sort(out)   -- parents before children; deterministic import order
  return out, ok
end

-- ---- JSON encode/decode (minimal, MVP-only) --------------------------------
-- darktable Lua does not bundle a JSON library reliably. Inline a tiny
-- encoder/decoder sufficient for our request/response shapes.

local json = {}

-- A sparse integer-keyed table is emitted as a dense array padded with nulls
-- (see the table branch below). Refuse to do that for pathological sparsity
-- -- {[1]=x, [1e9]=y} would otherwise build a billion-element array -- and
-- fall back to the object form instead.
local SPARSE_ARRAY_MAX_HOLES = 1000

local function encode_value(v)
  local t = type(v)
  if t == "nil" then return "null"
  elseif t == "boolean" then return v and "true" or "false"
  elseif t == "number" then
    -- JSON has no inf/nan. tostring() would emit `inf` / `nan` / `-nan`,
    -- which is not valid JSON and reaches the Python client as a
    -- BridgeProtocolError on the WHOLE response -- so one bad float would
    -- discard an otherwise good result. Emit null for the bad value instead
    -- and keep the rest of the document parseable.
    if v ~= v then return "null" end                     -- nan (nan ~= nan)
    if v == math.huge or v == -math.huge then return "null" end
    return tostring(v)
  elseif t == "string" then
    local escaped = v:gsub('\\', '\\\\'):gsub('"', '\\"'):gsub('\n', '\\n'):gsub('\r', '\\r'):gsub('\t', '\\t')
                     :gsub('\b', '\\b'):gsub('\f', '\\f')
    -- Escape any remaining control bytes (0x00-0x1F) as \u00XX.
    escaped = escaped:gsub('[%z\1-\31]', function(c)
      return string.format('\\u%04x', string.byte(c))
    end)
    return '"' .. escaped .. '"'
  elseif t == "table" then
    -- Array vs object detection. The old test was `n == max`, which silently
    -- mis-handled sparseness: {[1]=a, [3]=b} has n=2, max=3, so it fell
    -- through to the object branch and emitted {"1":a,"3":b} -- numeric
    -- string keys, a shape the Python client never expects for a list.
    -- Be explicit instead: classify the key set, then pick a branch.
    local n, max_idx = 0, 0
    local all_array_keys = true   -- every key is a positive whole number
    for k in pairs(v) do
      n = n + 1
      if type(k) == "number" and k >= 1 and k == math.floor(k) then
        if k > max_idx then max_idx = k end
      else
        all_array_keys = false
      end
    end

    if n == 0 then
      return "[]"
    elseif all_array_keys and max_idx == n then
      -- Dense 1..n: a plain array.
      local parts = {}
      for i = 1, n do parts[i] = encode_value(v[i]) end
      return "[" .. table.concat(parts, ",") .. "]"
    elseif all_array_keys and (max_idx - n) <= SPARSE_ARRAY_MAX_HOLES then
      -- Sparse but still array-shaped, e.g. {[1]=a, [3]=b} from a list that
      -- had an element removed. Keep it an ARRAY and fill the holes with
      -- null, which is what the Python client can actually consume.
      local parts = {}
      for i = 1, max_idx do parts[i] = encode_value(v[i]) end
      return "[" .. table.concat(parts, ",") .. "]"
    else
      -- Genuine object (string keys, mixed keys, or absurdly sparse indexes).
      -- Sort by key so the encoding is deterministic -- pairs() order is not.
      local entries = {}
      for k, val in pairs(v) do
        entries[#entries + 1] = {k = tostring(k), v = val}
      end
      table.sort(entries, function(a, b) return a.k < b.k end)
      local parts = {}
      for i = 1, #entries do
        parts[i] = encode_value(entries[i].k) .. ":" .. encode_value(entries[i].v)
      end
      return "{" .. table.concat(parts, ",") .. "}"
    end
  end
  error("cannot encode value of type " .. t)
end

function json.encode(v) return encode_value(v) end

-- Minimal recursive-descent decoder. Adequate for plain JSON requests.
local function skip_ws(s, i)
  while i <= #s and (s:sub(i,i) == " " or s:sub(i,i) == "\t" or s:sub(i,i) == "\n" or s:sub(i,i) == "\r") do
    i = i + 1
  end
  return i
end

local decode_value
local function decode_string(s, i)
  assert(s:sub(i,i) == '"', "expected string at " .. i)
  i = i + 1
  local out = {}
  while i <= #s do
    local c = s:sub(i,i)
    if c == '"' then return table.concat(out), i + 1
    elseif c == "\\" then
      local esc = s:sub(i+1, i+1)
      if esc == "n" then table.insert(out, "\n"); i = i + 2
      elseif esc == "r" then table.insert(out, "\r"); i = i + 2
      elseif esc == "t" then table.insert(out, "\t"); i = i + 2
      elseif esc == "b" then table.insert(out, "\b"); i = i + 2
      elseif esc == "f" then table.insert(out, "\f"); i = i + 2
      elseif esc == '"' or esc == "\\" or esc == "/" then table.insert(out, esc); i = i + 2
      elseif esc == "u" then
        -- \uXXXX: parse 4 hex digits, emit UTF-8 bytes.
        local hex = s:sub(i+2, i+5)
        if #hex ~= 4 or not hex:match("^%x%x%x%x$") then
          error("malformed \\u escape at " .. i)
        end
        local cp = tonumber(hex, 16)
        -- Encode codepoint as UTF-8.
        if cp < 0x80 then
          table.insert(out, string.char(cp))
        elseif cp < 0x800 then
          table.insert(out, string.char(
            0xC0 + math.floor(cp / 0x40),
            0x80 + (cp % 0x40)
          ))
        else
          table.insert(out, string.char(
            0xE0 + math.floor(cp / 0x1000),
            0x80 + (math.floor(cp / 0x40) % 0x40),
            0x80 + (cp % 0x40)
          ))
        end
        i = i + 6
      else error("unsupported escape \\" .. esc)
      end
    else
      table.insert(out, c)
      i = i + 1
    end
  end
  error("unterminated string")
end

local function decode_number(s, i)
  local start = i
  if s:sub(i,i) == "-" then i = i + 1 end
  while i <= #s and s:sub(i,i):match("[%d%.eE%-%+]") do i = i + 1 end
  local num = tonumber(s:sub(start, i-1))
  if num == nil then
    -- This is the catch-all branch of decode_value, so anything that is not
    -- an object/array/string/true/false/null lands here. Returning nil made
    -- json.decode succeed with a nil result for outright garbage, which the
    -- caller then had to guess at. Fail loudly instead.
    error(string.format("invalid JSON at position %d: %q",
      start, s:sub(start, start + 15)))
  end
  return num, i
end

local function decode_array(s, i)
  assert(s:sub(i,i) == "[")
  i = i + 1
  i = skip_ws(s, i)
  local out = {}
  if s:sub(i,i) == "]" then return out, i + 1 end
  while true do
    local v
    v, i = decode_value(s, i)
    table.insert(out, v)
    i = skip_ws(s, i)
    local c = s:sub(i,i)
    if c == "," then i = i + 1; i = skip_ws(s, i)
    elseif c == "]" then return out, i + 1
    else error("expected , or ] at " .. i)
    end
  end
end

local function decode_object(s, i)
  assert(s:sub(i,i) == "{")
  i = i + 1
  i = skip_ws(s, i)
  local out = {}
  if s:sub(i,i) == "}" then return out, i + 1 end
  while true do
    local k
    k, i = decode_string(s, i)
    i = skip_ws(s, i)
    assert(s:sub(i,i) == ":", "expected : at " .. i)
    i = skip_ws(s, i + 1)
    local v
    v, i = decode_value(s, i)
    out[k] = v
    i = skip_ws(s, i)
    local c = s:sub(i,i)
    if c == "," then i = i + 1; i = skip_ws(s, i)
    elseif c == "}" then return out, i + 1
    else error("expected , or } at " .. i)
    end
  end
end

decode_value = function(s, i)
  i = skip_ws(s, i)
  local c = s:sub(i,i)
  if c == "{" then return decode_object(s, i)
  elseif c == "[" then return decode_array(s, i)
  elseif c == '"' then return decode_string(s, i)
  elseif c == "t" and s:sub(i, i+3) == "true" then return true, i + 4
  elseif c == "f" and s:sub(i, i+4) == "false" then return false, i + 5
  elseif c == "n" and s:sub(i, i+3) == "null" then return nil, i + 4
  else return decode_number(s, i)
  end
end

function json.decode(s)
  local v = decode_value(s, 1)
  return v
end

-- ---- Method registry -------------------------------------------------------

local methods = {}

-- import_batch's post-import poll budget.
--
-- This poll runs INSIDE the worker loop and blocks every other bridge request
-- for its duration. It normally exits far earlier, as soon as the count stops
-- growing.
--
-- The old ceiling was 3s, which the per-subdirectory camera layout now
-- routinely outruns: import_from_camera writes one directory per camera
-- folder/card, so a single card import can register several hundred files
-- across many film rolls and darktable's background scan takes longer than
-- 3s to register them all. The Python client budgets 120s for import_batch
-- (DEFAULT_TIMEOUTS in darktable_mcp/bridge/client.py), so ~10s is affordable.
--
-- THREE independent bounds, because the attempt count alone is not a time
-- budget. Each attempt runs count_images_under, a full linear scan of
-- dt.database (there is no index to query -- see the note above view_photos),
-- so on a 30k-image library the scans, not the sleeps, dominate. The old
-- "100 x 100ms = 10s worst case" comment counted only the sleeping and was
-- wrong by however long 100 full library scans take -- potentially minutes of
-- head-of-line blocking, past the client's own 120s budget.
--   * IMPORT_POLL_ATTEMPTS      -- ceiling on scans, so the work is bounded.
--   * IMPORT_POLL_DEADLINE_SECONDS -- real wall clock, so slow scans cannot
--     push the total past it however few attempts they represent.
--   * IMPORT_ZERO_GRACE_POLLS   -- bail out early when NOTHING has appeared.
local IMPORT_POLL_ATTEMPTS = 100
local IMPORT_POLL_INTERVAL_MS = 100
-- Wall-clock ceiling on the whole poll, sleeps AND scans included.
local IMPORT_POLL_DEADLINE_SECONDS = 10
-- Consecutive polls showing no growth before the count is called final.
-- Without this the poll stopped at the FIRST non-zero count, which under-
-- reports a trickling multi-directory import.
local IMPORT_SETTLE_POLLS = 5
-- Attempts to allow before giving up on a count that is still exactly zero.
-- Zero after ~2s is the "bad path / darktable rejected the folder" case, and
-- 80 more full library scans will not rescue it -- they only make the caller
-- wait. Anything that HAS started arriving keeps the full settle behaviour.
local IMPORT_ZERO_GRACE_POLLS = 20

-- darktable's image.path is the parent directory; image.filename is the
-- bare basename. Callers want a single absolute file path they can hand
-- straight to export_images / Read / etc., so join them once here.
local function _image_full_path(image)
  local dir = image.path or ""
  if #dir > 0 and dir:sub(-1) ~= "/" then dir = dir .. "/" end
  return dir .. (image.filename or "")
end

-- KNOWN LIMITATION: this is an unavoidable O(n) linear scan of dt.database on
-- every call. darktable's Lua API exposes no index, query or predicate over
-- the library, so there is nothing to look something up by -- the only way to
-- find images matching a filter is to touch every one of them. On a 30k-image
-- library in interpreted Lua that is genuinely slow, which is why the Python
-- client budgets 30s for `view_photos` (DEFAULT_TIMEOUTS in
-- darktable_mcp/bridge/client.py) rather than the old flat 5s that made a
-- healthy-but-slow call look like "darktable is not running".
--
-- Since the scan itself cannot be avoided, the loop below avoids every bit of
-- per-image work that can be: string.lower(filter) is hoisted out (it used to
-- be recomputed for all n images), the cheapest predicate is tested first
-- (a numeric rating compare short-circuits before any string search), and no
-- result table is constructed for excluded images.
methods.view_photos = function(p)
  p = p or {}
  local out, count = {}, 0
  local limit = p.limit or 100
  local rating_min = p.rating_min
  local filter = p.filter
  -- Hoisted: one lower() for the whole scan instead of one per image.
  local filter_lc = nil
  if filter and filter ~= "" then filter_lc = string.lower(filter) end
  -- Hoisted: skip a global + table lookup per image per predicate.
  local slower, sfind = string.lower, string.find

  for _, image in ipairs(dt.database) do
    if count >= limit then break end
    -- Cheapest predicate first: a number compare costs far less than a
    -- lower() + substring search, and rejecting here skips both.
    local include = true
    if rating_min and (image.rating or 0) < rating_min then
      include = false
    end
    if include and filter_lc then
      if not sfind(slower(image.filename or ""), filter_lc, 1, true) then
        include = false
      end
    end
    if include then
      count = count + 1
      out[count] = {
        id = tostring(image.id),
        filename = image.filename,
        path = _image_full_path(image),
        rating = image.rating or 0,
      }
    end
  end
  return out
end

methods.rate_photos = function(p)
  p = p or {}
  local updated = 0
  for _, photo_id in ipairs(p.photo_ids or {}) do
    local image = dt.database[tonumber(photo_id)]
    if image then
      image.rating = p.rating
      updated = updated + 1
    end
  end
  return {updated = updated}
end

-- Per-image rating writes with HONEST misses.
--
-- w1ne's rate_photos returns only {updated = n} and silently skips ids
-- darktable does not know. For a 300-frame cull that is exactly the case that
-- must be visible, so this verb reports the before/after rating per image and
-- lists the ids it could not find. rate_photos is left untouched for its own
-- MCP tool.
methods["image.rate"] = function(p)
  p = p or {}
  local ids = p.image_ids or {}
  if #ids == 0 then error("image.rate: image_ids required and non-empty") end
  local rating = tonumber(p.rating)
  if rating == nil or rating < -1 or rating > 5 then
    error("image.rate: rating must be -1..5, got " .. tostring(p.rating))
  end
  local rated, missed, per_image = 0, {}, {}
  for _, id in ipairs(ids) do
    local image = dt.database[tonumber(id)]
    if not image then
      table.insert(missed, tonumber(id))
    else
      local before = image.rating or 0
      local ok, err = pcall(function() image.rating = rating end)
      if ok then
        rated = rated + 1
        table.insert(per_image, {image_id = tonumber(id), rating_before = before,
                                 rating_after = rating})
      else
        table.insert(missed, tonumber(id))
        table.insert(per_image, {image_id = tonumber(id), rating_before = before,
                                 rating_after = before, error = tostring(err)})
      end
    end
  end
  return {rating = rating, rated = rated, missed = missed, per_image = per_image}
end

-- Read-only colour-label lookup for a list of image ids (task 10 fix round
-- 0, controller ruling R12): `export.release`'s finals selection needs to
-- know which raws carry a colour label. darktable's Lua image object
-- exposes that as five boolean fields (red/yellow/green/blue/purple)
-- directly on the same object `image.lookup`/`image.rate` already index via
-- `dt.database[id]` -- no view switch, no import, no write. Honest misses:
-- an id `dt.database[id]` does not know about lands in `missing`, exactly
-- like `image.rate`'s `missed`.
local COLOR_LABELS = {"red", "yellow", "green", "blue", "purple"}
methods["image.color_labels"] = function(p)
  p = p or {}
  local ids = p.image_ids or {}
  if #ids == 0 then error("image.color_labels: image_ids required and non-empty") end
  local missing, per_image = {}, {}
  for _, id in ipairs(ids) do
    local numeric_id = tonumber(id)
    local image = dt.database[numeric_id]
    if not image then
      table.insert(missing, numeric_id)
    else
      local labels = {}
      for _, color in ipairs(COLOR_LABELS) do
        if image[color] then table.insert(labels, color) end
      end
      table.insert(per_image, {image_id = numeric_id, labels = labels})
    end
  end
  return {missing = missing, per_image = per_image}
end

-- Count images that landed anywhere under `source_path`.
--
-- Matching is by PREFIX, not equality. Each imported directory becomes its
-- own darktable film roll, so with the per-subdirectory camera layout
-- (<destination>/store_00010001_DCIM_100NCD80/...) a film's path is never
-- EQUAL to source_path. The old equality test would have counted zero for
-- every single camera import.
local function count_images_under(source_path)
  local prefix = source_path
  if prefix:sub(-1) == "/" then prefix = prefix:sub(1, -2) end
  local prefix_slash = prefix .. "/"
  local plen = #prefix_slash
  local n = 0
  for _, image in ipairs(dt.database) do
    local film = image.film
    local fp = film and film.path
    if type(fp) == "string" then
      if fp:sub(-1) == "/" then fp = fp:sub(1, -2) end
      if fp == prefix or fp:sub(1, plen) == prefix_slash then
        n = n + 1
      end
    end
  end
  return n
end

-- Wait for darktable's background scan to register the import.
--
-- Returns (count, scan_incomplete). `scan_incomplete` means the hard ceiling
-- expired while the answer was still unsettled, so `count` is a FLOOR rather
-- than a confirmed total.
--
-- WARNING: this BLOCKS the worker loop. dt.control.sleep yields to darktable,
-- not to our own scan_dir, so no other bridge request is served while it
-- runs. The bound is IMPORT_POLL_DEADLINE_SECONDS (10s) of wall clock, which
-- covers the library scans as well as the sleeps -- an attempt-count-only
-- bound does not, because each attempt is a full O(n) pass over dt.database.
-- The bridge is one-request-at-a-time by design (see the IPC bridge MVP
-- spec), so this is tolerated, but do not add more in-worker polling loops.
--
-- `deps.now` overrides the clock; the tests use it to prove the wall-clock
-- deadline fires without actually waiting 10 seconds.
local function poll_for_imported(source_path, deps)
  local now = (deps and deps.now) or os.time
  local started = now()
  local count, stable = 0, 0
  for attempt = 1, IMPORT_POLL_ATTEMPTS do
    local seen = count_images_under(source_path)
    if seen > count then
      -- Still arriving: reset the settle window rather than returning the
      -- first non-zero count, which would under-report a multi-directory
      -- card import that trickles in over several seconds.
      count, stable = seen, 0
    elseif count > 0 then
      stable = stable + 1
      if stable >= IMPORT_SETTLE_POLLS then
        return count, false     -- settled; this count is final
      end
    end
    -- Nothing has appeared at all after the grace window: this is the bad
    -- path / rejected-folder case, not a slow trickle. Return promptly with
    -- scan_incomplete rather than burning the rest of the budget on scans
    -- that have nothing to find.
    if count == 0 and attempt >= IMPORT_ZERO_GRACE_POLLS then
      return 0, true
    end
    -- Deadline check BEFORE the sleep, so a run whose scans alone have eaten
    -- the budget stops here instead of paying for one more of them.
    if now() - started >= IMPORT_POLL_DEADLINE_SECONDS then
      return count, true
    end
    if dt.control and dt.control.sleep then
      dt.control.sleep(IMPORT_POLL_INTERVAL_MS)
    else
      break                     -- no way to yield; do not spin the GUI thread
    end
  end
  -- Ceiling reached with the answer still moving (or nothing found at all).
  return count, true
end

methods.import_batch = function(p)
  p = p or {}
  local source_path = p.source_path
  if not source_path or source_path == "" then
    error("import_batch: source_path required")
  end
  local recursive = p.recursive
  if recursive == nil then recursive = true end

  -- RECURSION. dt.database.import(path) takes ONLY a path in darktable 5.x --
  -- there is no per-call recursion argument. darktable's own recursion is
  -- governed by a persistent user preference, which this plugin deliberately
  -- does NOT read or write:
  --   * darktable's Lua `dt.preferences` API is documented for SCRIPT-scoped
  --     preferences (register/read/write under a script namespace). Reaching
  --     a darktable core conf key such as recurse_directories through it is
  --     NOT documented, and could not be verified against a running
  --     darktable here -- the 2026-04-27 spike confirmed only that the
  --     `dt.preferences` table exists, not what it can address.
  --   * Even if it worked, temporarily flipping a persistent user preference
  --     from a background worker is its own bug: a crash or a concurrent GUI
  --     import would leave the user's setting changed behind their back.
  --
  -- So recursive = true is honoured HERE instead: enumerate the tree and
  -- import every directory in it. This is now mandatory rather than
  -- incidental, because import_from_camera writes one subdirectory per camera
  -- folder/card (camera filenames repeat across folders and across the two
  -- cards of a dual-slot body), so the photos never sit flat in the
  -- destination.
  local dirs, enumerated
  if recursive then
    dirs, enumerated = list_directories_recursive(source_path)
  else
    -- We can import just the top directory, but we cannot PREVENT darktable
    -- from recursing if the user's preference says to. So recursive = false
    -- is never something we can claim to have honoured.
    dirs, enumerated = {source_path}, false
  end

  -- The return value is heterogeneous: a dt_lua_image_t for a single file, a
  -- dt_lua_film_t (the registered film roll) for a directory. darktable scans
  -- the folder asynchronously, so the image count is not available yet.
  local table_total, saw_table, saw_other = 0, false, false
  for i = 1, #dirs do
    local r = dt.database.import(dirs[i])
    if type(r) == "table" then
      saw_table = true
      table_total = table_total + #r
    elseif r ~= nil then
      saw_other = true
    end
  end

  local count, scan_incomplete
  if saw_table and not saw_other then
    -- Stub-table return (unit tests): `#r` is the definitive count.
    count, scan_incomplete = table_total, false
  else
    count, scan_incomplete = poll_for_imported(source_path)
    -- NOTE: scan_incomplete is true both when nothing showed up at all and
    -- when images were still arriving as the ceiling expired. This replaces
    -- the old "final fallback: treat as 1", which reported imported = 1 for
    -- an import darktable never confirmed -- including a directory import
    -- that in fact found nothing.
  end

  local recursive_honoured = recursive and enumerated
  local note = nil
  if recursive and not enumerated then
    -- #dirs, not "only that path": the walk may have read part of the tree
    -- before hitting an unreadable subdirectory, and those directories WERE
    -- imported. Saying "only that path" would be a second false claim.
    note = "recursive=true requested, but " .. source_path .. " could not be "
        .. "fully enumerated -- " .. #dirs .. " director"
        .. (#dirs == 1 and "y was" or "ies were") .. " imported. Any "
        .. "subdirectory not listed was registered only if darktable's own "
        .. "recursive-import preference is enabled."
  elseif not recursive then
    note = "recursive=false could not be enforced: darktable's Lua API has no "
        .. "per-call recursion flag, and this plugin does not modify your "
        .. "recurse_directories preference. Only " .. source_path .. " was "
        .. "passed to darktable, but darktable may still have recursed into "
        .. "it if that preference is enabled."
  end

  return {
    imported = count,
    source_path = source_path,
    recursive = recursive,
    -- Whether the requested recursion mode is the one that actually happened.
    -- false => `recursive` above is what the CALLER asked for, not a
    -- description of what darktable did. Read `note`.
    recursive_honoured = recursive_honoured,
    note = note,
    -- How many directories were handed to dt.database.import.
    directories_imported = #dirs,
    -- true => the background scan had not settled before the poll ceiling.
    -- `imported` is a floor, not a confirmed total; the import may still be
    -- in progress.
    scan_incomplete = scan_incomplete,
  }
end

methods.list_styles = function(p)
  local out = {}
  for _, style in ipairs(dt.styles) do
    table.insert(out, {
      name = style.name,
      description = style.description,
    })
  end
  return {styles = out, count = #out}
end

methods.apply_preset = function(p)
  p = p or {}
  local preset_name = p.preset_name
  local photo_ids = p.photo_ids or {}
  if not preset_name or preset_name == "" then
    error("apply_preset: preset_name required")
  end
  if #photo_ids == 0 then
    error("apply_preset: photo_ids required and non-empty")
  end

  -- Linear scan to resolve preset_name (string lookup unavailable on dt.styles).
  local style = nil
  for _, s in ipairs(dt.styles) do
    if s.name == preset_name then style = s; break end
  end
  if not style then
    error(string.format("apply_preset: style %q not found (use list_styles)", preset_name))
  end

  local applied = 0
  local missed = {}
  for _, photo_id in ipairs(photo_ids) do
    local image = dt.database[tonumber(photo_id)]
    if image then
      image:apply_style(style)
      applied = applied + 1
    else
      table.insert(missed, tostring(photo_id))
    end
  end
  return {applied = applied, missed = missed, preset_name = preset_name}
end

-- ---- Darkroom verbs (Scouter phase 1) --------------------------------------
--
-- Every verb here is a thin, honest wrapper over dt.gui.action. Three rules,
-- all of them consequences of the phase 0 spike:
--   1. nan is the "not in the darkroom / module not in the pipe" signal, and
--      our JSON encoder turns nan into null -- so every result that carries an
--      action return value ALSO carries an explicit `nan` boolean. A null
--      status with nan=false means darktable returned nothing at all, which is
--      a different bug and must not be conflated.
--   2. Every verb reports the current view, so the Python layer can raise
--      wrong_view with an actionable message instead of interpreting a nan.
--   3. Nothing here writes library.db or an XMP directly. `darkroom.open` may
--      call dt.database.import to register an unknown path -- that IS the Lua
--      API for adding an image (w1ne's import_batch does the same thing), not
--      a direct library.db write. Everything else here goes through
--      dt.gui.action, dt.styles or image:apply_style, never the database file.

local function current_view_name()
  if not (dt.gui and dt.gui.current_view) then return "unknown" end
  local ok, view = pcall(dt.gui.current_view)
  if not ok or view == nil then return "unknown" end
  if type(view) == "string" then return view end
  return tostring(view.id or view.name or "unknown")
end

local function is_nan(v) return type(v) == "number" and v ~= v end

-- ---- View switching is ASYNCHRONOUS ----------------------------------------
--
-- Host-proven on this Mac (darktable 5.4.1, Lua API 9.6.0) by
-- spike/scouter_view_switch_probe.lua, run from a dt.control.dispatch
-- coroutine exactly like worker_loop:
--
--   * `dt.gui.current_view(v)` returns BEFORE the switch has happened.
--     Querying the view immediately afterwards returns the OLD view -- the
--     probe left the darkroom and `current_view()` still said "darkroom" at
--     0ms, then said "lighttable" after 150ms of yielding. Phase 0 saw the
--     identical thing. So reading the view straight after the call is not a
--     verification, it is a stale read, and acting on it is the bug.
--   * The switch DOES land, reliably, from the worker coroutine: 150ms out,
--     50ms back in the probe. "coroutine mutations get dropped" is false.
--   * `dt.gui.views.darkroom.display_image(image)` does NOT switch the view
--     when called from the lighttable: pcall reports ok=true and the view
--     stayed "lighttable" for the full 10s budget, twice. It only works from
--     INSIDE the darkroom, where it swaps the loaded image (which
--     `current_view(darkroom)` cannot do, being a no-op for the current
--     view). So the two calls are not interchangeable: each is correct on
--     exactly one side of the boundary.
--
-- Everything below follows from that: switch, then YIELD-POLL until the view
-- really is what we asked for, and never let display_image be the call that
-- is supposed to change the view.
local VIEW_SETTLE_POLL_MS = 50
local VIEW_SETTLE_BUDGET_MS = 5000
-- How long to stay in the lighttable mid-bounce. Leaving the darkroom is what
-- makes darktable flush the pipe's history to the sidecar; task 1's probe used
-- 500ms and the XMP advanced, so this dwell is deliberate, not a guess.
local VIEW_BOUNCE_DWELL_MS = 300

-- Yield-poll until the current view is `target`. Returns (landed, waited_ms).
--
-- dt.control.sleep is the yield: it hands the main loop back to darktable so
-- the switch it has queued can actually run. Without it this function would
-- spin on a value that can never change.
local function wait_for_view(target, budget_ms)
  budget_ms = budget_ms or VIEW_SETTLE_BUDGET_MS
  local waited = 0
  while true do
    if current_view_name() == target then return true, waited end
    if waited >= budget_ms then return false, waited end
    if not (dt.control and dt.control.sleep) then return false, waited end
    dt.control.sleep(VIEW_SETTLE_POLL_MS)
    waited = waited + VIEW_SETTLE_POLL_MS
  end
end

-- Both ids must be present, coercible numbers, AND equal. A naive
-- `tonumber(a) == tonumber(b)` is a trap: `tonumber(nil)` is nil, and Lua's
-- `nil == nil` is true, so "neither side has an id" would read as a match.
local function ids_match(a, b)
  local na, nb = tonumber(a), tonumber(b)
  return na ~= nil and nb ~= nil and na == nb
end

-- Run one dt.gui.action. Returns (value, nan_flag). Raises only when the API
-- itself is missing or the call blows up -- a nan is data, not an error.
local function gui_action(path, instance, element, effect, value)
  if not (dt.gui and dt.gui.action) then
    error("dt.gui.action is unavailable (darktable too old, or no GUI)")
  end
  local ok, res = pcall(dt.gui.action, path, instance or 0, element or "value",
                        effect or "", value or 0)
  if not ok then error("action " .. tostring(path) .. ": " .. tostring(res)) end
  if is_nan(res) then return nil, true end
  return res, false
end

methods["view.current"] = function()
  return {view = current_view_name()}
end

local function find_image_by_path(path)
  if not path or path == "" then return nil end
  for _, image in ipairs(dt.database) do
    if _image_full_path(image) == path then return image end
  end
  return nil
end

-- The film roll a library image belongs to, as a path, or nil. Indexing an
-- unknown field on a dt image userdata RAISES, so this is pcall-guarded.
local function image_film_path(image)
  local ok, film = pcall(function() return image.film end)
  if not ok or film == nil then return nil end
  local ok2, path = pcall(function() return film.path end)
  if ok2 and type(path) == "string" and path ~= "" then return path end
  return nil
end

-- Resolve an image by PATH or by ID, purely out of dt.database -- no GUI
-- touch whatsoever: no selection, no view switch, no import. This is the only
-- resolution a read verb (history.read, scout.*) or a dry run is allowed to
-- use; `darkroom.open` actively loads an image and belongs to write verbs
-- (final review, blocker 2).
--
-- An id is looked up with dt.database[id], which is documented to return the
-- image or nil -- it does not raise for an unknown id, but it is pcall-guarded
-- anyway so a future stricter build cannot turn a miss into a bridge error.
methods["image.lookup"] = function(p)
  p = p or {}
  local image = nil
  if p.image_id ~= nil then
    local ok, found = pcall(function() return dt.database[tonumber(p.image_id)] end)
    if ok then image = found end
  end
  if not image and p.path then image = find_image_by_path(p.path) end
  if not image then
    return {found = false, image_id = tonumber(p.image_id), path = p.path,
            filename = nil, film = nil, rating = nil}
  end
  return {
    found = true,
    image_id = tonumber(image.id),
    path = _image_full_path(image),
    filename = image.filename,
    film = image_film_path(image),
    rating = image.rating or 0,
  }
end

-- The image the darkroom currently has loaded, or nil, plus WHERE that
-- answer came from.
--
-- NEVER call `dt.gui.views.darkroom.display_image()` with no arguments here.
-- It is documented as the getter for the displayed image, and task 12 round 1
-- adopted it on that basis. It DEADLOCKS.
--
-- Host-proven, with a heartbeat file written by the worker loop on every tick
-- and around every step of this function (darktable 5.4.1, this Mac): the
-- very first `darkroom.current` after `view.current` reports "darkroom"
-- freezes with the heartbeat reading "display_image getter -- about to call"
-- and never advances again -- not in 4s, not in 72s, not after the render has
-- long finished. `pcall` does not help: this is a block, not an error. The
-- worker coroutine runs on darktable's own main thread, so blocking in it
-- blocks the thread that would have to unblock it, and the whole bridge is
-- dead for the rest of the session. Every later request then times out, which
-- is exactly the "no response at all in that window" that task 12 recorded
-- as Finding 2 and mistook for the pixel pipeline being busy. It was not
-- busy: the same window answers a real `dt.gui.action` on the first attempt
-- (spike/scouter_view_switch_probe.lua).
--
-- So the reading here is `dt.gui.action_images`, the selection-based reading
-- this plugin used before task 12 and which never blocked. It is less precise
-- (it reports the SELECTION, which can in principle differ from what the
-- darkroom shows), and callers are told so by the `source` return value --
-- an honest, slightly weaker answer beats a deadlocked bridge.
--
-- The display_image SETTER (`display_image(image)`, one argument) is a
-- different call and is safe: host-verified to return promptly and to swap
-- the loaded image when called from inside the darkroom. switch_to_darkroom
-- still uses it for exactly that.
local function loaded_darkroom_image()
  if not (dt.gui and dt.gui.action_images) then return nil, "action_images" end
  local ok, images = pcall(function() return dt.gui.action_images end)
  if not ok or type(images) ~= "table" then return nil, "action_images" end
  return images[1], "action_images"
end

-- Backward-compatible alias: pre-round-1 code and tests called this
-- darkroom_image() and only wanted the image, not its source.
local function darkroom_image()
  local image = loaded_darkroom_image()
  return image
end

-- A read-only snapshot of "what is loaded right now", with NO side effects --
-- never selects, never switches views, never imports. This is the honest way
-- for a caller to learn which image (if any) the darkroom has loaded: unlike
-- `darkroom.open`, which actively loads a requested image and errors when
-- nothing was given to load, `darkroom.current` just reports. `image_id` and
-- `path` are absent (not merely null) when nothing is loaded -- see
-- encode_value's `nil` handling.
methods["darkroom.current"] = function()
  local image, source = loaded_darkroom_image()
  if not image then
    return {view = current_view_name(), source = source}
  end
  return {
    view = current_view_name(),
    image_id = tonumber(image.id),
    path = _image_full_path(image),
    source = source,
  }
end

-- Actually switch the darkroom onto `image`. Returns (ok, method_or_error).
--
-- The two mechanisms are NOT interchangeable -- see the host findings above
-- wait_for_view. Each is used on the only side of the boundary where it
-- works:
--
--   * Already in the darkroom: `display_image(image)` swaps the loaded image.
--     `dt.gui.current_view(darkroom)` is a no-op for the view you are already
--     in, so it cannot do this (that was fix round 1's bug).
--   * Outside the darkroom: `dt.gui.current_view(darkroom)` is the switch
--     that lands. `display_image` from the lighttable is a silent no-op --
--     pcall ok=true, view unchanged for 10s -- which is what made fix round
--     1's "prefer display_image everywhere" turn a working bounce into a
--     reliable failure.
--
-- Whichever path runs, the view is yield-polled afterwards rather than read
-- immediately, because the switch is asynchronous.
local function switch_to_darkroom(image)
  local view = dt.gui and dt.gui.views and dt.gui.views.darkroom
  if not view then return false, "dt.gui.views.darkroom missing" end

  -- Select FIRST, unconditionally. loaded_darkroom_image()'s action_images
  -- fallback must never read a stale selection if display_image's own getter
  -- turns out to be unreliable (its getter answering nil while its setter
  -- works is a real observed quirk).
  if dt.gui.selection and image then pcall(dt.gui.selection, {image}) end

  if current_view_name() == "darkroom" then
    if image and type(view.display_image) == "function" then
      local ok = pcall(view.display_image, image)
      if ok then return true, "display_image" end
    end
    -- No usable display_image: bounce out to the lighttable so the switch
    -- back is a real transition rather than another no-op.
    if not (dt.gui.views and dt.gui.views.lighttable) then
      return false, "dt.gui.views.lighttable missing"
    end
    if not pcall(dt.gui.current_view, dt.gui.views.lighttable) then
      return false, "bounce to lighttable failed"
    end
    local left = wait_for_view("lighttable")
    if not left then
      return false, "still in the darkroom after the bounce out to the lighttable"
    end
  end

  local ok, err = pcall(dt.gui.current_view, view)
  if not ok then return false, "view switch failed: " .. tostring(err) end
  local landed, waited = wait_for_view("darkroom")
  if not landed then
    return false, string.format("view is still %s %dms after switching to darkroom",
                                current_view_name(), waited)
  end

  -- In the darkroom now. If it came up on some other image, display_image is
  -- the call that can swap it -- and this is the side it works from.
  if image and type(view.display_image) == "function" then
    local loaded = loaded_darkroom_image()
    if not (loaded and ids_match(loaded.id, image.id)) then
      pcall(view.display_image, image)
    end
  end
  return true, "view_switch"
end

-- Return to the darkroom, with or without a specific image to load. Used by
-- the commit bounce, whose return leg may have nothing to re-load (no image
-- was resolvable) but must still get the view back.
local function return_to_darkroom(image)
  if image then return switch_to_darkroom(image) end
  local ok, err = pcall(dt.gui.current_view, dt.gui.views.darkroom)
  if not ok then return false, "view switch failed: " .. tostring(err) end
  local landed, waited = wait_for_view("darkroom")
  if not landed then
    return false, string.format("view is still %s %dms after switching to darkroom",
                                current_view_name(), waited)
  end
  return true, "view_switch"
end

methods["darkroom.open"] = function(p)
  p = p or {}
  local image = nil
  local imported = false
  if p.image_id then image = dt.database[tonumber(p.image_id)] end
  if not image and p.path then
    image = find_image_by_path(p.path)
    if not image then
      -- Not in the library yet: register it, then look it up again. import()
      -- returns an image for a file and a film for a directory, so do not
      -- trust its return type -- re-scan instead.
      pcall(dt.database.import, p.path)
      image = find_image_by_path(p.path)
      if image then imported = true end
    end
  end
  if not image then
    error("darkroom.open: no image for " .. tostring(p.image_id or p.path))
  end

  local loaded, source = loaded_darkroom_image()
  local already = current_view_name() == "darkroom"
      and loaded ~= nil and ids_match(loaded.id, image.id)

  if not already then
    local switched, detail = switch_to_darkroom(image)
    if not switched then
      error("darkroom.open: " .. tostring(detail))
    end
  end

  -- VERIFY: a "successful" switch is not enough on its own -- the no-op bug
  -- this fixes would otherwise still report opened=true for an image that
  -- never actually loaded. Trust only what is now actually displayed. This
  -- is a genuine failure of the request, not a normal result shape, so it is
  -- a JSON-RPC error like the method's other failures -- not a
  -- {opened=false, error=...} result a careless caller could ignore.
  --
  -- Task 12's host run found a SECOND way the switch can lie: `image` can
  -- match while the VIEW never actually left the lighttable (loaded_
  -- darkroom_image's action_images fallback reads the current *selection*,
  -- which display_image/dt.gui.selection can set without the view itself
  -- switching). Checking only the image, as before, let `opened=true` through
  -- for a caller that was never actually in the darkroom -- so the view is
  -- checked too now, not just the image.
  -- switch_to_darkroom has already yield-polled the view, but wait once more
  -- (a no-op when we are already there) so the `already` fast path is held to
  -- the same standard as a real switch.
  wait_for_view("darkroom")
  local final_image, final_source = loaded_darkroom_image()
  local matches = final_image ~= nil and ids_match(final_image.id, image.id)
  local final_view = current_view_name()
  if not matches then
    error("wrong image loaded: " .. tostring(image.id))
  end
  if final_view ~= "darkroom" then
    error("wrong image loaded: " .. tostring(image.id) .. " (view is "
          .. tostring(final_view) .. ", not darkroom)")
  end

  return {
    image_id = tonumber(image.id),
    path = _image_full_path(image),
    view = final_view,
    opened = not already,
    already_open = already,
    imported = imported,
    source = already and source or final_source,
  }
end

methods["darkroom.action"] = function(p)
  p = p or {}
  if type(p.path) ~= "string" or p.path == "" then
    error("darkroom.action: path required (e.g. iop/exposure/exposure)")
  end
  local effect = p.effect or "set"
  if effect == "set" and type(p.value) ~= "number" then
    -- HONESTY: value is the absolute value dt.gui.action sets for effect=
    -- "set" (phase 0 finding 1). Defaulting a missing/mistyped value to 0
    -- would silently zero a slider instead of reporting the caller's mistake.
    error('darkroom.action: value is required and must be a number when effect="set"')
  end
  local value, nan = gui_action(p.path, p.instance, p.element or "value",
                               effect, p.value or 0)
  return {
    path = p.path,
    instance = p.instance or 0,
    element = p.element or "value",
    effect = effect,
    value = p.value,
    status = value,
    nan = nan,
    view = current_view_name(),
  }
end

methods["darkroom.status"] = function(p)
  p = p or {}
  if type(p.path) ~= "string" or p.path == "" then
    error("darkroom.status: path required")
  end
  -- Speed 0 with an empty effect is the documented status query; it returns the
  -- NORMALISED position in the slider's visible range, never the value.
  local pos, nan = gui_action(p.path, p.instance, p.element or "value", "", 0)
  return {path = p.path, instance = p.instance or 0, position = pos, nan = nan,
          view = current_view_name()}
end

local MODULE_ACTIONS = {
  enable  = {element = "enable", effect = "on"},
  disable = {element = "enable", effect = "off"},
  reset   = {element = "reset",  effect = "activate"},
}

local function module_verb(action_name)
  return function(p)
    p = p or {}
    if type(p.module) ~= "string" or p.module == "" then
      error("module." .. action_name .. ": module required (e.g. exposure)")
    end
    local spec = MODULE_ACTIONS[action_name]
    local value, nan = gui_action("iop/" .. p.module, p.instance,
                                  spec.element, spec.effect, 1)
    return {module = p.module, action = action_name, instance = p.instance or 0,
            status = value, nan = nan, view = current_view_name()}
  end
end

methods["module.enable"] = module_verb("enable")
methods["module.disable"] = module_verb("disable")
methods["module.reset"] = module_verb("reset")

-- Commit trigger.
--
-- Task 1's measurement (docs/measurements.md) REJECTED every sidecar-write
-- action spelling below -- C1 and all three C2 variants returned nan on this
-- darktable build, the same "not a real widget path" signal phase 0 saw for
-- absolute sets outside the darkroom. So strategy="auto" goes straight to
-- C3, the view bounce, which is the only strategy phase 0/1 proved commits.
-- The ladder below stays reachable only via an EXPLICIT
-- strategy="sidecar_action" request, for a future darktable build where one
-- of these spellings might actually be wired up.
local SIDECAR_ACTION_PATHS = {
  "lib/selected image[s]/write sidecar files",
  "lib/metadata/write sidecar files",
  "lib/image/write sidecar files",
}

local function commit_sidecar_action()
  for _, path in ipairs(SIDECAR_ACTION_PATHS) do
    local ok, value, nan = pcall(gui_action, path, 0, "button", "activate", 1)
    if ok and not nan then return true, path end
  end
  return false, nil
end

-- Returns {ok, error, view}. `view` is always the ACTUAL current view when
-- the function returns, whether or not the bounce succeeded -- callers must
-- never report a view the session did not really end up in.
local function commit_view_bounce()
  local starting_view = current_view_name()
  if starting_view ~= "darkroom" then
    -- A bounce that starts outside the darkroom cannot commit anything: the
    -- lighttable-then-darkroom dance only re-triggers a pipe write if there
    -- was a live darkroom pipe to write from.
    return {ok = false, error = "not in darkroom", view = starting_view}
  end
  if not (dt.gui.views and dt.gui.views.lighttable and dt.gui.views.darkroom) then
    return {ok = false, error = "dt.gui.views.lighttable/darkroom missing", view = starting_view}
  end

  -- Remember which image was loaded BEFORE leaving the darkroom, so the
  -- return leg can put the same one back.
  local image = darkroom_image()

  -- Leg 1. The switch is asynchronous, so this is NOT verified by reading the
  -- view straight afterwards (that returns the stale "darkroom"): yield until
  -- the lighttable really is current. Leaving the darkroom is also what makes
  -- darktable flush the pipe to the sidecar, so this wait is the commit.
  local ok1 = pcall(dt.gui.current_view, dt.gui.views.lighttable)
  local left = ok1 and wait_for_view("lighttable")
  if not left then
    local stuck = current_view_name()
    return {ok = false,
            error = "could not leave the darkroom (view is " .. stuck .. ")",
            view = stuck}
  end
  -- Dwell in the lighttable so the sidecar write finishes before the pipe is
  -- reloaded underneath it.
  if dt.control and dt.control.sleep then dt.control.sleep(VIEW_BOUNCE_DWELL_MS) end

  -- Leg 2. return_to_darkroom yield-polls the view itself, so ok2 is a real
  -- answer about where the session ended up, not just "the call did not raise".
  local ok2, detail2 = return_to_darkroom(image)
  if not ok2 then
    -- One retry: a single flaky switch back should not strand the session in
    -- the lighttable when the commit itself already landed (the pipe unloaded
    -- on the way out). Same dwell before trying again.
    if dt.control and dt.control.sleep then dt.control.sleep(VIEW_BOUNCE_DWELL_MS) end
    ok2, detail2 = return_to_darkroom(image)
  end
  local final_view = current_view_name()
  if not ok2 or final_view ~= "darkroom" then
    return {ok = false, error = detail2 or "view switch failed", view = final_view}
  end
  return {ok = true, error = nil, view = final_view}
end

-- ---- Confirming a commit ---------------------------------------------------
--
-- Host-proven (task 12 fix round 1, darktable 5.4.1, this Mac):
--
--   * The lighttable<->darkroom bounce DOES make darktable write the sidecar,
--     but only when the history has actually CHANGED. Setting a slider to the
--     value it already holds adds no history item, so nothing is written --
--     correctly. A commit that writes nothing is therefore not automatically a
--     failure, and callers need to be able to tell the two apart.
--   * There is no Lua API and no action path that writes a sidecar directly:
--     every "write sidecar files" spelling returns nan from the lighttable
--     (where that panel actually lives) as well as from the darkroom, there is
--     no dt.database.write_*, and dt.gui.libs.image exposes no method. The
--     full inventories are in spike/scouter_sidecar_probe2.lua's output.
--   * `dt.preferences.write("darktable", "write_sidecar_files", ...)` does NOT
--     reach darktable's core preference: it creates a SCRIPT-namespaced key
--     (`lua/darktable/write_sidecar_files` appears in darktablerc) and leaves
--     the core setting alone. An earlier round of this fix tried exactly that,
--     believing the core preference gated the commit; it was inert, and the
--     only thing it achieved was littering the user's darktablerc. The commits
--     it "fixed" were in fact fixed by the view-switch repair above. Do not
--     reintroduce it -- and note more generally that this plugin does not
--     rewrite persistent user preferences (see import_batch's note).
--
-- So the commit reports what actually happened on disk, and the Python layer
-- (scouter/core/darkroom.py) resolves "nothing was written" by reading the
-- value back: if the sidecar already carries what was asked for, the request
-- is satisfied.
local SIDECAR_WRITE_POLL_MS = 100
-- Host-measured: the sidecar appears within ~1.2s of the bounce when there is
-- anything to write. 1500ms per attempt, twice, keeps the worst case (a commit
-- with nothing to write) inside the Python layer's own 5s commit budget.
local SIDECAR_WRITE_BUDGET_MS = 1500
-- How many bounces one history.commit may spend. The FIRST commit of a
-- session can land while darktable is still finishing its initial full-size
-- render, and that bounce flushes nothing; the next one does. Bounded, and
-- gated on a real confirmation (the sidecar changed), never on a fixed sleep.
local COMMIT_BOUNCE_ATTEMPTS = 2

-- The sidecar path darktable itself reports for this image, or nil.
-- Indexing an unknown field on a dt image userdata RAISES, so this is
-- pcall-guarded rather than compared against nil.
local function image_sidecar_path(image)
  if not image then return nil end
  local ok, value = pcall(function() return image.sidecar end)
  if ok and type(value) == "string" and value ~= "" then return value end
  return nil
end

-- stat(1) is not portable: BSD/macOS spells the format `-f '%Fm %z'`, GNU
-- coreutils (Linux) spells it `-c '%Y %s'` and rejects -f outright. Detected
-- once, from `uname -s`, rather than guessing per call.
local function stat_format_args()
  local p = io.popen("uname -s 2>/dev/null")
  local uname = ""
  if p then
    uname = (p:read("*a") or ""):gsub("%s+$", "")
    p:close()
  end
  if uname == "Linux" then return "-c '%Y %s'" end
  return "-f '%Fm %z'"
end

local STAT_ARGS = stat_format_args()

-- "<mtime> <size>" for `path`, or nil when it does not exist. Lua has no
-- stat(2), and whole-second os.time granularity cannot tell two writes in the
-- same second apart -- which is exactly the resolution a commit needs. stat(1)
-- can (fractional on macOS, whole seconds plus the size on Linux, which is why
-- the size is part of the signature).
local function sidecar_signature(path)
  if type(path) ~= "string" or path == "" then return nil end
  local p = io.popen("stat " .. STAT_ARGS .. " " .. shell_quote(path) .. " 2>/dev/null")
  if not p then return nil end
  local out = p:read("*a") or ""
  p:close()
  out = out:gsub("^%s+", ""):gsub("%s+$", "")
  if out == "" then return nil end
  return out
end

-- Bytes at `path`, or 0 when it does not exist. Reuses `sidecar_signature`'s
-- "<mtime> <size>" form (built from `stat_format_args()`'s STAT_ARGS) rather
-- than inventing a second, separately-portable `stat` invocation: the size
-- is always the trailing whitespace-separated token in that signature.
local function file_size(path)
  local sig = sidecar_signature(path)
  if not sig then return 0 end
  local size = sig:match("(%d+)%s*$")
  return tonumber(size) or 0
end

-- Yield-poll until `path`'s signature differs from `before` (nil `before`
-- means "did not exist"). Returns (changed, waited_ms).
local function wait_for_sidecar(path, before, budget_ms)
  budget_ms = budget_ms or SIDECAR_WRITE_BUDGET_MS
  local waited = 0
  while true do
    if sidecar_signature(path) ~= before then return true, waited end
    if waited >= budget_ms then return false, waited end
    if not (dt.control and dt.control.sleep) then return false, waited end
    dt.control.sleep(SIDECAR_WRITE_POLL_MS)
    waited = waited + SIDECAR_WRITE_POLL_MS
  end
end

-- The only strategies history.commit understands. Anything else is a
-- caller mistake, not a silent fallback to "auto".
local HISTORY_COMMIT_STRATEGIES = {auto = true, view_bounce = true, sidecar_action = true}

methods["history.commit"] = function(p)
  p = p or {}
  local strategy = p.strategy or "auto"
  if not HISTORY_COMMIT_STRATEGIES[strategy] then
    error("history.commit: unknown strategy " .. tostring(strategy)
      .. ' (valid: "auto", "view_bounce", "sidecar_action")')
  end
  -- cpu_ms: CPU time (os.clock()), not wall-clock -- matches
  -- docs/measurements.md, which records both and notes CPU time understates
  -- the real cost. Named cpu_ms (not ms) so callers cannot mistake it for a
  -- wall-clock duration.
  local started = os.clock()
  if strategy == "sidecar_action" then
    local ok, path = commit_sidecar_action()
    if ok then
      return {strategy_used = "sidecar_action", ok = true, detail = path,
              view_after = current_view_name(), cpu_ms = (os.clock() - started) * 1000}
    end
    return {strategy_used = "sidecar_action", ok = false,
            detail = "every sidecar-write action returned nan on this build",
            view_after = current_view_name(), cpu_ms = (os.clock() - started) * 1000}
  end
  -- strategy == "auto" or "view_bounce".
  --
  -- The bounce alone is NOT a commit: it flushes the darkroom pipe, and
  -- darktable turns that flush into a sidecar write only when the history
  -- ACTUALLY CHANGED (and only while the user's own "store XMP sidecar files"
  -- preference allows it -- a preference this plugin never reads and never
  -- writes; the earlier claim that it could be set from Lua was wrong, see the
  -- comment block above). So the write is confirmed below by watching the
  -- sidecar itself, never assumed from a successful bounce.
  local image = darkroom_image()
  local sidecar = image_sidecar_path(image)
  local before = sidecar and sidecar_signature(sidecar) or nil

  --
  -- Up to COMMIT_BOUNCE_ATTEMPTS bounces: the FIRST commit of a session can
  -- land while darktable is still finishing its initial full-size render, and
  -- that bounce flushes nothing. A second bounce right after does. This is a
  -- bounded retry over a real confirmation (did the sidecar change?), not a
  -- longer blind wait.
  local ok_call, result = true, nil
  local written, write_ms = nil, 0
  for attempt = 1, COMMIT_BOUNCE_ATTEMPTS do
    ok_call, result = pcall(commit_view_bounce)
    if not ok_call then break end
    if not (result.ok and sidecar) then break end
    local changed, waited = wait_for_sidecar(sidecar, before)
    written = changed
    write_ms = write_ms + waited
    if changed then break end
    if attempt < COMMIT_BOUNCE_ATTEMPTS and dt.control and dt.control.sleep then
      dt.control.sleep(VIEW_BOUNCE_DWELL_MS)
    end
  end
  if not ok_call then error(result) end

  local detail = result.error
  if detail == nil and written == false then
    detail = "the view bounce completed " .. COMMIT_BOUNCE_ATTEMPTS
      .. " time(s) but " .. tostring(sidecar) .. " did not change within "
      .. write_ms .. "ms of waiting -- darktable writes a sidecar only when "
      .. "the history actually changes, so this is expected when the requested "
      .. "value is the one the image already had"
  end

  return {
    strategy_used = "view_bounce",
    -- ok means "this image's history is now on disk", not "the views moved".
    ok = result.ok and written ~= false,
    detail = detail,
    view_after = result.view,
    cpu_ms = (os.clock() - started) * 1000,
    sidecar = sidecar,
    -- nil when there was no sidecar path to watch (no image loaded), so a
    -- caller can tell "not checked" from "checked and did not change".
    sidecar_written = written,
    sidecar_wait_ms = write_ms,
  }
end

methods["snapshot.take"] = function()
  -- Preferred: the documented lib API. Fallback: the panel's button action.
  if dt.gui and dt.gui.libs and dt.gui.libs.snapshots
     and dt.gui.libs.snapshots.take_snapshot then
    local ok, err = pcall(dt.gui.libs.snapshots.take_snapshot)
    if ok then return {taken = true, via = "libs.snapshots", view = current_view_name()} end
    local _, nan = gui_action("lib/snapshots/take snapshot", 0, "button", "activate", 1)
    return {taken = not nan, via = "action", detail = tostring(err),
            view = current_view_name()}
  end
  local _, nan = gui_action("lib/snapshots/take snapshot", 0, "button", "activate", 1)
  return {taken = not nan, via = "action", view = current_view_name()}
end

methods["style.create_from_current"] = function(p)
  p = p or {}
  if type(p.name) ~= "string" or p.name == "" then
    error("style.create_from_current: name required")
  end
  local image, source = loaded_darkroom_image()
  if not image then error("style.create_from_current: no image loaded in the darkroom") end

  local existed = false
  for _, s in ipairs(dt.styles) do
    if s.name == p.name then existed = true; break end
  end

  local ok, err = pcall(dt.styles.create, image, p.name, p.description or "")
  if not ok then error("style.create_from_current: " .. tostring(err)) end

  -- HONESTY: dt.styles.create takes the WHOLE history. Lua API 9.6.0 has no
  -- module filter, so `modules` cannot be honoured here. Say so rather than
  -- letting the caller believe the style is narrow. Filtering by writing a
  -- .dtstyle from the XMP and importing it is phase 2.
  local requested = 0
  for _ in ipairs(p.modules or {}) do requested = requested + 1 end
  return {
    name = p.name, created = true, existing_replaced = existed,
    modules_requested = requested, modules_honoured = false,
    source = source,
    note = requested > 0 and ("dt.styles.create has no module filter in Lua API "
      .. "9.6.0; the style carries the image's whole history") or nil,
  }
end

methods["style.apply"] = function(p)
  p = p or {}
  local ids = p.image_ids or {}
  if type(p.name) ~= "string" or p.name == "" then error("style.apply: name required") end
  if #ids == 0 then error("style.apply: image_ids required and non-empty") end
  local mode = p.mode or "append"
  if mode ~= "append" then
    -- HONESTY: image:apply_style always appends. There is no overwrite in Lua
    -- API 9.6.0; darktable-cli --style-overwrite covers renders only.
    error("style.apply: mode " .. tostring(mode) .. " is not available in Lua API "
          .. "9.6.0 (apply_style always appends); use append, or export with "
          .. "darktable-cli --style-overwrite")
  end

  local style = nil
  for _, s in ipairs(dt.styles) do
    if s.name == p.name then style = s; break end
  end
  if not style then error(string.format("style.apply: style %q not found", p.name)) end

  local applied, missed, errors = 0, {}, {}
  for _, id in ipairs(ids) do
    local image = dt.database[tonumber(id)]
    if not image then
      table.insert(missed, tonumber(id))
    else
      local ok, err = pcall(function() image:apply_style(style) end)
      if ok then
        applied = applied + 1
      else
        table.insert(errors, {image_id = tonumber(id), error = tostring(err)})
      end
    end
  end
  return {name = p.name, mode = mode, applied = applied, missed = missed, errors = errors}
end

-- Fix round 2, item 6: `scouter/core/dtstyle.py`'s writer escapes `&`/`<`/`>`
-- in `<name>` the same way ElementTree escapes `<description>`, so the raw
-- regex-extracted text can carry `&amp;`/`&lt;`/`&gt;`/`&quot;`/`&apos;`.
-- Comparing the ESCAPED form against `s.name` (which darktable stores
-- unescaped) would silently never match a name containing any of those
-- characters -- unescape before comparing. `&amp;` last, so `&amp;lt;`
-- unescapes to `&lt;`, not all the way to `<` (matches every XML unescaper's
-- ordering, including Python's xml.sax.saxutils.unescape defaults).
local function xml_unescape(s)
  if not s then return s end
  s = s:gsub("&lt;", "<")
  s = s:gsub("&gt;", ">")
  s = s:gsub("&quot;", "\"")
  s = s:gsub("&apos;", "'")
  s = s:gsub("&amp;", "&")
  return s
end

-- The <name> a .dtstyle file will actually import as, read straight off the
-- file's own <info><name>...</name> (fix round 1, should-fix 9): a caller's
-- `name` parameter is only ever a HINT (dt.styles.import derives the real
-- name from the file, not from any parameter), so the collision check below
-- must compare against what the file itself says, not against the hint --
-- otherwise a mismatched hint could make the check pass or fail against the
-- wrong style entirely. A plain string match, not real XML parsing: this is
-- the same tradeoff `sidecar_signature`/`file_size` already make for a
-- narrow, well-known field.
local function dtstyle_file_name(path)
  local f = io.open(path, "r")
  if not f then return nil end
  local content = f:read("*a") or ""
  f:close()
  return xml_unescape(content:match("<info>.-<name>(.-)</name>"))
end

-- Fix round 2, nit: sha256 of the first 4KB, appended to `sidecar_signature`'s
-- mtime+size -- a filesystem whose mtime resolution is coarser than the
-- write (already `sidecar_signature`'s own reasoning) could ALSO, in theory,
-- leave the size unchanged for a same-length rewrite; hashing real content
-- closes that gap for the specific "did this file's content actually
-- change" question `style.export_file` answers. `shasum` is not guaranteed
-- present on every host, so a missing/failed hash degrades to "" (still
-- combined with the always-available mtime+size signature) rather than
-- erroring -- this is a strengthened confidence check, not the only signal.
local function export_signature(path)
  local sig = sidecar_signature(path)
  local hash = nil
  local p = io.popen("head -c 4096 " .. shell_quote(path)
    .. " 2>/dev/null | shasum -a 256 2>/dev/null | awk '{print $1}'")
  if p then
    local out = (p:read("*a") or ""):gsub("%s+$", "")
    p:close()
    if out ~= "" then hash = out end
  end
  if sig == nil and hash == nil then return nil end
  return (sig or "") .. "|" .. (hash or "")
end

-- Export an existing style to a .dtstyle file. Its first job is to PIN the file
-- format: a style made by style.create_from_current is exported here and the
-- real file is committed as tests/fixtures/hero.dtstyle, so the writer in
-- scouter/core/dtstyle.py is asserted against darktable's own output rather
-- than against an assumption about another project's XML.
methods["style.export_file"] = function(p)
  p = p or {}
  if type(p.name) ~= "string" or p.name == "" then error("style.export_file: name required") end
  if type(p.dir) ~= "string" or p.dir == "" then error("style.export_file: dir required") end
  local style = nil
  for _, s in ipairs(dt.styles) do if s.name == p.name then style = s; break end end
  if not style then error(string.format("style.export_file: style %q not found", p.name)) end
  local path = p.dir .. "/" .. p.name .. ".dtstyle"
  -- fix round 1, should-fix 9: `exported` is only true when the file's own
  -- signature actually advanced -- a `pcall` returning ok=true proves
  -- dt.styles.export did not RAISE, not that it wrote anything.
  local before_sig = export_signature(path)
  local ok, err = pcall(dt.styles.export, style, p.dir)
  if not ok then error("style.export_file: " .. tostring(err)) end
  local after_sig = export_signature(path)
  local advanced = after_sig ~= nil and after_sig ~= before_sig
  return {name = p.name, path = path, exported = advanced, bytes = file_size(path)}
end

methods["style.import_file"] = function(p)
  p = p or {}
  if type(p.path) ~= "string" or p.path == "" then error("style.import_file: path required") end
  -- fix round 1, should-fix 9: the collision check is against the name the
  -- FILE will actually import as, not the caller's `name` hint.
  local file_name = dtstyle_file_name(p.path)
  local name = file_name or p.name
  local before, existing = 0, nil
  for _, s in ipairs(dt.styles) do
    before = before + 1
    if name and s.name == name then existing = s end
  end
  if existing and not p.overwrite then
    error(string.format("style.import_file: a style named %q already exists; pass "
          .. "overwrite=true to replace it", name))
  end
  local replaced = false
  if existing and p.overwrite then
    local ok, err = pcall(dt.styles.delete, existing)
    if not ok then error("style.import_file: could not delete the existing style: "
                         .. tostring(err)) end
    replaced = true
  end
  -- fix round 1, should-fix 9: the destructive window is REAL and unavoidable
  -- with this Lua API (there is no atomic "replace" primitive) -- delete has
  -- already happened above when `replaced` is true, so an import failure
  -- from here on cannot be rolled back. Named explicitly in the error rather
  -- than silently leaving the caller to discover the style is just gone.
  local ok, err = pcall(dt.styles.import, p.path)
  if not ok then
    if replaced then
      error(string.format(
        "style.import_file: import of %q failed AFTER the existing style of "
        .. "the same name was already deleted (destructive window: it cannot "
        .. "be restored automatically) -- %s", name, tostring(err)))
    end
    error("style.import_file: " .. tostring(err))
  end
  local after = 0
  for _ in ipairs(dt.styles) do after = after + 1 end
  return {name = name, imported = true, replaced = replaced,
          styles_before = before, styles_after = after}
end

-- Delete one style by name. Tolerant of "already gone" (existed=false)
-- rather than erroring, since its main caller is test/probe cleanup
-- (fix round 1, should-fix 10) where "nothing to clean up" is a normal,
-- not exceptional, outcome.
--
-- Fix round 2, item 5: this deletes real user data with no undo, so it is
-- gated TWICE, both before the `dt.styles` scan even runs: `prefix`
-- (defaults to Scouter's own "scouter-", so a typo'd or malicious `name`
-- cannot reach an arbitrary style someone actually uses) and `confirm=true`
-- (no default that makes a bare `{name=...}` call ever delete anything).
methods["style.delete"] = function(p)
  p = p or {}
  if type(p.name) ~= "string" or p.name == "" then error("style.delete: name required") end
  -- Fix round 3, item 4: an OMITTED prefix (nil) defaults to "scouter-", but
  -- an EXPLICIT empty prefix is refused rather than silently rewritten to
  -- the same default -- prefix="" would match every style name (an empty
  -- string is a prefix of everything in Lua's sub(1, 0) == ""), which is
  -- exactly the safety check this argument exists to prevent bypassing.
  local prefix = p.prefix
  if prefix == nil then
    prefix = "scouter-"
  elseif type(prefix) ~= "string" or prefix == "" then
    error("style.delete: prefix must be a non-empty string (omit it to use "
      .. "the default \"scouter-\"; an explicit empty prefix would match "
      .. "every style name, defeating the safety check)")
  end
  if p.name:sub(1, #prefix) ~= prefix then
    error(string.format(
      "style.delete: refusing to delete %q -- it does not start with the "
      .. "required prefix %q (pass a different `prefix` if this is "
      .. "intentional; the default only lets Scouter clean up its own "
      .. "throwaway styles)", p.name, prefix))
  end
  if not p.confirm then
    error(string.format(
      "style.delete: pass confirm=true to actually delete %q -- this call "
      .. "never deletes without an explicit confirmation", p.name))
  end
  local style = nil
  for _, s in ipairs(dt.styles) do if s.name == p.name then style = s; break end end
  if not style then return {name = p.name, deleted = false, existed = false} end
  local ok, err = pcall(dt.styles.delete, style)
  if not ok then error("style.delete: " .. tostring(err)) end
  return {name = p.name, deleted = true, existed = true}
end

-- ---- Dispatch --------------------------------------------------------------

local function handle(req)
  -- A decodable request with a usable id but a junk `method` still deserves a
  -- real error response, not silence: silence costs the client its whole
  -- timeout and then reports "darktable not running", which is a lie.
  if type(req.method) ~= "string" then
    return {
      id = req.id,
      error = "malformed request: 'method' must be a string, got "
              .. type(req.method),
    }
  end
  local fn = methods[req.method]
  if not fn then
    return {id = req.id, error = "unknown method: " .. tostring(req.method)}
  end
  local ok, result_or_err = pcall(fn, req.params)
  if not ok then
    return {id = req.id, error = "handler raised: " .. tostring(result_or_err)}
  end
  return {id = req.id, result = result_or_err}
end

-- ---- File I/O --------------------------------------------------------------

-- shell_quote lives up top, next to the other filesystem helpers.

local function cache_dir()
  local base = os.getenv("XDG_CACHE_HOME")
  if not base or base == "" then
    local home = os.getenv("HOME")
    if not home or home == "" then home = "." end
    base = home .. "/.cache"
  end
  return base .. "/darktable-mcp"
end

-- In-process listing via LuaFileSystem. Preferred when available: no fork, no
-- shell, no quoting hazard, and no ~36k subprocesses per hour of idle session.
-- `lfs_mod` is injectable so the tests can exercise this path on a host where
-- lfs is not installed.
local function list_request_files_lfs(dir, lfs_mod)
  lfs_mod = lfs_mod or lfs
  local out = {}
  local ok = pcall(function()
    for entry in lfs_mod.dir(dir) do
      -- Readers must ignore *.tmp; the `.json$` anchor already does that,
      -- since an in-flight write is named request-<id>.json.tmp.
      if entry:match("^request%-.+%.json$") then
        out[#out + 1] = dir .. "/" .. entry
      end
    end
  end)
  if not ok then return {} end
  table.sort(out)   -- lfs.dir order is arbitrary; keep dispatch deterministic
  return out
end

-- Fallback listing for a darktable Lua without lfs. Note the glob stays
-- OUTSIDE the quoted directory so the shell still expands it.
local function list_request_files_ls(dir)
  local out = {}
  local p = io.popen("ls -1 " .. shell_quote(dir) .. "/request-*.json 2>/dev/null")
  if not p then return out end
  for line in p:lines() do
    if not line:match("%.tmp$") then table.insert(out, line) end
  end
  p:close()
  return out
end

local list_request_files
if lfs_available then
  list_request_files = function(dir) return list_request_files_lfs(dir) end
else
  list_request_files = list_request_files_ls
end

local function read_file(path)
  local f = io.open(path, "r")
  if not f then return nil end
  local content = f:read("*a")
  f:close()
  return content
end

-- Returns true on success, or false plus a reason string. Every failure mode
-- (open, write, close, rename) is reported: a silent failure here is
-- indistinguishable, from the client's side, from darktable not running.
local function write_file_atomic(path, content)
  local tmp = path .. ".tmp"
  local f, oerr = io.open(tmp, "w")
  if not f then return false, tostring(oerr) end
  local wok, werr = f:write(content)
  -- close() is where a full disk usually surfaces: the write may sit in the
  -- stdio buffer until flush.
  local cok, cerr = f:close()
  if not wok then os.remove(tmp); return false, tostring(werr) end
  if not cok then os.remove(tmp); return false, tostring(cerr) end
  local rok, rerr = os.rename(tmp, path)
  if not rok then os.remove(tmp); return false, tostring(rerr) end
  return true
end

-- Write the response for `id`, reporting anything that goes wrong. A dropped
-- response costs the caller its entire timeout and then reports "darktable
-- not running" -- so a full disk or a bad permission MUST leave a trace.
local function write_response(dir, id, resp)
  local resp_path = dir .. "/response-" .. id .. ".json"
  local enc_ok, encoded = pcall(json.encode, resp)
  if not enc_ok then
    -- Encoding the result blew up; still answer, with the encoder's error.
    encoded = json.encode({
      id = id,
      error = "response encoding failed: " .. tostring(encoded),
    })
  end
  local ok, err = write_file_atomic(resp_path, encoded)
  if not ok then
    log(string.format(
      "darktable-mcp: FAILED to write %s (%s) -- disk full or permissions? "
      .. "The client will time out and report darktable as not running.",
      resp_path, tostring(err)))
    return false
  end
  return true
end

-- Extract a response-nameable id from a decoded request, or nil.
-- The pattern is a filename-safety check, not a UUID check: the id goes
-- straight into a path, so `/`, `..` and friends must never get through.
local function safe_request_id(req)
  if type(req) ~= "table" then return nil end
  local raw = req.id
  -- A JSON number id is perfectly recoverable -- coerce rather than drop it.
  if type(raw) ~= "string" and type(raw) ~= "number" then return nil end
  raw = tostring(raw)
  if not raw:match("^[%w%-_]+$") then return nil end
  return raw
end

-- Scans `dir` once. Returns the number of request files handled, which the
-- worker loop uses to drive its adaptive poll interval.
local function scan_dir(dir)
  local handled = 0
  for _, req_path in ipairs(list_request_files(dir)) do
    local content = read_file(req_path)
    if content then
      handled = handled + 1
      local ok, req = pcall(json.decode, content)
      local req_id = ok and safe_request_id(req) or nil

      if req_id then
        -- Recoverable: we can name a response file, so ALWAYS write one --
        -- handle() turns any junk in the rest of the request into an error
        -- response rather than silence.
        req.id = req_id
        write_response(dir, req_id, handle(req))
      else
        -- Unrecoverable: with no safely-nameable id there is no response
        -- filename the client would be polling for, so nothing can be sent
        -- back. Log it, so the failure is at least diagnosable, and still
        -- delete the file so it does not get retried forever.
        local reason
        if not ok then
          reason = "unparseable JSON: " .. tostring(req)
        elseif type(req) ~= "table" then
          reason = "top-level JSON value is not an object"
        else
          reason = "missing or unsafe 'id': " .. tostring(req.id)
        end
        log(string.format(
          "darktable-mcp: dropping malformed request %s (%s) -- no response "
          .. "can be addressed, the caller will time out",
          req_path, reason))
      end
      os.remove(req_path)
    end
  end
  return handled
end

-- Delete abandoned files older than max_age_seconds.
--
-- Covers responses as well as requests. When the Python client times out it
-- removes its own request file, but the worker may already be mid-flight and
-- writes response-<uuid>.json afterwards -- with nobody left to read or
-- delete it. Sweeping only request-*.json (the old behaviour) let those
-- orphans accumulate in the cache directory forever, across sessions.
-- *.json.tmp is swept too: a crash between open and rename strands one.
local function sweep_stale(dir, max_age_seconds)
  -- find's -mmin granularity is whole minutes, so 60s == 1min.
  local minutes = math.max(1, math.floor(max_age_seconds / 60))
  os.execute(string.format(
    'find %s -maxdepth 1 \\( -name "request-*.json" -o -name "response-*.json"'
    .. ' -o -name "*.json.tmp" \\) -mmin +%d -delete 2>/dev/null',
    shell_quote(dir), minutes))
end

-- ---- Worker loop -----------------------------------------------------------

-- Adaptive poll cadence.
--
-- The worker used to sleep a flat 100ms forever, so with the `ls` fallback it
-- forked ~10 subprocesses per second -- ~36,000 per hour -- for the entire
-- lifetime of the user's darktable session, even completely idle. Burning a
-- laptop battery to discover an empty directory 36,000 times is not a fair
-- trade for sub-100ms latency the user is not waiting on.
--
-- So: stay fast while requests are actually arriving, and step down after a
-- run of consecutive empty scans. Any request at all snaps the interval
-- straight back to POLL_FAST_MS, so a burst pays the fast cadence from its
-- second request onward and only the first request of an idle period pays the
-- backed-off latency.
local POLL_FAST_MS = 100     -- active: requests arriving, keep latency low
local POLL_MEDIUM_MS = 250   -- cooling down
local POLL_SLOW_MS = 1000    -- idle steady state: ~1 poll per second

-- Thresholds in consecutive IDLE SCANS (not wall-clock, since each tick's
-- length depends on the tier it was in).
--   ticks  1..9   @ 100ms -> ~1.0s of quiet
--   ticks 10..17  @ 250ms -> ~2.0s more (3.0s total)
--   ticks 18+     @ 1000ms -> steady state
local IDLE_TICKS_BEFORE_MEDIUM = 10
local IDLE_TICKS_BEFORE_SLOW = 18

-- Pure function of the idle-tick count, factored out so the progression is
-- unit-testable without running the real (infinite) loop.
local function poll_interval_ms(idle_ticks)
  if idle_ticks >= IDLE_TICKS_BEFORE_SLOW then return POLL_SLOW_MS end
  if idle_ticks >= IDLE_TICKS_BEFORE_MEDIUM then return POLL_MEDIUM_MS end
  return POLL_FAST_MS
end

-- Sweep cadence is measured in ELAPSED SECONDS, not ticks. The old
-- `tick % 100` fired every ~10s at the flat 100ms poll, but under adaptive
-- backoff a tick is 100ms..1000ms, so 100 ticks would drift anywhere from 10s
-- to 100s. Elapsed time keeps the cadence honest whatever the poll tier.
local SWEEP_INTERVAL_SECONDS = 10

-- INVARIANT: STALE_AGE_SECONDS must stay comfortably GREATER than the largest
-- per-method client budget in DEFAULT_TIMEOUTS (darktable_mcp/bridge/client.py
-- -- currently 300s for style.apply, task 8's apply_set batching 25 ids per
-- call). Change either number and you must re-check the other;
-- tests/test_lua_dispatcher.py asserts the relation across the two languages.
--
-- The sweep exists to collect files whose owner is GONE. A request whose
-- caller is still waiting is not abandoned, and the worker is single-threaded:
-- while it serves one 300s-budget call, later requests sit queued in the cache
-- directory doing nothing wrong. At the old 300s (equal to the largest budget)
-- the sweep would have no margin left for the queueing delay ahead of a
-- request stuck behind it, so task 8 raised it to 600s -- comfortably above
-- the new largest budget.
local STALE_AGE_SECONDS = 600

local function worker_loop()
  local dir = cache_dir()
  os.execute("mkdir -p " .. shell_quote(dir))
  local idle_ticks = 0
  local last_sweep = os.time()
  while true do
    local handled = scan_dir(dir)
    if handled > 0 then
      idle_ticks = 0            -- snap back to the fast cadence immediately
    else
      idle_ticks = idle_ticks + 1
    end

    local now = os.time()
    if now - last_sweep >= SWEEP_INTERVAL_SECONDS then
      sweep_stale(dir, STALE_AGE_SECONDS)
      last_sweep = now
    end

    if dt.control and dt.control.sleep then
      dt.control.sleep(poll_interval_ms(idle_ticks))
    else
      log("darktable-mcp: dt.control.sleep missing, worker exiting")
      return
    end
  end
end

-- ---- Entry point -----------------------------------------------------------

log("darktable-mcp bridge: ready")
if dt.control and dt.control.dispatch then
  dt.control.dispatch(worker_loop)
end

-- ---- Test exports (used by tests/lua/test_dispatcher.lua) ------------------
return {
  handle = handle,
  scan_dir = scan_dir,
  methods = methods,
  json = json,
  -- Internals exercised directly by the unit tests.
  shell_quote = shell_quote,
  list_directories_recursive = list_directories_recursive,
  count_images_under = count_images_under,
  poll_for_imported = poll_for_imported,
  import_poll = {
    attempts = IMPORT_POLL_ATTEMPTS,
    interval_ms = IMPORT_POLL_INTERVAL_MS,
    settle_polls = IMPORT_SETTLE_POLLS,
    deadline_seconds = IMPORT_POLL_DEADLINE_SECONDS,
    zero_grace_polls = IMPORT_ZERO_GRACE_POLLS,
    max_depth = IMPORT_MAX_DEPTH,
  },
  cache_dir = cache_dir,
  xml_unescape = xml_unescape,
  dtstyle_file_name = dtstyle_file_name,
  safe_request_id = safe_request_id,
  write_file_atomic = write_file_atomic,
  write_response = write_response,
  sweep_stale = sweep_stale,
  list_request_files = list_request_files,
  list_request_files_ls = list_request_files_ls,
  list_request_files_lfs = list_request_files_lfs,
  lfs_available = lfs_available,
  poll_interval_ms = poll_interval_ms,
  poll_tiers = {
    fast_ms = POLL_FAST_MS,
    medium_ms = POLL_MEDIUM_MS,
    slow_ms = POLL_SLOW_MS,
    idle_ticks_before_medium = IDLE_TICKS_BEFORE_MEDIUM,
    idle_ticks_before_slow = IDLE_TICKS_BEFORE_SLOW,
  },
  sweep_interval_seconds = SWEEP_INTERVAL_SECONDS,
  stale_age_seconds = STALE_AGE_SECONDS,
  -- Scouter phase 1 darkroom verb internals, exercised directly by
  -- tests/lua/test_dispatcher.lua.
  current_view_name = current_view_name,
  gui_action = gui_action,
  is_nan = is_nan,
  ids_match = ids_match,
  find_image_by_path = find_image_by_path,
  darkroom_image = darkroom_image,
  loaded_darkroom_image = loaded_darkroom_image,
  switch_to_darkroom = switch_to_darkroom,
  return_to_darkroom = return_to_darkroom,
  wait_for_view = wait_for_view,
  view_settle = {
    poll_ms = VIEW_SETTLE_POLL_MS,
    budget_ms = VIEW_SETTLE_BUDGET_MS,
    bounce_dwell_ms = VIEW_BOUNCE_DWELL_MS,
  },
  commit_view_bounce = commit_view_bounce,
  image_sidecar_path = image_sidecar_path,
  sidecar_signature = sidecar_signature,
  wait_for_sidecar = wait_for_sidecar,
  sidecar_commit = {
    write_poll_ms = SIDECAR_WRITE_POLL_MS,
    write_budget_ms = SIDECAR_WRITE_BUDGET_MS,
    bounce_attempts = COMMIT_BOUNCE_ATTEMPTS,
  },
  module_actions = MODULE_ACTIONS,
  sidecar_action_paths = SIDECAR_ACTION_PATHS,
}
