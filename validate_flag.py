#!/usr/bin/env python3
"""Thin flag-validation client for an xbow-validator endpoint.

Single file, standard library only — share it with anyone who can reach the API.

Usage:
    ./validate_flag.py --api http://192.168.0.101:34493/validate XBEN-093-24 'FLAG{...}'
    ./validate_flag.py --api HOST:PORT XBEN-093-24 'FLAG{...}'
    ./validate_flag.py --api http://192.168.0.101:34493/validate --list
    XBOW_API_URL=http://192.168.0.101:34493/validate ./validate_flag.py XBEN-093-24 'FLAG{...}'

Exit codes: 0 = valid, 1 = invalid, 2 = usage / connection / API error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from urllib import error, parse, request

DEFAULT_TIMEOUT = 10.0


class APIError(Exception):
    """A connection or API-level failure (not a wrong flag)."""


def endpoint(raw: str) -> str:
    """Normalize a user-supplied API address to a full /validate URL."""
    raw = raw.strip()
    if "://" not in raw:
        raw = f"http://{raw}"
    parts = parse.urlsplit(raw)
    path = parts.path.rstrip("/")
    if not path.endswith("/validate"):
        path = f"{path}/validate"
    return parse.urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def call(url: str, payload: dict | None = None, token: str = "",
         timeout: float = DEFAULT_TIMEOUT) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = request.Request(
        url, data=data, headers=headers, method="POST" if data is not None else "GET"
    )

    try:
        with request.urlopen(req, timeout=timeout) as response:  # noqa: S310
            body = response.read().decode()
    except error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            detail = json.loads(body).get("error", body)
        except (json.JSONDecodeError, AttributeError):
            detail = body.strip() or e.reason
        raise APIError(f"HTTP {e.code}: {detail}") from None
    except OSError as e:
        raise APIError(f"cannot reach {url}: {e}") from None

    try:
        result = json.loads(body)
    except json.JSONDecodeError:
        raise APIError(f"API returned non-JSON: {body[:200]!r}") from None
    if not isinstance(result, dict):
        raise APIError("API returned an unexpected response")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate a captured flag against an xbow-validator API."
    )
    parser.add_argument(
        "--api", default=os.environ.get("XBOW_API_URL", ""),
        help="API URL or host:port (or set XBOW_API_URL)",
    )
    parser.add_argument(
        "--token", default=os.environ.get("XBOW_VALIDATOR_TOKEN", ""),
        help="Bearer token, if the API requires one (or XBOW_VALIDATOR_TOKEN)",
    )
    parser.add_argument("--list", action="store_true", help="List hosted benchmarks")
    parser.add_argument("--json", action="store_true", help="Print the raw JSON response")
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT, help="Request timeout in seconds"
    )
    parser.add_argument("benchmark", nargs="?", metavar="ID")
    parser.add_argument("flag", nargs="?", metavar="FLAG")
    args = parser.parse_args()

    if not args.api:
        parser.error("--api (or XBOW_API_URL) is required")

    url = endpoint(args.api)
    try:
        if args.list:
            result = call(url, token=args.token, timeout=args.timeout)
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                hosted = result.get("hosted") or []
                print(f"{len(hosted)} hosted: " + (", ".join(hosted) if hosted else "none"))
            return 0

        if not args.benchmark or args.flag is None:
            parser.error("benchmark and flag are required (or use --list)")

        result = call(
            url,
            {"benchmark_id": args.benchmark, "flag": args.flag},
            token=args.token,
            timeout=args.timeout,
        )
    except APIError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        benchmark_id = result.get("benchmark_id", args.benchmark)
        if result.get("valid") is True:
            print(f"VALID   {benchmark_id}")
        else:
            reason = result.get("reason")
            suffix = f" ({reason})" if reason else ""
            print(f"INVALID {benchmark_id}{suffix}")

    return 0 if result.get("valid") is True else 1


if __name__ == "__main__":
    sys.exit(main())
