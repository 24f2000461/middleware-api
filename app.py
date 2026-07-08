"""
Middleware Stack: Rate-Limit + CORS + Request Context

Endpoint: GET /ping -> {"email": "...", "request_id": "..."}

Layers (outer -> inner):
  1. CORS        - only the assigned allowed origin (+ the exam page origin
                    for verification) ever receives Access-Control-Allow-Origin.
                    No wildcard.
  2. Rate limit  - per X-Client-Id sliding window, bucket size B.
  3. Request ctx - reuses inbound X-Request-ID or generates a uuid4; always
                    echoed in the response body and X-Request-ID header.
"""

import threading
import time
import uuid
from collections import deque

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# Assigned values
# ---------------------------------------------------------------------------
EMAIL = "24f2000461@ds.study.iitm.ac.in"
ALLOWED_ORIGIN = "https://app-1a015h.example.com"
EXAM_PAGE_ORIGIN = "https://exam.sanand.workers.dev"  # so the grader page can verify
RATE_LIMIT_B = 13
RATE_LIMIT_WINDOW_SECONDS = 10

app = FastAPI(title="Middleware Stack Demo")

# ---------------------------------------------------------------------------
# Middleware 3 (innermost, defined first): per-client rate limiting
# ---------------------------------------------------------------------------
_rate_lock = threading.Lock()
_rate_buckets: dict[str, deque] = {}


def check_rate_limit(client_id: str):
    now = time.monotonic()
    with _rate_lock:
        bucket = _rate_buckets.setdefault(client_id, deque())
        while bucket and now - bucket[0] >= RATE_LIMIT_WINDOW_SECONDS:
            bucket.popleft()

        if len(bucket) >= RATE_LIMIT_B:
            oldest = bucket[0]
            retry_after = max(1, int(RATE_LIMIT_WINDOW_SECONDS - (now - oldest)) + 1)
            return False, retry_after

        bucket.append(now)
        return True, 0


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    if request.method == "OPTIONS":
        return await call_next(request)

    client_id = request.headers.get("X-Client-Id", "anonymous")
    allowed, retry_after = check_rate_limit(client_id)

    if not allowed:
        return JSONResponse(
            status_code=429,
            content={"detail": "Rate limit exceeded. Try again later."},
            headers={"Retry-After": str(retry_after)},
        )

    return await call_next(request)


# ---------------------------------------------------------------------------
# Middleware 2 (added after rate-limit, so it ends up wrapping it -> CORS
# headers appear even on 429 responses; CORS also short-circuits OPTIONS
# preflight before it ever reaches the rate limiter).
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=[ALLOWED_ORIGIN, EXAM_PAGE_ORIGIN],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)


# ---------------------------------------------------------------------------
# Middleware 1 (outermost, added last): request-context / request-id
# propagation. Being outermost means it wraps CORS + rate limiting so every
# response -- including 429s and CORS-rejected ones -- gets an
# X-Request-ID header.
# ---------------------------------------------------------------------------
@app.middleware("http")
async def request_context_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    request.state.request_id = request_id

    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------
@app.get("/ping")
async def ping(request: Request):
    request_id = getattr(request.state, "request_id", None) or str(uuid.uuid4())
    return {"email": EMAIL, "request_id": request_id}


@app.get("/")
@app.head("/")
async def root():
    return {
        "status": "ok",
        "endpoint": "GET /ping",
        "allowed_origin": ALLOWED_ORIGIN,
        "rate_limit": f"{RATE_LIMIT_B} requests / {RATE_LIMIT_WINDOW_SECONDS}s",
    }
