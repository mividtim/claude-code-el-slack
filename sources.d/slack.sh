#!/bin/bash
# slack — Slack event source for Claude Code (sidecar processor mode).
#
# Community event source for claude-code-event-listeners.
# Install: claude plugin marketplace add mividtim/claude-code-el-slack
#          claude plugin install el-slack
# Or manually: /el:register ./sources.d/slack.sh
#
# Runs slack-processor.py which drains raw events from el-sidecar,
# processes them (signature verification, filtering, dedup), and
# outputs clean JSONL to stdout. The processor handles sidecar
# unavailability with exponential backoff.
#
# Env:  SLACK_SIGNING_SECRET (for webhook signature verification)
#       SLACK_BOT_ID (for self-filtering)
#       SLACK_TOKEN (optional, for conversations.history polling)
#       SLACK_CHANNEL (optional, for conversations.history polling)
#       SIDECAR_URL (default: http://localhost:9999)
#       SLACK_WATERMARK_FILE (default: /tmp/el-slack-agent-watermark)
#       SLACK_SEEN_IDS_FILE (default: /tmp/slack-webhook-seen-ids)
#       SLACK_POLL_INTERVAL (default: 60, 0=disabled)
#
# Requires: python3, el-sidecar running (processor retries if not)
#
# Event Source Protocol:
#   Outputs JSONL continuously: one JSON object per line.
#   {"user": "...", "text": "...", "ts": "...", "channel": "...", ...}

set -euo pipefail

# Resolve through symlinks so companion files are found when registered via el
SCRIPT_DIR="$(cd "$(dirname "$(readlink "$0" 2>/dev/null || echo "$0")")" && pwd)"

exec python3 "$SCRIPT_DIR/slack-processor.py"
