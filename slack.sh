#!/bin/bash
# slack — One-shot Slack event listener for Claude Code.
#
# Community event source for claude-code-event-listeners.
# Install: /el:register ./slack.sh
#
# Listens on 0.0.0.0:PORT for Slack webhook events behind an ngrok tunnel.
# Filters out bot self-messages, old events (watermark), duplicates, and edits.
# Outputs clean event JSON to stdout on the first real message, then exits.
#
# Args: [port=9999] [bot-id=]
# Env:  SLACK_BOT_ID (alternative to bot-id arg)
#       SLACK_WATERMARK_FILE (default: /tmp/slack-webhook-watermark)
#       SLACK_SEEN_IDS_FILE (default: /tmp/slack-webhook-seen-ids)
#
# Requires: python3, ngrok running separately (forwarding to PORT)
#
# Event Source Protocol:
#   Blocks until a real Slack message arrives.
#   Outputs JSON: {"user": "...", "text": "...", "ts": "...", "channel": "...", ...}

set -euo pipefail

# Resolve through symlinks so companion files are found when registered via el
SCRIPT_DIR="$(cd "$(dirname "$(readlink "$0" 2>/dev/null || echo "$0")")" && pwd)"

PORT="${1:-9999}"
BOT_ID="${2:-${SLACK_BOT_ID:-}}"

exec python3 "$SCRIPT_DIR/slack-listener.py" "$PORT" "$BOT_ID"
