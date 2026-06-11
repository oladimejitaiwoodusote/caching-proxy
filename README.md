# HTTP Caching Proxy

A Python-based HTTP caching proxy that forwards request to an origin server while adding caching, persistence, concurrency control, and observability features.

## Features

### Core Proxy

- Forwards HTTP GET requests to an origin server
- Preserves response status codes and headers
- Adds `X-Cache: HIT | MISS` header