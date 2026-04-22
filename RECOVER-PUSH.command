#!/bin/bash
# Recovery script for when the scheduled scraper couldn't push.
# Cleans up any stuck git state, commits any pending changes,
# rebases on top of origin, and pushes. Run from Finder (double-click)
# or Terminal to recover and push.

cd "$(dirname "$0")"

echo "== Cleaning up stuck git state =="
rm -f .git/index.lock
rm -f .git/HEAD.lock
rm -f .git/refs/heads/*.lock
rm -rf .git/rebase-merge
rm -rf .git/rebase-apply

echo "== Current status =="
git status --short

echo "== Staging and committing any pending changes =="
git add -A
# Only commit if there's something to commit
if ! git diff --cached --quiet; then
  git commit -m "Daily scraper: calibration + prices (recovery push)"
else
  echo "(nothing new to commit)"
fi

echo "== Rebasing on origin/main and pushing =="
git pull --rebase origin main && git push origin main

echo ""
echo "== Done. You can close this window. =="
read -p "Press enter to close..."
