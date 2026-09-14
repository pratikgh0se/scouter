-- Scouter phase 2 task 11 probe: which `dt.gui.action` paths reach the
-- `temperature` module's per-channel COEFFICIENT sliders (red/green/blue/
-- emerald), as opposed to the ordinary Kelvin/tint sliders that
-- `dtbridge.ACTION_PATHS` already pins (`iop/temperature/temperature`).
--
-- Standalone artifact, not exercised by CI: `tests/test_host_phase2.py::
-- PinWbCoefficientSlidersHostTest.test_pin_wb_coefficient_sliders` is the
-- test that actually runs and records this probe, through the real darktable_mcp
-- bridge (`darkroom.status`), because that is what `dtbridge.py`/
-- `darkroom.py` consume in production and what the brief asks to be pinned.
-- This script exists alongside it as the plain, no-bridge, no-Python
-- reproduction: load it directly from a throwaway configdir's luarc (same
-- pattern as spike/scouter_commit_probe.lua) to re-run the same probe by
-- hand, e.g. when the bridge itself is suspect and a lower-level check is
-- needed.
--
-- THE FACT this probe exists to test (wb.py's own docstring, phase 1 design
-- 7.2): `temperature` modversion 4 stores `coeff_r, coeff_g, coeff_b,
-- coeff_g2, preset` -- camera-relative multipliers, not Kelvin. If any of
-- these paths answers a real (non-nan) `dt.gui.action(path, "get")`, an
-- absolute `set` on it is an EXACT temperature undo (design 11.3, tier 1).
-- If none do, that is not a probe failure -- it is the evidence for tier 4's
-- honest refusal, and this probe's whole job is to produce that evidence
-- honestly, never to guess a path that merely sounds right.
local dt = require("darktable")

local OUT = os.getenv("SCOUTER_PROBE_OUT") or "/tmp/scouter-wb-probe.jsonl"

local function log_line(s)
  local f = io.open(OUT, "a")
  if f then f:write(s .. "\n"); f:close() end
  if dt.print_log then dt.print_log("scouter-wb-probe: " .. s) end
end

local function jstr(s)
  return '"' .. tostring(s):gsub('\\', '\\\\'):gsub('"', '\\"') .. '"'
end

local function record(path, value, note)
  local v = "null"
  if type(value) == "number" then
    if value ~= value then v = jstr("nan") else v = tostring(value) end
  elseif value ~= nil then
    v = jstr(value)
  end
  log_line(string.format('{"path":%s,"value":%s,"note":%s}', jstr(path), v, note and jstr(note) or "null"))
end

-- Exactly the brief's own candidate list (scouter/core/wb.py step 3),
-- red/green/blue/emerald under two naming schemes, plus the two module-level
-- widgets that might expose the whole vector at once.
local CANDIDATES = {
  "iop/temperature/red",
  "iop/temperature/green",
  "iop/temperature/blue",
  "iop/temperature/emerald",
  "iop/temperature/channel coefficients/red",
  "iop/temperature/channel coefficients/green",
  "iop/temperature/channel coefficients/blue",
  "iop/temperature/channel coefficients/emerald",
  "iop/temperature/scene illuminant temp",
  "iop/temperature/settings",
}

local function run_probe()
  record("probe_start", 1, "scouter_wb_probe.lua")
  for _, path in ipairs(CANDIDATES) do
    local ok, value = pcall(dt.gui.action, path, 0, "value", "get", 0.0)
    if ok then
      record(path, value, nil)
    else
      record(path, nil, "dt.gui.action raised: " .. tostring(value))
    end
  end
  record("probe_end", 1, nil)
end

-- Give the darkroom a moment to settle (this script is meant to be loaded
-- once an image is already showing in the darkroom -- see
-- tests/test_host_phase2.py for the real readiness check this repo relies
-- on instead of a blind sleep, when driven through Python).
dt.control.dispatch(run_probe)
