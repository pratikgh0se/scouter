#!/usr/bin/env bash
# Shared interpreter resolver (Z8 few parts, card t_7a05352a): the single
# chain scripts/mutation_check.sh and scripts/htc_post_run.sh both need
# (row 478 / t_0f1197e6), extracted so it exists exactly once. Sourced, not
# executed -- it defines resolve_scouter_python and nothing else, stdlib
# shell only.
#
# resolve_scouter_python <script_home> resolves, in order, and sets $PYTHON:
#   1. $SCOUTER_PYTHON, if set AND executable (WARNs and falls through if
#      set but not executable); ambient $PYTHON is never consulted -- only
#      this chain ever assigns it (fix round 1 / R2-I1).
#   2. <script_home>/.venv/bin/python3 (the caller's own checkout's venv).
#   3. The MAIN checkout's venv, found via `git -C <script_home> rev-parse
#      --git-common-dir`, guarded to a `.git` basename so it never picks a
#      bare-repo sibling's venv or a submodule's nested common dir (M1).
#   4. A bare `python3` on PATH.
resolve_scouter_python() {
  local script_home="$1"
  PYTHON=""
  if [[ -n "${SCOUTER_PYTHON:-}" ]]; then
    if [[ -x "$SCOUTER_PYTHON" ]]; then
      PYTHON="$SCOUTER_PYTHON"
    else
      echo "WARN: SCOUTER_PYTHON=$SCOUTER_PYTHON is not executable; ignoring" >&2
    fi
  fi
  if [[ -z "${PYTHON:-}" ]]; then
    if [[ -x "$script_home/.venv/bin/python3" ]]; then
      PYTHON="$script_home/.venv/bin/python3"
    else
      local common_dir
      common_dir="$(git -C "$script_home" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)"
      if [[ "${common_dir##*/}" == .git && -x "${common_dir%/.git}/.venv/bin/python3" ]]; then
        PYTHON="${common_dir%/.git}/.venv/bin/python3"
      else
        PYTHON=python3
      fi
    fi
  fi
}
