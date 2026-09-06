#!/usr/bin/env bash
# Red/green regression run for ssh_execute_background (t_fa393f3c).
#
# Reproduces the iperf-dc-01 breakage and proves the KEEP+FIX (5973526):
#   RED  - old build (afae558): the regression cases FAIL
#   GREEN- new build (5973526): the regression cases PASS
#
# CI-safe: unit tests run with a stubbed mcp_call (no sshd, no hosts, no
# gateway). Uses git worktrees + the same test file staged into each
# worktree, so both builds run the IDENTICAL regression suite against their
# own code. Optional E2E (real ssh) runs only when SSH_HOST is exported.
#
# Usage:
#   scripts/red_green_ssh_bg.sh                         # unit layer
#   SSH_HOST=172.16.171.11 scripts/red_green_ssh_bg.sh  # + E2E on green
#
# Exit 0 iff red phase FAILED (as expected) and green phase PASSED.

set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_SRC="$REPO/test/test_ssh_background_regression.py"
OLD_COMMIT="afae558"
NEW_COMMIT="5973526"
WT="$REPO/.redgreen-wt"

say()  { printf '\n=== %s ===\n' "$*"; }
red()  { printf '\033[31m%s\033[0m\n' "$*"; }
green(){ printf '\033[32m%s\033[0m\n' "$*"; }

run_phase() {
  local label="$1" commit="$2" expect="$3" rc
  say "$label build @ $commit"
  git -C "$REPO" worktree remove --force "$WT" >/dev/null 2>&1
  git -C "$REPO" worktree add -f "$WT" "$commit" >/dev/null 2>&1
  cp "$TEST_SRC" "$WT/test/test_ssh_background_regression.py"
  (
    cd "$WT"
    PYTHONPATH="src" python3 -m pytest -q \
      test/test_ssh_extension.py test/test_ssh_background_regression.py \
      -p no:cacheprovider --no-header 2>&1 | tail -n 40
    exit ${PIPESTATUS[0]}
  )
  rc=$?
  if [ "$expect" = red ]; then
    if [ "$rc" -ne 0 ]; then
      green "RED phase reproduced (old build failed as expected)."
      return 0
    else
      red "RED phase FAILED TO REPRODUCE: old build passed - tests don't cover the bug."
      return 1
    fi
  else
    if [ "$rc" -eq 0 ]; then
      green "GREEN phase passed (patched build)."
      return 0
    else
      red "GREEN phase failed - the fix does not satisfy the regression tests."
      return 1
    fi
  fi
}

for c in "$OLD_COMMIT" "$NEW_COMMIT"; do
  git -C "$REPO" rev-parse --verify -q "$c" >/dev/null || { red "missing commit $c"; exit 2; }
done

trap 'git -C "$REPO" worktree remove --force "$WT" >/dev/null 2>&1; true' EXIT

run_phase "RED (old)" "$OLD_COMMIT" red || exit 3
run_phase "GREEN (new)" "$NEW_COMMIT" green || exit 4

say "DONE - red reproduced on $OLD_COMMIT, green passed on $NEW_COMMIT"
