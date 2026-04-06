#!/bin/bash
cd "/Users/colehenderson/Bet The Farm"

echo "📦 Staging changes..."
git add -A

echo "💾 Committing..."
git diff --staged --quiet || git commit -m "update hub - $(date '+%Y-%m-%d %H:%M')"

echo "⬇️  Pulling latest from remote..."
if ! git pull --rebase; then
  echo ""
  echo "❌ Pull failed. Check for conflicts or run 'git status' to diagnose."
  echo ""
  read -p "Press Enter to close..."
  exit 1
fi

echo "⬆️  Pushing to GitHub..."
if ! git push; then
  echo ""
  echo "❌ Push failed. Check above for details."
  echo ""
  read -p "Press Enter to close..."
  exit 1
fi

echo ""
echo "✅ Hub updated! Changes are live at:"
echo "   https://chendo1933.github.io/bet-the-farm-hub/"
echo ""
read -p "Press Enter to close..."
