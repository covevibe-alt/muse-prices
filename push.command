#!/bin/bash
cd ~/muse-prices
git add listener-ratios.json
git commit -m "chore: daily listener ratio update $(date -u +%Y-%m-%d)"
git push origin main
echo ""
echo "=== PUSH COMPLETE ==="
echo "Press any key to close..."
read -n 1
