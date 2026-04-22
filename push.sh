#!/bin/bash
set -e
cd ~/muse-prices

# Clean up stuck git state
rm -f .git/index.lock
git rebase --abort 2>/dev/null || true

# Save updated file
cp listener-ratios.json /tmp/lr-backup.json

# Reset to clean state from remote
git fetch origin
git reset --hard origin/main

# Restore updated file
cp /tmp/lr-backup.json listener-ratios.json

# Commit and push
git add listener-ratios.json
git commit -m "chore: daily listener ratio update $(date -u +%Y-%m-%d)"
git push origin main

echo ""
echo "=== PUSH COMPLETE ==="
rm -f push.sh
