# claude-code-el-slack

Community event source for [claude-code-event-listeners](https://github.com/mividtim/claude-code-event-listeners) that listens for Slack messages via webhook.

## Install

```bash
# Clone and register with the event listeners plugin
git clone https://github.com/mividtim/claude-code-el-slack.git
/el:register ./claude-code-el-slack/slack.sh
```

## Prerequisites

- **Slack app** with Event Subscriptions enabled (message and app_mention events)
- **ngrok** (or similar) forwarding to localhost on the listener port
- Your Slack app's Request URL pointed at the ngrok tunnel

## Usage

```
/el:listen slack
```

Or with explicit port and bot ID:

```
/el:listen slack 9999 B0ADWQ06NSV
```

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SLACK_BOT_ID` | *(none)* | Your bot's ID — messages from this bot are filtered out to prevent self-loops |
| `SLACK_WATERMARK_FILE` | `/tmp/slack-webhook-watermark` | File storing the last processed event timestamp |
| `SLACK_SEEN_IDS_FILE` | `/tmp/slack-webhook-seen-ids` | File storing recently seen client message IDs for dedup |

## Output Format

```json
{"user": "U011EBY6S6A", "text": "Hello Herald", "ts": "1770668053.732849", "channel": "C0ADW24BYJV", "type": "message"}
```

Thread replies include `thread_ts`. Messages from other bots include `bot_id`.

## What it handles

| Concern | How |
|---------|-----|
| **Slack URL verification** | Responds to challenge/response handshake automatically |
| **Bot self-filtering** | Skips messages from your own bot ID (configurable) |
| **Watermark dedup** | Skips events older than the last processed timestamp |
| **@mention double-delivery** | Slack sends both `message` and `app_mention` for @mentions — deduplicates via `client_msg_id` |
| **Message edit filtering** | Skips `message_changed` subtypes (typo fixes, edits) |

## Typical workflow

```
Start listener → user posts in Slack →
  listener fires with event JSON → process and respond →
  restart listener → repeat
```

After processing each event, start a new listener to catch the next message. The watermark file ensures you never reprocess old events.

## Requirements

- [claude-code-event-listeners](https://github.com/mividtim/claude-code-event-listeners) plugin installed
- Python 3
- ngrok or similar tunnel (running separately)

## License

MIT
