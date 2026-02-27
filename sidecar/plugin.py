"""Slack source plugin for el-sidecar.

Handles all Slack-specific logic:
    - Webhook ingestion (POST /slack)
    - conversations.history + conversations.replies polling
    - Event filtering (bot self-filter, dedup, subtypes)
    - Watermark management
    - Slack API helpers

Registers itself with el-sidecar via register(sidecar).

Env vars:
    SLACK_TOKEN            — Bot token for API calls
    SLACK_CHANNEL          — Channel ID to poll
    SLACK_BOT_ID           — Bot ID for self-filtering
    SLACK_WATERMARK_FILE   — Watermark path (default: /tmp/el-slack-agent-watermark)
    SLACK_POLL_INTERVAL    — Polling interval in seconds (default: 60, 0=disabled)
"""

import json
import os
import sys
import threading
import time
import urllib.request

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SLACK_TOKEN = os.environ.get('SLACK_TOKEN', '')
SLACK_CHANNEL = os.environ.get('SLACK_CHANNEL', '')
MY_BOT_ID = os.environ.get('SLACK_BOT_ID', '')
WATERMARK_FILE = os.environ.get('SLACK_WATERMARK_FILE', '/tmp/el-slack-agent-watermark')
POLL_INTERVAL = int(os.environ.get('SLACK_POLL_INTERVAL', '60'))
MAX_SEEN_IDS = 50

# Sidecar reference (set during register())
_sidecar = None

# ---------------------------------------------------------------------------
# Watermark
# ---------------------------------------------------------------------------


def _read_watermark():
    try:
        with open(WATERMARK_FILE, 'r') as f:
            return f.read().strip()
    except FileNotFoundError:
        return '0'


def _write_watermark(ts):
    with open(WATERMARK_FILE, 'w') as f:
        f.write(ts)


# ---------------------------------------------------------------------------
# Seen-IDs dedup (client_msg_id)
# ---------------------------------------------------------------------------

_seen_ids = []
_seen_ids_lock = threading.Lock()


def _check_and_add_seen_id(client_msg_id):
    """Returns True if the ID was already seen. Adds it if not."""
    if not client_msg_id:
        return False
    with _seen_ids_lock:
        if client_msg_id in _seen_ids:
            return True
        _seen_ids.append(client_msg_id)
        while len(_seen_ids) > MAX_SEEN_IDS:
            _seen_ids.pop(0)
        return False


# ---------------------------------------------------------------------------
# Event filtering
# ---------------------------------------------------------------------------


def _filter_slack_event(event):
    """Apply all Slack filters. Returns a clean message dict, or None to skip."""
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
    client_msg_id = event.get('client_msg_id', '')
    if _check_and_add_seen_id(client_msg_id):
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
# Slack API helper
# ---------------------------------------------------------------------------


def _slack_api(endpoint, params):
    """Call a Slack API endpoint. Returns parsed JSON or None."""
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"https://slack.com/api/{endpoint}?{qs}"
    req = urllib.request.Request(url, headers={
        'Authorization': f'Bearer {SLACK_TOKEN}',
        'Content-Type': 'application/json',
    })
    resp = urllib.request.urlopen(req, timeout=10)
    data = json.loads(resp.read())
    if not data.get('ok'):
        sys.stderr.write(f"[el-slack] {endpoint} error: {data.get('error', 'unknown')}\n")
        return None
    return data


# ---------------------------------------------------------------------------
# Event ingestion
# ---------------------------------------------------------------------------


def _ingest_message(raw_msg, fallback_channel=None):
    """Filter and insert a Slack message via sidecar. Returns True if new."""
    assert _sidecar is not None, "el-slack not registered"
    msg = _filter_slack_event(raw_msg)
    if msg is None:
        return False
    if not msg.get('channel') and fallback_channel:
        msg['channel'] = fallback_channel

    inserted = _sidecar['insert_event'](
        source='slack',
        ts=msg.get('ts', ''),
        user_id=msg.get('user', ''),
        text=msg.get('text', ''),
        channel=msg.get('channel', ''),
        type=msg.get('type', ''),
        thread_ts=msg.get('thread_ts', ''),
        bot_id=msg.get('bot_id', ''),
    )
    if inserted:
        _sidecar['notify_waiters']()
    return inserted


# ---------------------------------------------------------------------------
# Webhook handler
# ---------------------------------------------------------------------------


def handle_webhook(handler):
    """POST /slack — Slack Event Subscriptions webhook."""
    body = handler._read_body()
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        handler._send_json({"error": "invalid json"}, 400)
        return

    # URL verification handshake
    if data.get('type') == 'url_verification':
        challenge = data.get('challenge', '')
        handler.send_response(200)
        handler.send_header('Content-Type', 'text/plain')
        handler.end_headers()
        handler.wfile.write(str(challenge).encode())
        return

    # Ack Slack immediately (must respond within 3s)
    handler.send_response(200)
    handler.end_headers()

    if data.get('type') != 'event_callback':
        return

    event = data.get('event', {})
    msg = _filter_slack_event(event)
    if msg is None:
        return

    # Watermark check — skip events older than last processed
    event_ts = event.get('event_ts', event.get('ts', '0'))
    watermark = _read_watermark()
    try:
        if float(event_ts) <= float(watermark):
            return
    except ValueError:
        pass

    _ingest_message(event, fallback_channel=SLACK_CHANNEL)


# ---------------------------------------------------------------------------
# Conversations.history + replies poller
# ---------------------------------------------------------------------------


def _safe_float(s):
    try:
        return float(s)
    except (ValueError, TypeError):
        return 0.0


