"""Slack event listener for claude-code-event-listeners plugin.

Blocks until a real Slack message arrives, outputs JSON to stdout, exits.
Handles URL verification, bot self-filtering, watermark dedup, @mention
double-delivery, and message edit filtering.

Usage: python3 slack-listener.py [port] [bot-id]
"""
import json, os, sys
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9999
MY_BOT_ID = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else os.environ.get('SLACK_BOT_ID', '')
WATERMARK_FILE = os.environ.get('SLACK_WATERMARK_FILE', '/tmp/slack-webhook-watermark')
SEEN_IDS_FILE = os.environ.get('SLACK_SEEN_IDS_FILE', '/tmp/slack-webhook-seen-ids')
MAX_SEEN_IDS = 50


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        data = json.loads(body)

        # URL verification (Slack challenge/response handshake)
        if data.get('type') == 'url_verification':
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(data['challenge'].encode())
            return

        if data.get('type') != 'event_callback':
            self.send_response(200)
            self.end_headers()
            return

        event = data.get('event', {})

        # Only process message and app_mention events
        if event.get('type', '') not in ('message', 'app_mention'):
            self.send_response(200)
            self.end_headers()
            return

        # Skip own bot messages (if bot ID configured)
        if MY_BOT_ID:
            if event.get('bot_id') == MY_BOT_ID:
                self.send_response(200)
                self.end_headers()
                return
            if event.get('subtype') == 'bot_message' and event.get('bot_id') == MY_BOT_ID:
                self.send_response(200)
                self.end_headers()
                return

        # Skip message edits
        if event.get('subtype') == 'message_changed':
            self.send_response(200)
            self.end_headers()
            return

        # Watermark check — skip events older than last processed
        event_ts = event.get('event_ts', event.get('ts', '0'))
        try:
            with open(WATERMARK_FILE, 'r') as f:
                watermark = f.read().strip()
        except FileNotFoundError:
            watermark = '0'
        if float(event_ts) <= float(watermark):
            self.send_response(200)
            self.end_headers()
            return

        # Deduplicate @mention double-delivery
        # Slack sends both message and app_mention for the same user message
        client_msg_id = event.get('client_msg_id', '')
        if client_msg_id:
            try:
                with open(SEEN_IDS_FILE, 'r') as f:
                    seen = f.read().strip().split('\n')
            except FileNotFoundError:
                seen = []
            if client_msg_id in seen:
                self.send_response(200)
                self.end_headers()
                return
            seen.append(client_msg_id)
            with open(SEEN_IDS_FILE, 'w') as f:
                f.write('\n'.join(seen[-MAX_SEEN_IDS:]))

        # Real message — respond to Slack, advance watermark, output event
        self.send_response(200)
        self.end_headers()
        self.wfile.flush()

        if float(event_ts) > float(watermark):
            with open(WATERMARK_FILE, 'w') as f:
                f.write(event_ts)

        # Output clean event JSON to stdout (el protocol)
        output = {
            'user': event.get('user', ''),
            'text': event.get('text', ''),
            'ts': event.get('ts', ''),
            'channel': event.get('channel', ''),
            'type': event.get('type', ''),
        }
        if event.get('thread_ts'):
            output['thread_ts'] = event['thread_ts']
        if event.get('bot_id'):
            output['bot_id'] = event['bot_id']
        print(json.dumps(output), flush=True)
        os._exit(0)

    def log_message(self, *a):
        pass


HTTPServer(('0.0.0.0', PORT), Handler).serve_forever()
