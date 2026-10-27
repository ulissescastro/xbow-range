#!/usr/bin/env python3
"""xbow-validator — a tiny, single-endpoint flag-validation API.

Runs as an independent sidecar container managed by ``xbow``. It reads the
hosted-target view that ``xbow`` publishes to ``$XBOW_STATE_DIR/state.json``
(``.xbow/api/state.json`` on the host) and answers one question:

    Is this flag correct for a benchmark that is up right now?

One endpoint, ``/validate``:

    POST /validate   {"benchmark_id": "XBEN-093-24", "flag": "FLAG{...}"}
    GET  /validate?benchmark_id=XBEN-093-24&flag=FLAG%7B...%7D
    GET  /validate                     -> {"hosted": [...], "count": N}

The expected flag is never returned — only a boolean plus a reason. Flags are
compared in constant time and are never written to the logs.

Configuration (environment):
    PORT                     TCP port to listen on (default 8800)
    XBOW_STATE_DIR           directory containing state.json (default /state)
    XBOW_VALIDATOR_TOKEN     optional bearer token; loopback is always exempt
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import signal
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

STATE_DIR = os.environ.get("XBOW_STATE_DIR", "/state")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
PORT = int(os.environ.get("PORT", "8800"))
TOKEN = os.environ.get("XBOW_VALIDATOR_TOKEN", "")

BENCHMARK_RE = re.compile(r"^XBEN-\d{3}-\d{2}$")
MAX_BODY = 4096
MAX_FLAG = 1024
PROBE_TIMEOUT = 0.5
PROBE_TTL = 1.0

RATE_LIMIT = 30          # requests
RATE_WINDOW = 60.0       # seconds

_state_lock = threading.Lock()
_state_cache: dict[str, object] = {"key": None, "data": {}}

_probe_lock = threading.Lock()
_probe_cache: dict[str, tuple[float, bool]] = {}

_rate_lock = threading.Lock()
_rate: dict[str, tuple[float, float]] = {}   # ip -> (tokens, last_seen)


# =============================================================================
# Hosted-target view
# =============================================================================


def load_state() -> dict:
    """Read the xbow-published view, re-reading only when the file changes.

    xbow writes it atomically (tmp file + rename), so a reader either sees the
    old or the new document. A short retry covers the tiny window on filesystems
    where the rename is not immediately visible.
    """
    try:
        st = os.stat(STATE_FILE)
    except OSError:
        return {}

    key = (st.st_mtime_ns, st.st_size)
    with _state_lock:
        if _state_cache["key"] == key:
            return _state_cache["data"]  # type: ignore[return-value]

    data = None
    for _ in range(3):
        try:
            with open(STATE_FILE) as f:
                data = json.load(f)
            break
        except (json.JSONDecodeError, OSError):
            time.sleep(0.02)

    if data is None:
        with _state_lock:
            return _state_cache["data"]  # type: ignore[return-value]

    if not isinstance(data, dict):
        data = {}

    with _state_lock:
        _state_cache["key"] = key
        _state_cache["data"] = data
    return data


def expected_flag(benchmark_id: str, entry: dict) -> str:
    flag = entry.get("flag")
    if isinstance(flag, str) and flag:
        return flag
    digest = hashlib.sha256(benchmark_id.upper().encode()).hexdigest()
    return f"FLAG{{{digest}}}"


# =============================================================================
# Liveness probe
# =============================================================================


def _probe_targets(entry: dict) -> list[tuple[str, int]]:
    """Host/port pairs to try, in order (loopback first — we use host network)."""
    targets: list[tuple[str, int]] = []
    for pub in entry.get("published") or []:
        try:
            targets.append(("127.0.0.1", int(pub["host_port"])))
        except (KeyError, TypeError, ValueError):
            continue
    for url in entry.get("urls") or []:
        try:
            parts = urlsplit(str(url))
            if not parts.hostname:
                continue
            port = parts.port or (443 if parts.scheme == "https" else 80)
            targets.append((parts.hostname, port))
        except (TypeError, ValueError):
            continue

    seen: set[tuple[str, int]] = set()
    unique = []
    for target in targets:
        if target not in seen:
            seen.add(target)
            unique.append(target)
    return unique


def target_running(benchmark_id: str, entry: dict) -> bool:
    """True if the target's published port accepts a TCP connection.

    Cached for PROBE_TTL so a dead target costs at most one timeout per second
    while repeated validation requests stay fast.
    """
    now = time.monotonic()
    with _probe_lock:
        cached = _probe_cache.get(benchmark_id)
        if cached and now - cached[0] < PROBE_TTL:
            return cached[1]

    alive = False
    for host, port in _probe_targets(entry)[:2]:
        try:
            with socket.create_connection((host, port), PROBE_TIMEOUT):
                alive = True
                break
        except OSError:
            continue

    with _probe_lock:
        _probe_cache[benchmark_id] = (now, alive)
    return alive


# =============================================================================
# Validation
# =============================================================================


def validate(benchmark_id: str, flag: str) -> dict:
    benchmark_id = benchmark_id.strip().upper()
    result: dict[str, object] = {"benchmark_id": benchmark_id, "valid": False}

    if not BENCHMARK_RE.match(benchmark_id):
        result["reason"] = "unknown_benchmark"
        return result

    entry = load_state().get(benchmark_id)
    if not isinstance(entry, dict):
        result["reason"] = "not_hosted"
        return result

    if not target_running(benchmark_id, entry):
        result["reason"] = "not_running"
        return result

    candidate = flag.strip()
    if len(candidate) > MAX_FLAG:
        return result
    result["valid"] = hmac.compare_digest(candidate, expected_flag(benchmark_id, entry))
    return result


def hosted() -> dict:
    state = load_state()
    return {"hosted": sorted(state), "count": len(state)}


# =============================================================================
# Simple per-IP rate limiting
# =============================================================================


def _is_loopback(ip: str) -> bool:
    return ip in ("127.0.0.1", "::1") or ip.startswith("127.")


def rate_allowed(ip: str) -> bool:
    if _is_loopback(ip):
        return True
    now = time.monotonic()
    with _rate_lock:
        tokens, last = _rate.get(ip, (float(RATE_LIMIT), now))
        tokens = min(float(RATE_LIMIT), tokens + (now - last) * (RATE_LIMIT / RATE_WINDOW))
        allowed = tokens >= 1.0
        if allowed:
            tokens -= 1.0
        _rate[ip] = (tokens, now)

        if len(_rate) > 4096:
            cutoff = now - RATE_WINDOW
            for key in [k for k, v in _rate.items() if v[1] < cutoff]:
                del _rate[key]
    return allowed


# =============================================================================
# HTTP handler
# =============================================================================


class Handler(BaseHTTPRequestHandler):
    server_version = "xbow-validator/1.0"
    protocol_version = "HTTP/1.1"

    # -- logging: never include the query string (it may contain a flag) ------

    def log_request(self, code: object = "-", size: object = "-") -> None:
        path = urlsplit(self.path).path
        sys.stderr.write(
            f'{self.log_date_time_string()} {self.client_address[0]} '
            f'"{self.command} {path}" {code}\n'
        )

    def log_error(self, format: str, *args: object) -> None:
        sys.stderr.write(f"{self.log_date_time_string()} {format % args}\n")

    # -- helpers --------------------------------------------------------------

    def _send(self, code: int, payload: dict, extra: dict[str, str] | None = None) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _authorized(self) -> bool:
        if not TOKEN or _is_loopback(self.client_address[0]):
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            return hmac.compare_digest(header[7:].strip(), TOKEN)
        return False

    def _json_body(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length <= 0 or length > MAX_BODY:
            return None
        try:
            data = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, OSError):
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _first(params: dict[str, list[str]], *names: str) -> str | None:
        for name in names:
            values = params.get(name)
            if values and values[0] != "":
                return values[0]
        return None

    # -- routes ---------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        if urlsplit(self.path).path != "/validate":
            self._send(404, {"error": "not_found"})
            return
        if not rate_allowed(self.client_address[0]):
            self._send(429, {"error": "rate_limited"}, {"Retry-After": "60"})
            return
        if not self._authorized():
            self._send(401, {"error": "unauthorized"})
            return

        params = parse_qs(urlsplit(self.path).query)
        benchmark_id = self._first(params, "benchmark_id", "benchmark", "id")
        flag = self._first(params, "flag")

        if benchmark_id is None and flag is None:
            self._send(200, hosted())
            return
        if benchmark_id is None or flag is None:
            self._send(400, {"error": "benchmark_id and flag are required"})
            return
        self._send(200, validate(benchmark_id, flag))

    def do_POST(self) -> None:  # noqa: N802
        if urlsplit(self.path).path != "/validate":
            self._send(404, {"error": "not_found"})
            return
        if not rate_allowed(self.client_address[0]):
            self._send(429, {"error": "rate_limited"}, {"Retry-After": "60"})
            return
        if not self._authorized():
            self._send(401, {"error": "unauthorized"})
            return

        payload = self._json_body()
        if payload is None:
            self._send(400, {"error": "expected a JSON object"})
            return

        benchmark_id = payload.get("benchmark_id") or payload.get("benchmark") or payload.get("id")
        flag = payload.get("flag")
        if not isinstance(benchmark_id, str) or not isinstance(flag, str):
            self._send(400, {"error": "benchmark_id and flag are required"})
            return
        self._send(200, validate(benchmark_id, flag))

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Allow", "GET, POST, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_HEAD = do_GET


def main() -> None:
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    httpd.daemon_threads = True

    def _terminate(_signum: int, _frame: object) -> None:
        # shutdown() must not run in the thread executing serve_forever().
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGINT, _terminate)

    print(
        f"xbow-validator listening on 0.0.0.0:{PORT} (state={STATE_FILE}, "
        f"auth={'on' if TOKEN else 'off'})",
        flush=True,
    )
    try:
        httpd.serve_forever(poll_interval=0.2)
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
