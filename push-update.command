#!/bin/bash
cd "/Users/colehenderson/Bet The Farm"
git add -A
git commit -m "update hub - $(date '+%Y-%m-%d %H:%M')"
git push
echo ""
echo "✅ Hub updated! Changes are live at:"
echo "   https://chendo1933.github.io/bet-the-farm-hub/"
echo ""
read -p "Press Enter to close..."