def poll_conversations():
    """Background poller: channel messages + thread replies."""
    if not SLACK_TOKEN or not SLACK_CHANNEL or POLL_INTERVAL <= 0:
        return

    sys.stderr.write(f"[el-slack] Polling conversations.history every {POLL_INTERVAL}s\n")

    thread_watermarks = {}
    oldest = _read_watermark()

    while True:
        time.sleep(POLL_INTERVAL)
        try:
            # --- 1. Channel messages ---
            params = {"channel": SLACK_CHANNEL, "limit": "20"}
            if oldest and oldest != '0':
                params["oldest"] = oldest

            data = _slack_api("conversations.history", params)
            if not data:
                continue

            messages = data.get('messages', [])
            new_max_ts = oldest
            threads_to_check = []

            for raw_msg in messages:
                msg_ts = raw_msg.get('ts', '0')
                try:
                    if float(msg_ts) <= float(oldest):
                        continue
                except ValueError:
                    continue

                _ingest_message(raw_msg, fallback_channel=SLACK_CHANNEL)

                if msg_ts > new_max_ts:
                    new_max_ts = msg_ts

                if raw_msg.get('reply_count', 0) > 0:
                    threads_to_check.append(raw_msg.get('ts'))

            if new_max_ts > oldest:
                oldest = new_max_ts

            # --- 2. Thread replies ---
            all_threads = set(threads_to_check) | set(thread_watermarks.keys())

            # Discover active threads from DB
            assert _sidecar is not None
            try:
                with _sidecar['db_lock']:
                    conn = _sidecar['get_db']()
                    cutoff = time.time() - 86400
                    rows = conn.execute(
                        "SELECT DISTINCT thread_ts FROM events "
                        "WHERE source='slack' AND thread_ts IS NOT NULL "
                        "AND thread_ts != '' "
                        "AND CAST(thread_ts AS REAL) > ?",
                        (cutoff,)
                    ).fetchall()
                    for (tts,) in rows:
                        if tts:
                            all_threads.add(tts)
                    rows2 = conn.execute(
                        "SELECT ts FROM events "
                        "WHERE source='slack' "
                        "AND CAST(ts AS REAL) > ?",
                        (cutoff,)
                    ).fetchall()
                    for (ts_val,) in rows2:
                        if ts_val:
                            all_threads.add(ts_val)
                    conn.close()
            except Exception:
                pass

            for thread_ts in all_threads:
                if not thread_ts:
                    continue
                thread_oldest = thread_watermarks.get(thread_ts, thread_ts)

                reply_data = _slack_api("conversations.replies", {
                    "channel": SLACK_CHANNEL,
                    "ts": thread_ts,
                    "oldest": thread_oldest,
                    "limit": "50",
                })
                if not reply_data:
                    continue

                thread_max = thread_oldest
                for reply in reply_data.get('messages', []):
                    reply_ts = reply.get('ts', '0')
                    try:
                        if float(reply_ts) <= float(thread_oldest):
                            continue
                    except ValueError:
                        continue

                    if not reply.get('thread_ts'):
                        reply['thread_ts'] = thread_ts

                    _ingest_message(reply, fallback_channel=SLACK_CHANNEL)

                    if reply_ts > thread_max:
                        thread_max = reply_ts

                thread_watermarks[thread_ts] = thread_max

            # Expire old thread watermarks (>24h)
            now = time.time()
            expired = [t for t in thread_watermarks
                       if _safe_float(t) < now - 86400]
            for t in expired:
                del thread_watermarks[t]

        except Exception as e:
            sys.stderr.write(f"[el-slack] poll error: {e}\n")


# ---------------------------------------------------------------------------
# Watermark init (apply to sidecar DB)
# ---------------------------------------------------------------------------


def init():
    """Apply Slack watermark to skip old events on startup."""
    assert _sidecar is not None, "el-slack not registered"
    watermark = _read_watermark()
    if watermark and float(watermark) > 0:
        with _sidecar['db_lock']:
            conn = _sidecar['get_db']()
            conn.execute(
                "UPDATE events SET picked_up = 1 WHERE source = 'slack' AND ts <= ?",
                (watermark,),
            )
            conn.commit()
            conn.close()


# ---------------------------------------------------------------------------
# Watermark advance on drain
# ---------------------------------------------------------------------------


def on_events_picked(events):
    """Called by sidecar after events are drained. Advance Slack watermark."""
    max_ts = '0'
    for evt in events:
        if evt.get('source') == 'slack':
            ts = evt.get('ts', '0')
            if ts and ts > max_ts:
                max_ts = ts
    if max_ts != '0':
        _write_watermark(max_ts)


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------


def register(sidecar):
    """Register this Slack plugin with el-sidecar.

    sidecar is a dict providing:
        insert_event(source, **fields)  — insert an event with enrichment
        notify_waiters()                — wake up drain long-poll
        register_route(method, path, handler)  — register an HTTP route
        register_poller(name, func)     — register a background poller
        register_init(name, func)       — register a startup hook
        register_on_pick(name, func)    — register a drain callback
        get_db()                        — get a DB connection
        db_lock                         — threading lock for DB access
    """
    global _sidecar
    _sidecar = sidecar

    # Register webhook route
    sidecar['register_route']('POST', '/slack', handle_webhook)

    # Register poller
    if SLACK_TOKEN and SLACK_CHANNEL and POLL_INTERVAL > 0:
        sidecar['register_poller']('slack', poll_conversations)

    # Register init hook
    sidecar['register_init']('slack', init)

    # Register drain callback (advances watermark)
    sidecar['register_on_pick']('slack', on_events_picked)

    sys.stderr.write(f"[el-slack] Registered (channel={SLACK_CHANNEL}, "
                     f"bot_filter={MY_BOT_ID or '(none)'}, "
                     f"poll={POLL_INTERVAL}s)\n")
