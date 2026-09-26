#!/bin/bash
# Start the Email Link Crawler web UI
# Usage: ./start.sh [port]
# Default port: 8500

PORT="${1:-8500}"
echo "Starting Email Link Crawler UI on http://localhost:$PORT"
echo "Press Ctrl+C to stop"
echo ""

exec python3 server.py "$PORT"
