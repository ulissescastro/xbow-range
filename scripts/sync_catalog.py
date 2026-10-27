#!/usr/bin/env python3
"""Regenerate the local benchmark catalog from the forked benchmark repo.

This is a maintainer tool, not part of xbow's runtime. It uses the
authenticated `gh` CLI to read `benchmarks/*/benchmark.json` from the pinned
fork snapshot and writes one consolidated, display-ready catalog:

    catalog/benchmarks.json   # source metadata + 104 benchmark entries
    catalog/LICENSE           # Apache-2.0, copied verbatim
    catalog/NOTICE            # upstream attribution, copied verbatim
    catalog/SOURCE.md         # human-readable provenance

Usage:
    python3 scripts/sync_catalog.py
    python3 scripts/sync_catalog.py --check
    python3 scripts/sync_catalog.py --ref main --repo owner/repo
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import json
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPO = "ulissescastro/validation-benchmarks"
DEFAULT_REF = "xbow-snapshot-2026-10-27"
DEFAULT_EXPECT = 104

BENCHMARK_PATH = re.compile(r"^benchmarks/(XBEN-\d{3}-\d{2})/benchmark\.json$")

# GitHub API is authenticated via gh, so these are well within the 5,000/hour
# limit. A little parallelism keeps a full refresh down to a few seconds.
DEFAULT_JOBS = 8
RETRIES = 3


class SyncError(Exception):
    pass


def gh(endpoint: str, *extra: str, retries: int = RETRIES) -> str:
    """Run `gh api ENDPOINT [--jq FILTER]` and return stdout."""
    cmd = ["gh", "api", endpoint, *extra]
    last_error = ""
    for attempt in range(retries):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)
        except FileNotFoundError:
            raise SyncError("the 'gh' CLI is not installed or not on PATH") from None
        if proc.returncode == 0:
            return proc.stdout
        last_error = (proc.stderr or proc.stdout).strip()
        if attempt < retries - 1:
            time.sleep(1.5 * (attempt + 1))
    raise SyncError(f"gh api {endpoint} failed: {last_error}")


def fetch_blob(repo: str, sha: str) -> dict[str, Any]:
    content = gh(f"repos/{repo}/git/blobs/{sha}", "--jq", ".content")
    try:
        raw = base64.b64decode(content)
    except (ValueError, TypeError) as e:
        raise SyncError(f"cannot decode blob {sha}: {e}") from None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise SyncError(f"invalid JSON in blob {sha}: {e}") from None


def fetch_text_file(repo: str, path: str) -> str:
    content = gh(f"repos/{repo}/contents/{path}", "--jq", ".content")
    return base64.b64decode(content).decode("utf-8")


def parse_entry(benchmark_id: str, meta: dict[str, Any], path: str) -> dict[str, Any]:
    name = str(meta.get("name") or "").strip()
    description = str(meta.get("description") or "").strip()
    if not name:
        raise SyncError(f"{path}: missing 'name'")
    if not description:
        raise SyncError(f"{path}: missing 'description'")

    try:
        level = int(meta.get("level"))
    except (TypeError, ValueError):
        raise SyncError(f"{path}: 'level' is not an integer: {meta.get('level')!r}") from None

    tags = meta.get("tags")
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise SyncError(f"{path}: 'tags' must be a list of strings")

    win_condition = str(meta.get("win_condition") or "flag")

    # `canaries` is intentionally dropped: it is not needed for display and
    # keeps the catalog focused. The upstream NOTICE is preserved separately.
    return {
        "id": benchmark_id,
        "name": name,
        "description": description,
        "level": level,
        "tags": tags,
        "win_condition": win_condition,
    }


def sort_key(entry: dict[str, Any]) -> tuple[int, int]:
    match = re.match(r"^XBEN-(\d+)-(\d+)$", entry["id"])
    if not match:
        return (999999, 999999)
    return (int(match.group(1)), int(match.group(2)))


def generate(
    repo: str, ref: str, expect: int, jobs: int
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    commit = gh(f"repos/{repo}/commits/{ref}", "--jq", ".sha").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise SyncError(f"could not resolve {repo}@{ref} to a commit (got {commit!r})")

    tree = json.loads(gh(f"repos/{repo}/git/trees/{commit}?recursive=1"))
    if tree.get("truncated"):
        raise SyncError("git tree response was truncated; cannot enumerate safely")

    paths: dict[str, str] = {}
    for item in tree.get("tree", []):
        if item.get("type") != "blob":
            continue
        match = BENCHMARK_PATH.match(item.get("path", ""))
        if match:
            paths[match.group(1)] = item["sha"]

    if not paths:
        raise SyncError(f"no benchmarks/*/benchmark.json found in {repo}@{ref}")
    if expect and len(paths) != expect:
        raise SyncError(
            f"expected {expect} benchmarks, found {len(paths)} "
            "(pass --expect 0 to disable this check)"
        )

    def load(item: tuple[str, str]) -> dict[str, Any]:
        benchmark_id, blob_sha = item
        meta = fetch_blob(repo, blob_sha)
        return parse_entry(benchmark_id, meta, f"benchmarks/{benchmark_id}/benchmark.json")

    items = sorted(paths.items())
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        entries = list(pool.map(load, items))
    entries.sort(key=sort_key)

    payload = {
        "version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "source": {
            "repo": repo,
            "url": f"https://github.com/{repo}",
            "ref": ref,
            "commit": commit,
            "license": "Apache-2.0",
        },
        "benchmarks": entries,
    }
    return payload, entries


def write_catalog(
    repo: str, ref: str, payload: dict[str, Any], out_dir: Path
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "benchmarks.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    )

    for name in ("LICENSE", "NOTICE"):
        try:
            text = fetch_text_file(repo, name)
        except SyncError as e:
            raise SyncError(f"cannot fetch upstream {name}: {e}") from None
        (out_dir / name).write_text(text)

    source = payload["source"]
    source_md = f"""# Benchmark catalog source

