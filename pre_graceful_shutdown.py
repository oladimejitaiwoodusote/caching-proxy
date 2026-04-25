import logging
import json
import argparse
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import requests
import time
import threading
from collections import OrderedDict
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("proxy")

def log_event(level, event, **kwargs):
    log = {
        "event": event,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        **kwargs
    }

    getattr(logger, level)(json.dumps(log))

metrics = {
    "requests": 0,
    "cache_hits": 0,
    "cache_misses": 0,
    "origin_requests": 0,
    "evictions": 0,
}

metrics_lock = threading.Lock()

# cache = {}
cache = OrderedDict()
MAX_CACHE_ITEMS = 100

cache_lock = threading.Lock() #✅ Thread-safe lock
in_flight_requests = {}

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
        
class ProxyServer:
    def __init__(self, origin, ttl):
        self.origin = origin
        self.ttl = ttl

    def get_metrics(self):
        with metrics_lock:
            total = metrics["requests"]
            hits = metrics["cache_hits"]
            hit_ratio = (hits / total) if total > 0 else 0

            return {
                "requests": total,
                "cache_hits": hits,
                "cache_misses": metrics["cache_misses"],
                "origin_requests": metrics["origin_requests"],
                "evictions": metrics["evictions"],
                "hit_ratio": round(hit_ratio, 3)
            }
            

    def fetch_from_origin(self, path):
        url = self.origin + path
        try:
            return requests.get(url, timeout=5)
        except requests.RequestException as e:
            log_event("error", "origin_error", error=str(e))
            return None

    def handle_request(self, method, path):
        with metrics_lock:
            metrics["requests"] += 1

        cache_key = f"{method}:{path}"
        log_event("info", "request_received", cache_key=cache_key)

        #1. Check cache
        with cache_lock:
            cached = cache.get(cache_key)
            if cached:
                cache.move_to_end(cache_key)

                age = time.time() - cached["timestamp"]

                if age < self.ttl:
                    with metrics_lock:
                        metrics["cache_hits"] += 1

                    log_event("info", "cache_hit", cache_key=cache_key)
                    return {
                        "status": cached["status"],
                        "headers": cached["headers"],
                        "body": cached["body"],
                        "cache": "HIT"
                    }
                else:
                    log_event("info", "cache_expired", cache_key=cache_key)
                    del cache[cache_key]

        #2. Request coalescing
        is_first = False

        with cache_lock:
            if cache_key in in_flight_requests:
                event = in_flight_requests[cache_key]
                log_event("info", "coalesced_wait", cache_key=cache_key)
                #Think this one is wrong
            else:
                event = threading.Event()
                in_flight_requests[cache_key] = event
                event.clear()
                is_first = True
        
        if is_first:
            log_event("info", "cache_miss", cache_key=cache_key)
            with metrics_lock:
                metrics["cache_misses"] += 1

        if not is_first:            
            event.wait()
            with cache_lock:
                cached = cache.get(cache_key)
                if cached:
                    with metrics_lock:
                        metrics["cache_hits"] += 1
                    cache.move_to_end(cache_key)
                    log_event("info", "coalesced_hit", cache_key=cache_key)
                    return {
                        "status": cached["status"],
                        "headers": cached["headers"],
                        "body": cached["body"],
                        "cache": "HIT"
                    }

        #3. Fetch from origin
        with metrics_lock:
            metrics["origin_requests"] += 1

        response = self.fetch_from_origin(path)

        if response is None:
            with cache_lock:
                event = in_flight_requests.pop(cache_key, None)
                if event:
                    event.set()
            
            return {
                "status": 502,
                "headers": {},
                "body": b'Bad Gateway',
                "cache": "MISS"
            }

        #4. Save in cache
        if response.status_code == 200:
            with cache_lock:
                if len(cache) >= MAX_CACHE_ITEMS:
                    with metrics_lock:
                        metrics["evictions"] += 1

                    evicted_key, _ = cache.popitem(last = False)
                    log_event("info", "cache_eviction", evicted_key=evicted_key)                
                cache[cache_key] = {
                    "status": response.status_code,
                    "headers": dict(response.headers),
                    "body": response.content,
                    "timestamp": time.time()
                }

                cache.move_to_end(cache_key)

        # release waiting requests
        with cache_lock:
            if cache_key in in_flight_requests:
                event = in_flight_requests.pop(cache_key)
                event.set()

        return {
            "status": response.status_code,
            "headers": dict(response.headers),
            "body": response.content,
            "cache": "MISS"
        }


class ProxyHandler(BaseHTTPRequestHandler):
    proxy = None

    def do_GET(self):
        if self.path == "/metrics":
            data = self.proxy.get_metrics()

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()

            import json
            self.wfile.write(json.dumps(data).encode())
            return

        result = self.proxy.handle_request(self.command, self.path)

        self.send_response(result["status"])

        for key,value in result["headers"].items():
            if key.lower() not in HOP_BY_HOP:
                self.send_header(key, value)

        self.send_header("X-Cache", result["cache"])
        self.end_headers()
        self.wfile.write(result["body"])

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
    proxy = ProxyServer(origin.rstrip("/"), ttl)
    ProxyHandler.proxy = proxy
    server = ThreadingHTTPServer(("localhost", port), ProxyHandler)
    # print(f"🚀 Caching proxy running on port {port}")
    # print(f"➡️ Forwarding requests to {origin}")
    # print(f"⏳ TTL set to {ttl} seconds")
    log_event("info", "server_started", port=port, origin=origin, ttl=ttl)
    server.serve_forever()

if __name__ == "__main__":
    args = parse_args()

    if args.clear_cache:
        cache.clear()
        log_event("info", "cache_cleared")
        exit(0)

    if not args.port or not args.origin:
        log_event("error", "missing_required_args")
        exit(1)

    run_server(args.port, args.origin, args.ttl)