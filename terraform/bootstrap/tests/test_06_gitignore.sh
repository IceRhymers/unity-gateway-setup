#!/bin/sh
# test_06_gitignore.sh — .lakebase.env is ignored, and backend.tf is tracked.
# The inverse matters: a .gitignore rule that swept up backend.tf would silently
# restore the worktree failure mode this whole design exists to prevent.
set -eu

# shellcheck disable=SC2164
TESTS_DIR="$(cd "$(dirname -- "$0")" && pwd)"
BOOTSTRAP_DIR=$(dirname "${TESTS_DIR}")
REPO_ROOT=$(dirname "$(dirname "${BOOTSTRAP_DIR}")")
T="test_06_gitignore"

if ! command -v git >/dev/null 2>&1; then
  printf 'FAIL: %s — git is required and is not installed\n' "${T}"
  exit 1
fi

# .lakebase.env must be ignored.
if ! git -C "${REPO_ROOT}" check-ignore -q terraform/infra/.lakebase.env; then
  printf 'FAIL: %s — terraform/infra/.lakebase.env is not gitignored\n' "${T}"
  exit 1
fi

# backend.tf must be tracked (staged or committed both count).
if ! git -C "${REPO_ROOT}" ls-files --error-unmatch terraform/infra/backend.tf >/dev/null 2>&1; then
  printf 'FAIL: %s — terraform/infra/backend.tf is not tracked. The backend must\n' "${T}"
  printf '       travel with every worktree and fresh clone; gitignoring it would\n'
  printf '       reintroduce the silent "1 to add" failure.\n'
  exit 1
fi

printf '  ok: .lakebase.env ignored and backend.tf tracked\n'
printf 'PASS: %s\n' "${T}"
