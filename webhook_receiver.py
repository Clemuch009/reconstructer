# save as webhook_receiver.py
from http.server import HTTPServer, BaseHTTPRequestHandler
import json

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length)
        print("\n--- WEBHOOK RECEIVED ---")
        try:
            data = json.loads(body)
            print(f"event_type : {data.get('event_type')}")
            print(f"source_id  : {data.get('source_id')}")
            print(f"version    : {data.get('version')}")
            print(f"timestamp  : {data.get('timestamp')}")
            segments = data.get("payload", {}).get("machine_readable", {}).get("segments", [])
            print(f"segments   : {len(segments)}")
        except Exception as e:
            print(f"raw: {body[:200]}")
        self.send_response(200)
        self.end_headers()

HTTPServer(("0.0.0.0", 9000), Handler).serve_forever()
