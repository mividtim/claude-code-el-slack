# claude-code-el-slack

Community event source for [claude-code-event-listeners](https://github.com/mividtim/claude-code-event-listeners) that listens for Slack messages via webhook.

## Install

```bash
# From the marketplace (recommended — auto-discovers, pulls in el as dependency)
claude plugin marketplace add mividtim/claude-code-el-slack
claude plugin install el-slack

# Or manually register the source
git clone https://github.com/mividtim/claude-code-el-slack.git
/el:register ./claude-code-el-slack/sources.d/slack.sh
```

## Prerequisites

- **Slack app** with Event Subscriptions enabled (message and app_mention events)
- **el-sidecar** running (ships with the el core plugin)
- **ngrok** (or similar) forwarding to the sidecar port (default: 9999)
- Your Slack app's Request URL pointed at `<ngrok-url>/slack`

## Architecture

`slack.sh` runs `slack-processor.py`, which drains raw webhook events from
el-sidecar (the core el plugin's generic HTTP event buffer). The processor
handles all Slack-specific logic: signature verification, event filtering,
watermark dedup, and optional `conversations.history` polling.

```
ngrok tunnel --> el-sidecar (POST /slack, buffers raw body+headers in SQLite)
                      |
                      v
                 slack-processor.py (drains ?source=slack, verifies, filters)
                      |
                      v
                 stdout JSONL --> el plugin
```

The sidecar is source-agnostic — it stores raw requests and tags them by URL
path. The processor is the Slack specialist. This means multiple event sources
(Slack, voice, GitHub, etc.) share one sidecar on one port.

### Legacy mode

The original one-shot listener (`slack-listener.py`) is still included as a fallback:

```bash
python3 sources.d/slack-listener.py [port] [bot-id]
```

## Usage

```
/el:listen slack
```

Or with explicit port and bot ID:

```
/el:listen slack 9999 B0ADWQ06NSV
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SLACK_SIGNING_SECRET` | *(none)* | HMAC signing secret for webhook signature verification |
| `SLACK_BOT_ID` | *(none)* | Your bot's ID — messages from this bot are filtered out to prevent self-loops |
| `SLACK_TOKEN` | *(none)* | Bot token for optional `conversations.history` polling |
| `SLACK_CHANNEL` | *(none)* | Channel ID for optional `conversations.history` polling |
| `SIDECAR_URL` | `http://localhost:9999` | el-sidecar base URL |
| `SLACK_WATERMARK_FILE` | `/tmp/el-slack-agent-watermark` | File storing the last processed event timestamp |
| `SLACK_SEEN_IDS_FILE` | `/tmp/slack-webhook-seen-ids` | File storing recently seen client message IDs for dedup |
| `SLACK_POLL_INTERVAL` | `60` | Seconds between `conversations.history` polls (0 = disabled) |

## Output Format

```json
{"user": "U011EBY6S6A", "text": "Hello Herald", "ts": "1770668053.732849", "channel": "C0ADW24BYJV", "type": "message"}
```

Thread replies include `thread_ts`. Messages from other bots include `bot_id`.

When multiple messages are buffered, each is output on its own line (JSONL format).

## What it handles

| Concern | How |
|---------|-----|
| **Slack URL verification** | el-sidecar echoes the challenge automatically |
| **Signature verification** | HMAC-SHA256 verification of `X-Slack-Signature` (optional, requires `SLACK_SIGNING_SECRET`) |
| **Bot self-filtering** | Skips messages from your own bot ID (configurable) |
| **Watermark dedup** | Skips events older than the last processed timestamp |
| **@mention double-delivery** | Slack sends both `message` and `app_mention` for @mentions — deduplicates via `client_msg_id` |
| **Message edit filtering** | Skips `message_changed` and `message_deleted` subtypes |
| **Missed message recovery** | Optional `conversations.history` polling catches webhook gaps |

## Typical workflow

```
Slack webhook --> el-sidecar (POST /slack) --> SQLite buffer
                                                    |
slack-processor.py (GET /events?source=slack) <-----+
         |
         v
    stdout JSONL --> el plugin --> agent processes events
```

The sidecar buffers raw webhooks persistently. The processor drains and
filters them. The watermark file ensures you never reprocess old events.

## Requirements

- [claude-code-event-listeners](https://github.com/mividtim/claude-code-event-listeners) plugin installed (v0.8.0+ with el-sidecar)
- Python 3
- ngrok or similar tunnel (forwarding to el-sidecar port)

## License

MIT
