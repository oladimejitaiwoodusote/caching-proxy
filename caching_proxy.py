import logging
import json
import argparse
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import requests
import time
import threading
import os
import hashlib
from collections import OrderedDict
from datetime import datetime
from urllib.parse import urlparse, parse_qs
from requests.adapters import HTTPAdapter

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("proxy")

CACHE_DIR = ".cache"
os.makedirs(CACHE_DIR, exist_ok=True)

def key_to_filename(key):
    return hashlib.sha256(key.encode()).hexdigest() + ".json"

def save_to_disk(cache_key, entry):
    filename = key_to_filename(cache_key)
    path = os.path.join(CACHE_DIR, filename)

    with open(path, "w") as f:
        json.dump({
            "cache_key": cache_key,
            "status": entry["status"],
            "headers": entry["headers"],
            "body": entry["body"].decode("latin1"),
            "timestamp": entry["timestamp"]
        }, f)

    log_event("info", "disk_cache_saved", cache_key=cache_key)

def delete_disk_cache(cache_key):
    filename = key_to_filename(cache_key)
    path = os.path.join(CACHE_DIR, filename)

    if os.path.exists(path):
        os.remove(path)
        log_event("info", "disk_cache_deleted", cache_key=cache_key)

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

    "total_latency_ms": 0,
    "total_origin_latency_ms": 0
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

        self.session = requests.Session()

        adapter = HTTPAdapter(
            pool_connections=20,
            pool_maxsize=20
        )

        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

        self.load_cache_from_disk()

    def load_cache_from_disk(self):
        for file in os.listdir(CACHE_DIR):
            path = os.path.join(CACHE_DIR, file)

            try:
                with open(path, "r") as f:
                    data = json.load(f)

                    cache_key = data["cache_key"]

                    age = time.time() - data["timestamp"]
                    if age >= self.ttl:
                        delete_disk_cache(cache_key)
                        continue

                    with cache_lock:
                        cache[cache_key] = {
                            "status": data["status"],
                            "headers": data["headers"],
                            "body": data["body"].encode("latin1"),
                            "timestamp": data["timestamp"]
                        }

                    log_event("info", "disk_cache_loaded", cache_key=cache_key)

            except Exception as e:
                log_event("error", "disk_cache_load_failed", error=str(e))

    def get_metrics(self):
        with metrics_lock:
            total = metrics["requests"]
            hits = metrics["cache_hits"]
            hit_ratio = (hits / total) if total > 0 else 0
            avg_latency = (
                metrics["total_latency_ms"] / total
                if total > 0 else 0
            )

            avg_origin_latency = (
                metrics["total_origin_latency_ms"] /
                metrics["origin_requests"]
                if metrics["origin_requests"] > 0
                else 0
            )

            return {
                "requests": total,
                "cache_hits": hits,
                "cache_misses": metrics["cache_misses"],
                "origin_requests": metrics["origin_requests"],
                "evictions": metrics["evictions"],
                "hit_ratio": round(hit_ratio, 3),

                "avg_latency_ms": round(avg_latency, 2),
                "avg_origin_latency_ms": round(avg_origin_latency, 2)
            }
            
    def get_cache_stats(self):
        with cache_lock:
            return {
                "entries": len(cache),
                "max_entries": MAX_CACHE_ITEMS,
                "keys": list(cache.keys())
            }
    
    def clear_cache(self):
        with cache_lock:
            keys = list(cache.keys())

            cache.clear()

            for key in keys:
                delete_disk_cache(key)

        log_event("info", "cache_cleared_admin")

    def invalidate_cache_key(self, cache_key):
        with cache_lock:
            if cache_key in cache:
                del cache[cache_key]

                delete_disk_cache(cache_key)

                log_event(
                    "info",
                    "cache_key_invalidated",
                    cache_key=cache_key
                )

                return True

        return False

    def fetch_from_origin(self, path):
        url = self.origin + path

        try:
            return self.session.get(url, timeout=5)

        except requests.RequestException as e:
            log_event("error", "origin_error", error=str(e))
            return None

    
    def handle_request(self, method, path):
        request_start = time.perf_counter()

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

                    self.record_request_latency(request_start)

                    return {
                        "status": cached["status"],
                        "headers": cached["headers"],
                        "body": cached["body"],
                        "cache": "HIT"
                    }
                else:
                    log_event("info", "cache_expired", cache_key=cache_key)
                    del cache[cache_key]
                    delete_disk_cache(cache_key)

        #2. Request coalescing
        is_first = False

        with cache_lock:
            if cache_key in in_flight_requests:
                event = in_flight_requests[cache_key]
                log_event("info", "coalesced_wait", cache_key=cache_key)
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

                    self.record_request_latency(request_start)
                    return {
                        "status": cached["status"],
                        "headers": cached["headers"],
                        "body": cached["body"],
                        "cache": "HIT"
                    }

        #3. Fetch from origin
        with metrics_lock:
            metrics["origin_requests"] += 1

        origin_start = time.perf_counter()
        
        response = self.fetch_from_origin(path)

        origin_latency = (
            time.perf_counter() - origin_start
        ) * 1000

        with metrics_lock:
            metrics["total_origin_latency_ms"] += origin_latency

        if response is None:
            log_event("error", "origin_fetch_failed", cache_key=cache_key)

            with cache_lock:
                event = in_flight_requests.pop(cache_key, None)
                if event:
                    event.set()
            
            self.record_request_latency(request_start)
            
            return {
                "status": 502,
                "headers": {},
                "body": b'Bad Gateway',
                "cache": "MISS"
            }

        log_event("info", "origin_fetch", cache_key=cache_key, status=response.status_code)

        #4. Save in cache
        if response.status_code == 200:
            with cache_lock:
                if len(cache) >= MAX_CACHE_ITEMS:
                    with metrics_lock:
                        metrics["evictions"] += 1

                    evicted_key, _ = cache.popitem(last = False)
                    delete_disk_cache(evicted_key)
                    log_event("info", "cache_eviction", evicted_key=evicted_key)       

                cache[cache_key] = {
                    "status": response.status_code,
                    "headers": dict(response.headers),
                    "body": response.content,
                    "timestamp": time.time()
                }

                save_to_disk(cache_key, cache[cache_key])

                cache.move_to_end(cache_key)

        # release waiting requests
        with cache_lock:
            if cache_key in in_flight_requests:
                event = in_flight_requests.pop(cache_key)
                event.set()

        self.record_request_latency(request_start)

        return {
            "status": response.status_code,
            "headers": dict(response.headers),
            "body": response.content,
            "cache": "MISS"
        }

    def record_request_latency(self, request_start):
        latency_ms = (
            time.perf_counter() - request_start
        ) * 1000

        with metrics_lock:
            metrics["total_latency_ms"] += latency_ms

