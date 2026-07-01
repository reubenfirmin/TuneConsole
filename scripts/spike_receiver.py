#!/usr/bin/env python3
"""Minimal local receiver for the browser-extension spike.

Proves the extension can deliver live YouTube Music session data to a local backend. Listens on
127.0.0.1:8799, accepts POST /ingest, and prints a summary of what arrived (including a few real
playlist/item titles so you can see it's genuinely your signed-in data). It also answers the
Private Network Access (PNA) preflight, which is what lets a browser reach localhost at all.

Throwaway proof code: stdlib only, no framework. Run:  python scripts/spike_receiver.py
"""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

HOST, PORT = "127.0.0.1", 8799


def _titles(data):
    """Best-effort walk of a browse response, pulling out titled/navigable items so we can show
    that real data arrived without hard-coding YouTube's deeply nested renderer schema."""
    found, stack = [], [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            t = node.get("title")
            if isinstance(t, dict) and t.get("runs") and node.get("navigationEndpoint"):
                run0 = t["runs"][0]
                if isinstance(run0, dict) and run0.get("text"):
                    found.append(run0["text"])
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    seen, out = set(), []
    for x in found:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


class Handler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        # Lets the browser's Private Network Access check pass for a request targeting localhost.
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n)
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

        try:
            payload = json.loads(raw)
        except Exception as e:  # noqa: BLE001
            print(f"[receiver] got {len(raw)} bytes, not JSON ({e})")
            return
        titles = _titles(payload.get("data", {}))
        print(f"\n[receiver] delivered browseId={payload.get('browseId')} "
              f"status={payload.get('status')} bytes={len(raw)}")
        print(f"[receiver] {len(titles)} titled item(s) in the response:")
        for t in titles[:25]:
            print(f"    - {t}")
        if len(titles) > 25:
            print(f"    ... (+{len(titles) - 25} more)")

    def log_message(self, *_a):  # silence default request logging
        pass


if __name__ == "__main__":
    print(f"[receiver] listening on http://{HOST}:{PORT}  (POST /ingest)")
    print("[receiver] load the unpacked extension, then open/refresh music.youtube.com while signed in.")
    HTTPServer((HOST, PORT), Handler).serve_forever()