- Repository: {source["url"]}
- Ref: `{source["ref"]}`
- Commit: `{source["commit"]}`
- Generated: {payload["generated_at"]}
- Benchmarks: {len(payload["benchmarks"])}
- License: {source["license"]}

`benchmarks.json` is derived from `benchmarks/*/benchmark.json` in the pinned
fork snapshot above. The `canaries` field is intentionally omitted; all other
display fields are preserved (whitespace trimmed). `LICENSE` and `NOTICE` are
copied verbatim from the upstream project and must be kept with the catalog.

Regenerate with:

```bash
python3 scripts/sync_catalog.py
```
"""
    (out_dir / "SOURCE.md").write_text(source_md)


def check_catalog(payload: dict[str, Any], out_dir: Path) -> int:
    path = out_dir / "benchmarks.json"
    if not path.exists():
        print(f"error: {path} does not exist; run sync_catalog.py", file=sys.stderr)
        return 1
    try:
        current = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        print(f"error: {path} is not valid JSON: {e}", file=sys.stderr)
        return 1

    current_benchmarks = current.get("benchmarks")
    new_benchmarks = payload["benchmarks"]
    current_commit = (current.get("source") or {}).get("commit")
    new_commit = payload["source"]["commit"]

    problems = []
    if current_commit != new_commit:
        problems.append(f"source commit: {current_commit} != {new_commit}")
    if current_benchmarks != new_benchmarks:
        if not isinstance(current_benchmarks, list):
            problems.append("catalog has no benchmark list")
        else:
            old = {entry.get("id"): entry for entry in current_benchmarks if isinstance(entry, dict)}
            new = {entry["id"]: entry for entry in new_benchmarks}
            added = sorted(set(new) - set(old))
            removed = sorted(set(old) - set(new))
            changed = sorted(
                bid for bid in set(old) & set(new) if old[bid] != new[bid]
            )
            if added:
                problems.append(f"added: {', '.join(added)}")
            if removed:
                problems.append(f"removed: {', '.join(removed)}")
            if changed:
                problems.append(f"changed: {', '.join(changed[:20])}")
            if not (added or removed or changed):
                problems.append("benchmark ordering/format differs")

    if problems:
        print("catalog is out of date:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"catalog is up to date ({len(new_benchmarks)} benchmarks @ {new_commit[:8]})")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate catalog/benchmarks.json from the fork snapshot."
    )
    parser.add_argument("--repo", default=DEFAULT_REPO, help="owner/repo")
    parser.add_argument("--ref", default=DEFAULT_REF, help="branch, tag, or commit")
    parser.add_argument(
        "--out", type=Path, default=ROOT / "catalog", help="output directory"
    )
    parser.add_argument(
        "--expect",
        type=int,
        default=DEFAULT_EXPECT,
        help="expected benchmark count (0 disables the check)",
    )
    parser.add_argument("--jobs", type=int, default=DEFAULT_JOBS)
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare against the existing catalog instead of writing",
    )
    args = parser.parse_args()

    try:
        payload, entries = generate(args.repo, args.ref, args.expect, args.jobs)
    except (SyncError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if args.check:
        return check_catalog(payload, args.out)

    try:
        write_catalog(args.repo, args.ref, payload, args.out)
    except (SyncError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    source = payload["source"]
    print(
        f"wrote {len(entries)} benchmarks to {args.out / 'benchmarks.json'} "
        f"({source['repo']}@{source['ref']} = {source['commit'][:8]})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