class ProxyHandler(BaseHTTPRequestHandler):
    proxy = None

    def do_GET(self):
        if self.path == "/metrics":
            data = self.proxy.get_metrics()

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()

            self.wfile.write(json.dumps(data).encode())
            return

        if self.path == "/cache":
            data = self.proxy.get_cache_stats()

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()

            self.wfile.write(json.dumps(data).encode())
            return

        if self.path == "/clear-cache":
            self.proxy.clear_cache()

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()

            self.wfile.write(
                json.dumps({"message": "Cache cleared"}).encode()
            )
            return

        if self.path.startswith("/invalidate"):
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)

            cache_key = params.get("key", [None])[0]

            if not cache_key:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Missing key parameter")
                return

            removed = self.proxy.invalidate_cache_key(cache_key)

            self.send_response(200 if removed else 404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()

            self.wfile.write(
                json.dumps({
                    "removed": removed,
                    "cache_key": cache_key
                }).encode()
            )
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
    server.daemon_threads = True

    log_event("info", "server_started", port=port, origin=origin, ttl=ttl)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log_event("info", "shutdown_signal_received")
    finally:
        proxy.session.close()

        server.shutdown()
        server.server_close()
        
        log_event("info", "server_stopped")

if __name__ == "__main__":
    args = parse_args()

    if args.clear_cache:
        cache.clear()

        for file in os.listdir(CACHE_DIR):
            path = os.path.join(CACHE_DIR, file)

            if os.path.isfile(path):
                os.remove(path)

        log_event("info", "cache_cleared")
        exit(0)

    if not args.port or not args.origin:
        log_event("error", "missing_required_args")
        exit(1)

    run_server(args.port, args.origin, args.ttl)