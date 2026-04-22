#!/bin/bash
set -u
cd ~/muse-prices

echo "==> Clearing stuck git state"
rm -f .git/index.lock
rm -f .git/HEAD.lock
rm -rf .git/rebase-merge .git/rebase-apply
git rebase --abort 2>/dev/null || true

echo "==> Saving current listener-ratios.json"
cp listener-ratios.json /tmp/lr-backup.json || { echo "missing file"; exit 1; }

echo "==> Fetching origin"
git fetch origin

echo "==> Hard-resetting to origin/main"
git reset --hard origin/main

echo "==> Restoring updated listener-ratios.json"
cp /tmp/lr-backup.json listener-ratios.json

echo "==> Committing"
git add listener-ratios.json
git commit -m "chore: daily listener ratio update $(date -u +%Y-%m-%d)"

echo "==> Pushing"
git push origin main

echo ""
echo "=== PUSH COMPLETE ==="
echo "Press any key to close..."
read -n 1
