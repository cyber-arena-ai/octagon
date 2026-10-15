#!/usr/bin/env bash
# Bump the challenge-warehouse submodule to the latest commit on its `main`
# branch and stage the new pointer. (Submodules pin a SHA; this is how you
# move that pin forward — `branch = main` in .gitmodules is what makes
# `--remote` track main.)
#
#   ./scripts/update_challenges.sh        # bump + stage; you commit
#   ./scripts/update_challenges.sh --commit   # bump + commit
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

git submodule update --remote --init challenge-warehouse

if git diff --quiet -- challenge-warehouse; then
  echo "challenge-warehouse already at latest main."
  exit 0
fi

sha="$(git -C challenge-warehouse rev-parse --short HEAD)"
git add challenge-warehouse
echo "challenge-warehouse bumped to ${sha}."

if [ "${1:-}" = "--commit" ]; then
  git commit -m "chore: bump challenge-warehouse to ${sha} (latest main)"
  echo "committed."
else
  echo "staged — run 'git commit' to record the new pointer."
fi
