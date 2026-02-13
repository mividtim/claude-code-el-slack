"""Slack event processor for claude-code-el-slack plugin.

Drains raw Slack webhook events from el-sidecar, verifies signatures,
filters events, and outputs clean message JSON lines to stdout.
Optionally polls conversations.history to catch missed webhooks.

The sidecar stores raw webhook body + headers. This processor handles
all Slack-specific logic: signature verification, event parsing,
bot self-filtering, dedup, and watermark management.

Usage: python3 slack-processor.py

Env vars:
    SLACK_SIGNING_SECRET  — HMAC signing secret for signature verification
    SLACK_TOKEN           — Bot token for conversations.history polling (optional)
    SLACK_CHANNEL         — Channel ID to poll (optional)
    SLACK_BOT_ID          — Bot ID for self-filtering
    SIDECAR_URL           — Sidecar base URL (default: http://localhost:9999)
    SLACK_WATERMARK_FILE  — Watermark path (default: /tmp/el-slack-agent-watermark)
    SLACK_SEEN_IDS_FILE   — Seen IDs path (default: /tmp/slack-webhook-seen-ids)
    SLACK_POLL_INTERVAL   — conversations.history interval (default: 60, 0=disabled)
"""

import hashlib
import hmac
import json
import os
import sys
import threading
import time
import urllib.request

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SLACK_SIGNING_SECRET = os.environ.get('SLACK_SIGNING_SECRET', '')
SLACK_TOKEN = os.environ.get('SLACK_TOKEN', '')
SLACK_CHANNEL = os.environ.get('SLACK_CHANNEL', '')
MY_BOT_ID = os.environ.get('SLACK_BOT_ID', '')
SIDECAR_URL = os.environ.get('SIDECAR_URL', 'http://localhost:9999').rstrip('/')
WATERMARK_FILE = os.environ.get('SLACK_WATERMARK_FILE', '/tmp/el-slack-agent-watermark')
SEEN_IDS_FILE = os.environ.get('SLACK_SEEN_IDS_FILE', '/tmp/slack-webhook-seen-ids')
POLL_INTERVAL = int(os.environ.get('SLACK_POLL_INTERVAL', '60'))
MAX_SEEN_IDS = 50
MAX_TIMESTAMP_AGE = 300  # 5 minutes — reject older Slack timestamps

# ---------------------------------------------------------------------------
# Thread-safe output
# ---------------------------------------------------------------------------

_output_lock = threading.Lock()


def _emit(msg):
    """Write a JSON line to stdout, thread-safe."""
    line = json.dumps(msg)
    with _output_lock:
        sys.stdout.write(line + '\n')
        sys.stdout.flush()


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------

