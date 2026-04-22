#!/bin/bash
set +e
cd ~/muse-prices

echo "=== Killing any stray git processes ==="
pkill -9 -f "git " 2>/dev/null
sleep 1

echo "=== Removing all stale lock files ==="
rm -f .git/index.lock .git/HEAD.lock .git/config.lock .git/packed-refs.lock
find .git -name "*.lock" -delete 2>/dev/null
rm -rf .git/rebase-merge .git/rebase-apply 2>/dev/null

echo "=== Aborting any stuck rebase/merge ==="
git rebase --abort 2>/dev/null
git merge --abort 2>/dev/null

echo "=== Backing up listener-ratios.json ==="
cp listener-ratios.json /tmp/lr-backup.json

echo "=== Fetching and resetting to origin/main ==="
git fetch origin
git reset --hard origin/main

echo "=== Restoring updated listener-ratios.json ==="
cp /tmp/lr-backup.json listener-ratios.json

echo "=== Removing defunct scrape-listeners.yml workflow ==="
git rm .github/workflows/scrape-listeners.yml 2>/dev/null
rm -f .github/workflows/scrape-listeners.yml

echo "=== Committing and pushing ==="
git add -A listener-ratios.json .github/workflows/scrape-listeners.yml
git commit -m "chore: daily listener ratio update $(date -u +%Y-%m-%d); remove defunct scrape-listeners workflow"
git push origin main

PUSH_STATUS=$?

echo ""
if [ $PUSH_STATUS -eq 0 ]; then
  echo "=== PUSH SUCCESSFUL ==="
else
  echo "=== PUSH FAILED (exit $PUSH_STATUS) ==="
fi
echo ""
echo "Press any key to close..."
read -n 1
