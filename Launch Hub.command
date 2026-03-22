#!/bin/bash
# Launch Hub.command
# Double-click this file to start the Bet The Farm Hub in your browser.
# macOS will open Terminal, start a local server, and load the hub automatically.

# Move to the folder this script lives in (works regardless of where you double-click from)
cd "$(dirname "$0")"

# Kill anything already running on port 8080
lsof -ti:8080 | xargs kill -9 2>/dev/null

# Start the local server in the background
echo "Starting Bet The Farm Hub on http://localhost:8080 ..."
python3 -m http.server 8080 &
SERVER_PID=$!

# Give it a moment to start
sleep 1

# Open the hub in the default browser
open "http://localhost:8080/Bet%20The%20Farm%20Hub.html"

echo ""
echo "✅ Hub is live at http://localhost:8080/Bet%20The%20Farm%20Hub.html"
echo "   Keep this window open to keep the server running."
echo "   Close this window (or press Ctrl+C) to shut it down."
echo ""

# Wait for the server process so Terminal stays open
wait $SERVER_PID
