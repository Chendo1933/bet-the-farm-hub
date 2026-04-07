#!/bin/bash
cd "/Users/colehenderson/Bet The Farm"

# Auto-cleanup any stale git lock files or broken rebase state
[ -f .git/HEAD.lock ]   && rm -f .git/HEAD.lock
[ -f .git/index.lock ]  && rm -f .git/index.lock
[ -d .git/rebase-merge ] && git rebase --abort 2>/dev/null
git worktree prune 2>/dev/null

# If we ended up in detached HEAD state, get back to main
BRANCH=$(git symbolic-ref --short HEAD 2>/dev/null)
if [ -z "$BRANCH" ]; then
  echo "⚠️  Detached HEAD detected — recovering..."
  git checkout main 2>/dev/null
fi

echo "📦 Staging changes..."
git add -A

echo "💾 Committing..."
git diff --staged --quiet || git commit -m "update hub - $(date '+%Y-%m-%d %H:%M')"

echo "🔄 Fetching remote state..."
git fetch origin main 2>/dev/null

echo "⬆️  Pushing to GitHub..."
if ! git push --force-with-lease origin main; then
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
