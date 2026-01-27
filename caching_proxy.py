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

    def do_Get(self):
        cache_key = self.path

        # 1. Check cache
        if cache_key in cache:
            cached = cache[cache_key]
            self.send_response(cached["status"])
            




# if __name__ == "__main__":
#     args = parse_args()
#     print(args)