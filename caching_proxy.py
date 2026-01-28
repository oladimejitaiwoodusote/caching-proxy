import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import requests

cache = {}

def parse_args():
    parser = argparse.ArgumentParser(description="Simple caching proxy")
    parser.add_argument("--port", type=int, help="Port to run the proxy on")
    parser.add_argument("--origin", type=str, help="Origin server URL")
    parser.add_argument("--clear-cache", action="store_true")
    return parser.parse_args()

class ProxyHandler(BaseHTTPRequestHandler):
    origin = None

    def do_GET(self):
        cache_key = self.path

        # 1. Check cache
        if cache_key in cache:
            cached = cache[cache_key]
            self.send_response(cached["status"])

            for key, value in cached["headers"].items():
                self.send_header(key, value)

            self.send_header("X-Cache", "HIT")
            self.end_headers()
            self.wfile.write(cached["body"])
            return

        #2. Forward request to origin
        url = self.origin + self.path
        response = requests.get(url)

        #3. Save in cache
        cache[cache_key] = {
            "status": response.status_code,
            "headers": dict(response.headers),
            "body": response.content
        }

        #4. Return response
        self.send_response(response.status_code)
        for key, value in response.headers.items():
            self.send_header(key, value)

        self.send_header("X-Cache", "MISS")
        self.end_headers()
        self.wfile.write(response.content)

def run_server(port, origin):
    ProxyHandler.origin = origin
    server = HTTPServer(("localhost", port), ProxyHandler)
    print(f"🚀 Caching proxy running on port {port}")
    print(f"➡️ Forwarding requests to {origin}")
    server.serve_forever()

if __name__ == "__main__":
    args = parse_args()

    if args.clear_cache:
        cache.clear()
        print("🧹 Cache cleared")
        exit(0)

    if not args.port or not args.origin:
        print("❌ --port and --origin are required")
        exit(1)

    run_server(args.port, args.origin)