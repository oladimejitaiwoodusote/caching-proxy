# HTTP Caching Proxy

A Python-based HTTP caching proxy that forwards request to an origin server while adding caching, persistence, concurrency control, and observability features.

## Features

### Core Proxy

- Forwards HTTP GET requests to an origin server
- Preserves response status codes and headers
- Adds `X-Cache: HIT | MISS` header

### Caching System

- In-memory LRU cache(`OrderedDict`)
- TTL-based expiration
- Configurable cache size
- Cache key format: `METHOD:PATH`

### Persistance

- Disk-based cache using `.cache/` directory
- Survives server restarts
- JSON-based serialization
- Automatic cleanup of expired entries

### Concurrency

- Thread-safe caching using locks
- Request coalescing (prevents duplicate origin calls)
- Built on `ThreadingHTTPServer`

### Performance Optimizations

- Connection pooling via `requests.Session`
- Retry strategy with exponential backoff (urlib3)

### Observability

- Structured JSON logs for :
    - cache hits/misses
    - origin fetches
    - evictions
    - coalesced requests
- Metrics endpoint:
    `GET /metrics`

### Admin Endpoints

- `GET/ cache` → view cache state
- `GET/ clear-cache` → clear cache
- `GET/ invalidate?key=...` → invalidate entry

## How It Works

### Request Flow

1. Request arrives
2. Check in-memory cache
3. If hit -> return immediately 
4. If miss:
    - Check request coalescing