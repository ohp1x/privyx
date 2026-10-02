"""Stand-in for an OpenAI-compatible provider, for proxy.tape.

Appends each user message it receives to provider.log, then replies by echoing
that message back, so the proxy has tokens to restore on the way out.
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        text = body["messages"][-1]["content"]
        with open("provider.log", "a") as log:
            log.write(text + "\n")
        reply = {
            "id": "chatcmpl-stub",
            "object": "chat.completion",
            "created": 0,
            "model": body["model"],
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": f"Done: {text}"},
                    "finish_reason": "stop",
                }
            ],
        }
        data = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