def _verify_signature(timestamp, body, signature):
    """Verify Slack request signature using HMAC-SHA256.

    Returns True if valid, or if no signing secret is configured.
    """
    if not SLACK_SIGNING_SECRET:
        return True
    if not timestamp or not signature:
        return False
    try:
        if abs(time.time() - float(timestamp)) > MAX_TIMESTAMP_AGE:
            return False
    except (ValueError, TypeError):
        return False
    sig_basestring = f"v0:{timestamp}:{body}"
    computed = 'v0=' + hmac.new(
        SLACK_SIGNING_SECRET.encode(),
        sig_basestring.encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(computed, signature)


# ---------------------------------------------------------------------------
# Watermark
# ---------------------------------------------------------------------------

_watermark_lock = threading.Lock()


def _read_watermark():
    try:
        with open(WATERMARK_FILE, 'r') as f:
            return f.read().strip()
    except FileNotFoundError:
        return '0'


def _write_watermark(ts):
    with open(WATERMARK_FILE, 'w') as f:
        f.write(ts)


def _check_watermark(event_ts):
    """Return True if event_ts is newer than the current watermark."""
    with _watermark_lock:
        watermark = _read_watermark()
        try:
            return float(event_ts) > float(watermark)
        except ValueError:
            return True


def _advance_watermark(event_ts):
    """Advance watermark if event_ts is newer."""
    with _watermark_lock:
        watermark = _read_watermark()
        try:
            if float(event_ts) > float(watermark):
                _write_watermark(event_ts)
        except ValueError:
            pass


# ---------------------------------------------------------------------------
# Seen IDs (client_msg_id dedup)
# ---------------------------------------------------------------------------

_seen_ids_lock = threading.Lock()


def _load_seen_ids():
    try:
        with open(SEEN_IDS_FILE, 'r') as f:
            return [x for x in f.read().strip().split('\n') if x]
    except FileNotFoundError:
        return []


def _save_seen_ids(ids):
    with open(SEEN_IDS_FILE, 'w') as f:
        f.write('\n'.join(ids[-MAX_SEEN_IDS:]))


def _check_and_add_seen_id(client_msg_id):
    """Returns True if the ID was already seen. Adds it if not."""
    if not client_msg_id:
        return False
    with _seen_ids_lock:
        seen = _load_seen_ids()
        if client_msg_id in seen:
            return True
        seen.append(client_msg_id)
        _save_seen_ids(seen)
        return False


# ---------------------------------------------------------------------------
# Event filtering (preserves slack-server.py logic exactly)
# ---------------------------------------------------------------------------

def _filter_event(event):
    """Apply all filters. Returns clean message dict, or None to skip."""
    # Only process message and app_mention events
    if event.get('type', '') not in ('message', 'app_mention'):
        return None

    # Skip own bot messages
    if MY_BOT_ID:
        if event.get('bot_id') == MY_BOT_ID:
            return None
        if event.get('subtype') == 'bot_message' and event.get('bot_id') == MY_BOT_ID:
            return None

    # Skip message edits and deletes
    if event.get('subtype') in ('message_changed', 'message_deleted'):
        return None

    # Deduplicate @mention double-delivery via client_msg_id
    if _check_and_add_seen_id(event.get('client_msg_id', '')):
        return None

    # Build clean output
    msg = {
        'user': event.get('user', ''),
        'text': event.get('text', ''),
        'ts': event.get('ts', ''),
        'channel': event.get('channel', ''),
        'type': event.get('type', ''),
    }
    if event.get('thread_ts'):
        msg['thread_ts'] = event['thread_ts']
    if event.get('bot_id'):
        msg['bot_id'] = event['bot_id']
    return msg


# ---------------------------------------------------------------------------
# Sidecar drain
# ---------------------------------------------------------------------------

def _drain_events():
    """Long-poll the sidecar for raw events. Returns list or None on failure."""
    url = f"{SIDECAR_URL}/events?wait=true&source=slack"
    try:
        resp = urllib.request.urlopen(url, timeout=35)
        data = json.loads(resp.read())
        return data if isinstance(data, list) else []
    except Exception:
        return None


def _process_raw_event(raw_event):
    """Process a single raw event from the sidecar. Emits to stdout if valid."""
    if raw_event.get('source') != 'slack':
        return

    headers = raw_event.get('headers', {})
    body_str = raw_event.get('body', '')

    # Signature verification
    sig = headers.get('X-Slack-Signature', headers.get('x-slack-signature', ''))
    ts = headers.get('X-Slack-Request-Timestamp', headers.get('x-slack-request-timestamp', ''))
    if not _verify_signature(ts, body_str, sig):
        sys.stderr.write("[slack-processor] Rejected: invalid signature\n")
        return

    # Parse body
    try:
        data = json.loads(body_str) if isinstance(body_str, str) else body_str
    except (json.JSONDecodeError, TypeError):
        return

    # Only process event_callback
    if data.get('type') != 'event_callback':
        return

    event = data.get('event', {})

    # Apply filters
    msg = _filter_event(event)
    if msg is None:
        return

    # Watermark check
    event_ts = event.get('event_ts', event.get('ts', '0'))
    if not _check_watermark(event_ts):
        return

    _emit(msg)
    _advance_watermark(event_ts)


# ---------------------------------------------------------------------------
# Conversations.history poller (catches missed webhooks)
# ---------------------------------------------------------------------------

def _poll_conversations_history():
    """Background thread: poll conversations.history for missed messages."""
    if not SLACK_TOKEN or not SLACK_CHANNEL or POLL_INTERVAL <= 0:
        return

    sys.stderr.write(
        f"[slack-processor] Polling conversations.history every {POLL_INTERVAL}s\n"
    )
    oldest = _read_watermark()

    while True:
        time.sleep(POLL_INTERVAL)
        try:
            url = (
                f"https://slack.com/api/conversations.history"
                f"?channel={SLACK_CHANNEL}&limit=20"
            )
            if oldest and oldest != '0':
                url += f"&oldest={oldest}"

            req = urllib.request.Request(url, headers={
                'Authorization': f'Bearer {SLACK_TOKEN}',
                'Content-Type': 'application/json',
            })
            resp = urllib.request.urlopen(req, timeout=10)
            data = json.loads(resp.read())

            if not data.get('ok'):
                sys.stderr.write(
                    f"[slack-processor] conversations.history error: "
                    f"{data.get('error', 'unknown')}\n"
                )
                continue

            messages = data.get('messages', [])
            new_max_ts = oldest

            for raw_msg in messages:
                msg = _filter_event(raw_msg)
                if msg is None:
                    continue

                msg_ts = raw_msg.get('ts', '0')
                try:
                    if float(msg_ts) <= float(oldest):
                        continue
                except ValueError:
                    continue

                # conversations.history doesn't include channel
                if not msg.get('channel'):
                    msg['channel'] = SLACK_CHANNEL

                _emit(msg)

                if msg_ts > new_max_ts:
                    new_max_ts = msg_ts

            if new_max_ts > oldest:
                _advance_watermark(new_max_ts)
                oldest = new_max_ts

        except Exception as e:
            sys.stderr.write(f"[slack-processor] poll error: {e}\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    sys.stderr.write(f"[slack-processor] Starting (sidecar: {SIDECAR_URL})\n")
    sys.stderr.write(f"[slack-processor] Bot ID filter: {MY_BOT_ID or '(none)'}\n")
    sys.stderr.write(
        f"[slack-processor] Signature verification: "
        f"{'enabled' if SLACK_SIGNING_SECRET else 'disabled'}\n"
    )

    # Start conversations.history poller in background
    if SLACK_TOKEN and SLACK_CHANNEL and POLL_INTERVAL > 0:
        threading.Thread(target=_poll_conversations_history, daemon=True).start()

    backoff = 1
    max_backoff = 30

    while True:
        result = _drain_events()

        if result is None:
            # Sidecar unavailable — retry with exponential backoff
            sys.stderr.write(
                f"[slack-processor] Sidecar unavailable, retrying in {backoff}s\n"
            )
            time.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)
            continue

        # Connected — reset backoff
        backoff = 1

        for raw_event in result:
            _process_raw_event(raw_event)


if __name__ == '__main__':
    main()
