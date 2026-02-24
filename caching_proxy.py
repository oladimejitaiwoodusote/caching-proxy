import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import requests
import time

cache = {}

def parse_args():
    parser = argparse.ArgumentParser(description="Simple caching proxy")
    parser.add_argument("--port", type=int, help="Port to run the proxy on")
    parser.add_argument("--origin", type=str, help="Origin server URL")
    parser.add_argument("--clear-cache", action="store_true")
    parser.add_argument("--ttl", type=int, default=30, help="Cache TTL in seconds")
    return parser.parse_args()

HOP_BY_HOP = {
    "content-encoding",
    "transfer-encoding",
    "content-length",
    "connection",
    "keep-alive",
    "proxy-authentication",
    "proxy-authorization",
    "te",
    "trailer",
    "upgrade",
}

def fetch_from_origin(origin, path):
    url = origin + path
    try:
        return requests.get(url, timeout =5)
    except requests.RequestException as e:
        print(f"❌ Error contacting origin: {e}")
        return None

class ProxyHandler(BaseHTTPRequestHandler):
    origin = None

    def do_GET(self):
        cache_key = self.path
        print(f"➡ Incoming request: {self.path}")

        # 1. Check cache
        if cache_key in cache:
            cached = cache[cache_key]
            age = time.time() - cached["timestamp"]

            if age < self.ttl:
                print(f"🟢 Cache Hit: {cache_key}")
                self.send_response(cached["status"])

                for key, value in cached["headers"].items():
                    if key.lower() not in HOP_BY_HOP:
                        self.send_header(key, value)

                self.send_header("X-Cache", "HIT")
                self.end_headers()
                self.wfile.write(cached["body"])
                return
            else:
                print(f"⏰ Cache expired: {cache_key}")
                del cache[cache_key]

        #2. Forward request to origin
        print(f"🔴 Cache MISS: {cache_key}")
        response = fetch_from_origin(self.origin, self.path)
        
        if response is None:
            self.send_response(502)
            self.end_headers()
            self.wfile.write(b"Bad Gateway")
            return

        #3. Save in cache
        if response.status_code == 200:
            cache[cache_key] = {
                "status": response.status_code,
                "headers": dict(response.headers),
                "body": response.content,
                "timestamp": time.time()
            }

        #4. Return response
        self.send_response(response.status_code)
        for key, value in response.headers.items():
            if key.lower() not in HOP_BY_HOP:
                self.send_header(key, value)

        self.send_header("X-Cache", "MISS")
        self.end_headers()
        self.wfile.write(response.content)

    def do_POST(self):
        self.send_response(405)
        self.end_headers()
        self.wfile.write(b"Method Not Allowed")

    def do_PUT(self):
        self.send_response(405)
        self.end_headers()
        self.wfile.write(b"Method Not Allowed")

    def do_DELETE(self):
        self.send_response(405)
        self.end_headers()
        self.wfile.write(b"Method Not Allowed")

def run_server(port, origin, ttl):
    ProxyHandler.origin = origin.rstrip("/")
    ProxyHandler.ttl = ttl
    server = HTTPServer(("localhost", port), ProxyHandler)
    print(f"🚀 Caching proxy running on port {port}")
    print(f"➡️ Forwarding requests to {origin}")
    print(f"⏳ TTL set to {ttl} seconds")
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

    run_server(args.port, args.origin, args.ttl)