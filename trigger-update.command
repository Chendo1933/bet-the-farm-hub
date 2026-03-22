#!/bin/bash
# ── Trigger the GitHub Actions daily-update workflow remotely ─────────────────
# Fires the "Daily Stats Update" workflow on GitHub without needing to git push.
# Useful after manually editing the hub when you want stats refreshed immediately.
#
# Requires: GitHub CLI (gh) — install via: brew install gh
# First-time setup: run  gh auth login  and authenticate with your GitHub account
# ─────────────────────────────────────────────────────────────────────────────

REPO="Chendo1933/bet-the-farm-hub"
WORKFLOW="daily-update.yml"

echo ""
echo "🚀 Triggering Daily Stats Update on GitHub Actions..."
echo ""

# Check gh is installed
if ! command -v gh &>/dev/null; then
  echo "❌ GitHub CLI (gh) not found."
  echo "   Install it with:  brew install gh"
  echo "   Then run:         gh auth login"
  echo ""
  read -p "Press Enter to close..."
  exit 1
fi

# Fire the workflow
gh workflow run "$WORKFLOW" --repo "$REPO" --ref main

if [ $? -eq 0 ]; then
  echo ""
  echo "✅ Workflow triggered! GitHub is now:"
  echo "   → Fetching NBA · NHL · MLB · NFL standings from ESPN"
  echo "   → Patching index.html with latest W/L and scoring stats"
  echo "   → Committing and deploying to GitHub Pages"
  echo ""
  echo "   Watch it run at:"
  echo "   https://github.com/${REPO}/actions"
  echo ""
  echo "   Live hub:"
  echo "   https://chendo1933.github.io/bet-the-farm-hub/"
else
  echo ""
  echo "❌ Trigger failed. Make sure you're authenticated:"
  echo "   gh auth login"
fi

echo ""
read -p "Press Enter to close..."
