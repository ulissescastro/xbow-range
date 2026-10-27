#!/usr/bin/env python3
"""xbow-range — prep and host XBOW validation benchmarks on the local network.

Clones a single XBOW CTF-style benchmark from the validation-benchmarks repo,
bakes in its deterministic flag, builds the vulnerable container(s), and
publishes the web service on this host's DHCP LAN IP at a random high TCP port
so other machines or tools on the network can reach it.

Targets are persistent: `up` leaves them running and records state, so you can
host several at once, list them with `ps`, and tear them down with `down`.

This is NOT an agent/solver. It only prepares and serves the target.

Usage:
    ./xbow list                      # Benchmarks with descriptions (offline)
    ./xbow list --tag ssti --level 3 # Filter the local catalog
    ./xbow list --json               # Machine-readable catalog
    ./xbow info XBEN-093-24          # Full details + live hosting state
    ./xbow up XBEN-001-24            # Build + host a target on the LAN
    ./xbow up XBEN-001-24 XBEN-002-24
    ./xbow reset XBEN-001-24         # Wipe to a clean state (same image + port)
    ./xbow ps                        # Show running targets (URL + flag)
    ./xbow flag XBEN-001-24          # Print the expected flag
    ./xbow url XBEN-001-24           # Print the target URL(s)
    ./xbow down XBEN-001-24          # Stop + remove a target
    ./xbow down --all                # Stop + remove everything

Requires: docker + docker compose, git, python3, pyyaml.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

# =============================================================================
# Configuration
# =============================================================================

BENCHMARKS_REPO = "https://github.com/ulissescastro/validation-benchmarks.git"
BENCHMARKS_REF = "xbow-snapshot-2026-10-27"
BENCHMARKS_COMMIT = "7f64b30838acae11673d812204ee149172044114"
BENCHMARKS_SUBDIR = "benchmarks"

SCRIPT_DIR = Path(__file__).parent.resolve()
STATE_DIR = SCRIPT_DIR / ".xbow"
STATE_FILE = STATE_DIR / "state.json"
REPO_CACHE = STATE_DIR / "repo"           # shared sparse clone of the repo
TARGETS_DIR = SCRIPT_DIR / "targets"      # per-target working copies while up
CATALOG_FILE = SCRIPT_DIR / "catalog" / "benchmarks.json"   # offline metadata

BUILD_TIMEOUT = 600   # seconds
START_TIMEOUT = 180   # seconds

# Independent sidecar container serving the flag-validation API. It is not part
# of any target's compose project, so target teardown never touches it.
VALIDATOR_NAME = "xbow-validator"
VALIDATOR_IMAGE = "xbow-validator:latest"
VALIDATOR_DIR = SCRIPT_DIR / "validator"
VALIDATOR_STATE_FILE = STATE_DIR / "validator.json"
VALIDATOR_DEFAULT_PORT = 8800
API_STATE_DIR = STATE_DIR / "api"          # read-only view mounted into the sidecar


# =============================================================================
# Small helpers
# =============================================================================


def compute_flag(benchmark_id: str) -> str:
    """Deterministic flag: FLAG{sha256(BENCHMARK_ID_UPPERCASE)} (XBOW common.mk)."""
    digest = hashlib.sha256(benchmark_id.upper().encode()).hexdigest()
    return f"FLAG{{{digest}}}"


def project_name(benchmark_id: str) -> str:
    """Docker compose project name for a benchmark."""
    return benchmark_id.lower()


def find_free_port() -> int:
    """Ask the OS for a free high TCP port (Linux ephemeral range)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return int(s.getsockname()[1])


def detect_lan_ip() -> str:
    """Best-effort detection of this host's DHCP/LAN IPv4 address.

    Uses the source address the kernel would pick for the default route.
    No packets are sent (UDP connect just selects a route).
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return str(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        return "127.0.0.1"


def die(msg: str, code: int = 1) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def _run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def check_prereqs() -> None:
    for tool in ("docker", "git"):
        if shutil.which(tool) is None:
            die(f"'{tool}' not found on PATH")
    if _run(["docker", "compose", "version"]).returncode != 0:
        die("'docker compose' plugin not available")


# =============================================================================
# State (which targets are up)
# =============================================================================


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _atomic_write(path: Path, text: str) -> None:
    """Write text via a temp file + rename so readers never see a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp"
    tmp.write_text(text)
    os.replace(tmp, path)


def _sync_api_state(state: dict[str, Any]) -> None:
    """Publish the minimal hosted-target view consumed by the validator sidecar."""
    view = {
        benchmark_id: {
            "flag": entry.get("flag", ""),
            "urls": entry.get("urls", []),
            "published": entry.get("published", []),
            "started_at": entry.get("started_at"),
        }
        for benchmark_id, entry in state.items()
    }
    _atomic_write(API_STATE_DIR / "state.json", json.dumps(view, indent=2))


def save_state(state: dict[str, Any]) -> None:
    _atomic_write(STATE_FILE, json.dumps(state, indent=2))
    _sync_api_state(state)


# =============================================================================
# Benchmark catalog (local, offline)
# =============================================================================

_catalog_cache: dict[str, Any] | None = None


def load_catalog() -> dict[str, Any]:
    """Load the committed benchmark catalog. No git, network, or API calls."""
    global _catalog_cache
    if _catalog_cache is not None:
        return _catalog_cache

    if not CATALOG_FILE.exists():
        die(
            f"benchmark catalog not found: {CATALOG_FILE}\n"
            "  run: python3 scripts/sync_catalog.py"
        )
    try:
        data = json.loads(CATALOG_FILE.read_text())
    except (json.JSONDecodeError, OSError) as e:
        die(f"cannot read benchmark catalog {CATALOG_FILE}: {e}")

    benchmarks = data.get("benchmarks")
    if not isinstance(benchmarks, list) or not benchmarks:
        die(f"benchmark catalog is empty or malformed: {CATALOG_FILE}")

    source = data.get("source") or {}
    catalog_commit = source.get("commit")
    if BENCHMARKS_COMMIT and catalog_commit and catalog_commit != BENCHMARKS_COMMIT:
        print(
            f"warning: catalog commit {catalog_commit[:8]} != pinned source commit "
            f"{BENCHMARKS_COMMIT[:8]} (run python3 scripts/sync_catalog.py)",
            file=sys.stderr,
        )

    _catalog_cache = data
    return data


def _benchmark_sort_key(entry: dict[str, Any]) -> tuple[int, int]:
    match = re.match(r"^XBEN-(\d+)-(\d+)$", str(entry.get("id", "")))
    if not match:
        return (10**9, 10**9)
    return (int(match.group(1)), int(match.group(2)))


def catalog_benchmarks() -> list[dict[str, Any]]:
    benchmarks = list(load_catalog().get("benchmarks") or [])
    benchmarks.sort(key=_benchmark_sort_key)
    return benchmarks


def catalog_by_id() -> dict[str, dict[str, Any]]:
    return {str(entry.get("id")): entry for entry in catalog_benchmarks()}


def _short_name(entry: dict[str, Any]) -> str:
    """Drop the redundant 'XBEN-…' prefix from display names."""
    benchmark_id = str(entry.get("id", ""))
    name = str(entry.get("name") or benchmark_id)
    prefix = f"{benchmark_id} "
    return name[len(prefix):] if name.startswith(prefix) else name


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


def _truncate(text: str, limit: int) -> str:
    text = _one_line(text)
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _filter_catalog(
    benchmarks: list[dict[str, Any]], args: argparse.Namespace
) -> list[dict[str, Any]]:
    level = getattr(args, "level", None)
    tag = (getattr(args, "tag", None) or "").strip().lower()
    search = (getattr(args, "search", None) or "").strip().lower()

    selected = []
    for entry in benchmarks:
        if level is not None and int(entry.get("level", 0)) != level:
            continue
        if tag and tag not in {str(t).lower() for t in entry.get("tags") or []}:
            continue
        if search:
            haystack = " ".join(
                [
                    str(entry.get("id", "")),
                    str(entry.get("name", "")),
                    str(entry.get("description", "")),
                    " ".join(str(t) for t in entry.get("tags") or []),
                ]
            ).lower()
            if search not in haystack:
                continue
        selected.append(entry)
    return selected


# =============================================================================
# Benchmark repository (clone)
# =============================================================================


def _repo_cache_matches() -> bool:
    """True if the cached clone is the pinned source repo and commit."""
    if not (REPO_CACHE / ".git").exists():
        return False
    remote = _run(
        ["git", "-C", str(REPO_CACHE), "remote", "get-url", "origin"]
    ).stdout.strip()
    if remote != BENCHMARKS_REPO:
        return False
    if BENCHMARKS_COMMIT:
        head = _run(
            ["git", "-C", str(REPO_CACHE), "rev-parse", "HEAD"]
        ).stdout.strip()
        if head != BENCHMARKS_COMMIT:
            return False
    return True


def _ensure_repo_cache() -> None:
    """Create or reuse a shared blob-less sparse clone of the pinned repo."""
    if _repo_cache_matches():
        return

    if (REPO_CACHE / ".git").exists():
        print("  benchmark source cache changed; rebuilding ...")
        shutil.rmtree(REPO_CACHE, ignore_errors=True)

    REPO_CACHE.mkdir(parents=True, exist_ok=True)
    clone_cmd = [
        "git", "clone", "--filter=blob:none", "--no-checkout", "--depth=1",
    ]
    if BENCHMARKS_REF:
        clone_cmd += ["--branch", BENCHMARKS_REF]
    clone_cmd += [BENCHMARKS_REPO, str(REPO_CACHE)]
    _run(clone_cmd, check=True)
    _run(
        ["git", "sparse-checkout", "init", "--cone"],
        cwd=REPO_CACHE,
        check=True,
    )


def clone_benchmark(benchmark_id: str) -> Path:
    """Materialize a single benchmark into targets/<ID>/ and return that path."""
    _ensure_repo_cache()
    _run(
        ["git", "sparse-checkout", "add", f"{BENCHMARKS_SUBDIR}/{benchmark_id}"],
        cwd=REPO_CACHE,
        check=True,
    )
    _run(["git", "checkout"], cwd=REPO_CACHE, check=True)

    src = REPO_CACHE / BENCHMARKS_SUBDIR / benchmark_id
    if not src.exists():
        die(f"unknown benchmark '{benchmark_id}' (not in the repo)")

    dst = TARGETS_DIR / benchmark_id
    if dst.exists():
        shutil.rmtree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst)
    return dst


def load_benchmark_metadata(benchmark_path: Path) -> dict[str, Any]:
    benchmark_id = benchmark_path.name
    metadata_file = benchmark_path / "benchmark.json"
    metadata: dict[str, Any] = {}
    if metadata_file.exists():
        with metadata_file.open() as f:
            metadata = json.load(f)
    return {
        "id": benchmark_id,
        "name": metadata.get("name", benchmark_id),
        "description": metadata.get("description", ""),
        "level": metadata.get("level", ""),
        "tags": metadata.get("tags", []),
        "flag": compute_flag(benchmark_id),
    }


# =============================================================================
# Docker compose: rewrite, build, start, stop
# =============================================================================


def rewrite_compose(original_path: Path, flag: str) -> tuple[Path, list[dict[str, int]]]:
    """Rewrite docker-compose.yml for LAN hosting.

    - force linux/amd64
    - inject FLAG as build-arg and environment variable
    - publish each `ports:` entry on 0.0.0.0 at a fresh random high port

    Returns (rewritten_compose_path, published) where published is a list of
    {"service", "host_port", "container_port"} dicts.
    """
    with original_path.open() as f:
        compose_data = yaml.safe_load(f)

    published: list[dict[str, Any]] = []
    services = compose_data.get("services", {})

    for service_name, service_config in services.items():
        service_config["platform"] = "linux/amd64"

        # Inject FLAG as a build arg
        build = service_config.get("build", {})
        if isinstance(build, str):
            build = {"context": build}
            service_config["build"] = build
        if isinstance(build, dict):
            args = build.get("args", {})
            if isinstance(args, list):
                # list form may contain bare "FLAG" (value from env) — replace it
                args = [a for a in args if str(a).split("=")[0].strip() != "FLAG"]
                args.append(f"FLAG={flag}")
            else:
                args["FLAG"] = flag
            build["args"] = args

        # Inject FLAG as an environment variable
        env = service_config.get("environment", [])
        if isinstance(env, list):
            env = [e for e in env if str(e).split("=")[0].strip() != "FLAG"]
            env.append(f"FLAG={flag}")
        elif isinstance(env, dict):
            env["FLAG"] = flag
        service_config["environment"] = env

        # Normalize `expose` (leave internal — not published)
        if "expose" in service_config:
            service_config["expose"] = [
                int(str(exp).split(":")[-1]) for exp in service_config["expose"]
            ]

        # Publish each `ports:` entry on 0.0.0.0 at a fresh random high port
        if "ports" in service_config:
            new_ports = []
            for port_entry in service_config["ports"]:
                container_port = int(str(port_entry).split(":")[-1])
                host_port = find_free_port()
                new_ports.append(f"0.0.0.0:{host_port}:{container_port}")
                published.append(
                    {
                        "service": service_name,
                        "host_port": host_port,
                        "container_port": container_port,
                    }
                )
            service_config["ports"] = new_ports

    output_path = original_path.parent / ".docker-compose.xben.yml"
    with output_path.open("w") as f:
        yaml.dump(compose_data, f, default_flow_style=False)

    return output_path, published


def _compose_cmd(project: str, compose_file: Path, *args: str) -> list[str]:
    return [
        "docker", "compose", "-p", project, "-f", compose_file.name, *args,
    ]


def build_target(target_dir: Path, compose_file: Path, project: str, flag: str) -> None:
    result = _run(
        _compose_cmd(project, compose_file, "build"),
        cwd=target_dir,
        timeout=BUILD_TIMEOUT,
        env={**os.environ, "FLAG": flag},
    )
    if result.returncode != 0:
        raise RuntimeError(f"build failed:\n{result.stderr or result.stdout}")


def start_target(target_dir: Path, compose_file: Path, project: str) -> None:
    # Try to wait for healthy; fall back to a plain detached start.
    result = _run(
        _compose_cmd(
            project, compose_file, "up", "-d", "--wait", "--wait-timeout",
            str(START_TIMEOUT),
        ),
        cwd=target_dir,
        timeout=START_TIMEOUT + 30,
    )
    if result.returncode != 0:
        print("  (healthcheck wait failed; starting without --wait)")
        result = _run(
            _compose_cmd(project, compose_file, "up", "-d"),
            cwd=target_dir,
            timeout=START_TIMEOUT,
        )
        if result.returncode != 0:
            raise RuntimeError(f"start failed:\n{result.stderr or result.stdout}")


def reset_target(entry: dict[str, Any]) -> None:
    """Wipe a target back to a clean state, cheaply.

    Drops the containers, volumes, and compose network (so every runtime
    modification an agent made — writable layer *and* DB/volume data — is
    discarded) and recreates the services from the already-built images.

    Keeps the built images and the rewritten compose file, so there is no
    rebuild and no re-pull, and the published host port is unchanged (it is
    hardcoded in the compose file). `compose up` only builds when an image is
    missing, so the normal case reuses the existing image untouched.
    """
    target_dir = Path(entry["target_dir"])
    compose_file = Path(entry["compose_file"])
    project = entry["project"]
    if not compose_file.exists():
        raise RuntimeError(
            "working copy is gone; use 'up <ID> --restart' to rebuild"
        )
    # Drop containers + volumes + network, but NOT images (no --rmi).
    result = _run(
        _compose_cmd(project, compose_file, "down", "-v", "--remove-orphans"),
        cwd=target_dir,
        timeout=150,
    )
    if result.returncode != 0:
        raise RuntimeError(f"reset down failed:\n{result.stderr or result.stdout}")
    # Recreate from the existing image (same ports, from the same compose file).
    start_target(target_dir, compose_file, project)


def _compose_down(target_dir: Path, compose_file: Path, project: str) -> None:
    """compose down removing volumes, orphans, and locally-built images.

    `--rmi local` drops the images compose built for this project but leaves
    shared base images (mysql, debian, python, ...) that were only pulled.
    """
    if not compose_file.exists():
        return
    with contextlib.suppress(Exception):
        _run(
            _compose_cmd(
                project, compose_file,
                "down", "-v", "--remove-orphans", "--rmi", "local",
            ),
            cwd=target_dir,
            timeout=150,
        )


def _project_images(project: str) -> list[str]:
    """repo:tag of images built for this compose project (never base images)."""
    result = _run(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"])
    if result.returncode != 0:
        return []
    prefixes = (f"{project}-", f"{project}_")
    return [
        line for line in result.stdout.splitlines()
        if line and line.rsplit(":", 1)[0].startswith(prefixes)
    ]


def _remove_project_artifacts(project: str) -> list[str]:
    """Name-based cleanup that works even if the compose file is gone.

    Removes only this project's own containers, built images, and default
    network. Shared base images are never matched. Returns removed image refs.
    """
    cids = _run(
        ["docker", "ps", "-aq", "--filter", f"name={project}-"]
    ).stdout.split()
    if cids:
        _run(["docker", "rm", "-f", *cids], timeout=60)

    imgs = _project_images(project)
    if imgs:
        _run(["docker", "rmi", "-f", *imgs], timeout=120)

    with contextlib.suppress(Exception):
        _run(["docker", "network", "rm", f"{project}_default"], timeout=30)
    return imgs


def full_teardown(entry: dict[str, Any]) -> list[str]:
    """Remove everything a hosted target created: containers, volumes, network,
    built images, and the working copy. Returns the removed image refs."""
    target_dir = Path(entry["target_dir"])
    compose_file = Path(entry["compose_file"])
    project = entry["project"]
    before = set(_project_images(project))
    _compose_down(target_dir, compose_file, project)
    _remove_project_artifacts(project)  # removes anything compose left
    removed = sorted(before - set(_project_images(project)))
    shutil.rmtree(target_dir, ignore_errors=True)
    return removed


def _is_xben_name(name: str) -> bool:
    return name.startswith(("xben-", "xben_"))


def sweep_xben_orphans() -> dict[str, list[str]]:
    """Remove ALL leftover xben-* docker artifacts (containers, built images,
    networks, volumes). Safe to call only when nothing is hosted — it catches
    stragglers from crashes or pre-fix runs so nothing accumulates."""
    swept: dict[str, list[str]] = {"containers": [], "images": [], "networks": [], "volumes": []}

    cids = _run(["docker", "ps", "-aq", "--filter", "name=xben-"]).stdout.split()
    if cids:
        _run(["docker", "rm", "-f", *cids], timeout=120)
        swept["containers"] = cids

    imgs = [
        line for line in _run(
            ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"]
        ).stdout.splitlines()
        if line and _is_xben_name(line.rsplit(":", 1)[0])
    ]
    if imgs:
        _run(["docker", "rmi", "-f", *imgs], timeout=180)
        swept["images"] = imgs

    nets = [
        n for n in _run(
            ["docker", "network", "ls", "--format", "{{.Name}}"]
        ).stdout.split()
        if _is_xben_name(n)
    ]
    for n in nets:
        with contextlib.suppress(Exception):
            _run(["docker", "network", "rm", n], timeout=30)
    swept["networks"] = nets

    vols = [
        v for v in _run(
            ["docker", "volume", "ls", "--format", "{{.Name}}"]
        ).stdout.split()
        if _is_xben_name(v)
    ]
    if vols:
        _run(["docker", "volume", "rm", "-f", *vols], timeout=60)
        swept["volumes"] = vols

    return swept


def prune_shared() -> None:
    """Reclaim shared, reusable docker space: dangling images + build cache.

    Global (not xben-specific). Runs on every `down` by default; `--no-purge`
    skips it.
    """
    print("  pruning dangling images and build cache (global) ...")
    _run(["docker", "image", "prune", "-f"], timeout=120)
    _run(["docker", "builder", "prune", "-f"], timeout=300)


def target_running(target_dir: Path, compose_file: Path, project: str) -> bool:
    if not compose_file.exists():
        return False
    result = _run(
        _compose_cmd(project, compose_file, "ps", "-q"),
        cwd=target_dir,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


# =============================================================================
# EOL Debian apt fix
# =============================================================================
#
# Many benchmarks build on Debian releases that have reached end-of-life
# (buster, bullseye, ...). Once a release is archived, its apt repos on
# deb.debian.org/security.debian.org are pruned and `apt-get install` fails
# with 404s. We repoint apt to archive.debian.org so the original base image
# and package list still install. This is faithful: the base image and the
# packages are unchanged — only the mirror URL and the stale-metadata check.

_EOL_DEBIAN_CODENAMES = ("buster", "bullseye", "stretch", "jessie", "wheezy")
_EOL_DEBIAN_NUMBERED = ("debian:8", "debian:9", "debian:10", "debian:11")

_APT_ARCHIVE_MARKER = "# [xbow] EOL-Debian apt archive fix"
_APT_ARCHIVE_FIX = r"""RUN set -eux; \
    if [ -f /etc/apt/sources.list ]; then \
      sed -i -e 's|http://deb.debian.org/debian-security|http://archive.debian.org/debian-security|g' \
             -e 's|http://deb.debian.org/debian|http://archive.debian.org/debian|g' \
             -e 's|http://security.debian.org/debian-security|http://archive.debian.org/debian-security|g' \
             -e 's|http://security.debian.org/debian|http://archive.debian.org/debian-security|g' \
             /etc/apt/sources.list; \
      sed -i '/-updates/d' /etc/apt/sources.list; \
    fi; \
    echo 'Acquire::Check-Valid-Until "false";' > /etc/apt/apt.conf.d/99xbow-archive"""


def _from_is_eol_debian(from_line: str) -> bool:
    """True if a Dockerfile FROM line uses an end-of-life Debian base."""
    tokens = from_line.lower().split()[1:]  # drop the leading 'from'
    ref = next((t for t in tokens if not t.startswith("--")), "")
    if not ref:
        return False
    if any(cn in ref for cn in _EOL_DEBIAN_CODENAMES):
        return True
    return any(ref.startswith(n) for n in _EOL_DEBIAN_NUMBERED)


def patch_eol_debian_apt(target_dir: Path) -> list[str]:
    """Repoint apt to archive.debian.org in Dockerfiles on EOL Debian bases.

    Edits only the throwaway per-target copy (the repo cache stays pristine).
    Returns the list of patched Dockerfile paths (relative to target_dir).
    """
    patched: list[str] = []
    for dockerfile in sorted(target_dir.rglob("Dockerfile*")):
        if not dockerfile.is_file():
            continue
        text = dockerfile.read_text()
        if _APT_ARCHIVE_MARKER in text:
            continue
        out: list[str] = []
        changed = False
        for line in text.splitlines():
            out.append(line)
            if line.strip().lower().startswith("from ") and _from_is_eol_debian(line):
                out.append(_APT_ARCHIVE_MARKER)
                out.append(_APT_ARCHIVE_FIX)
                changed = True
        if changed:
            dockerfile.write_text("\n".join(out) + "\n")
            patched.append(str(dockerfile.relative_to(target_dir)))
    return patched


# =============================================================================
# Flag-validation sidecar
# =============================================================================
#
# A single tiny container, independent of every target's compose project, that
# reads the hosted-target view xbow publishes to .xbow/api/state.json and
# answers flag-validation requests on one endpoint (/validate). It is started
# whenever at least one target is hosted and removed when the last one goes
# away. The container prefers host networking so it can probe targets through
# their published loopback port.


def _container_running(name: str) -> bool:
    result = _run(["docker", "inspect", "-f", "{{.State.Running}}", name])
    return result.returncode == 0 and result.stdout.strip().lower() == "true"


def _image_exists(ref: str) -> bool:
    return _run(["docker", "image", "inspect", ref]).returncode == 0


def _port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        # SO_REUSEADDR lets us reuse our own port while old connections are in
        # TIME_WAIT; it does not let us steal a port that is actively listening.
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
        except OSError:
            return False
    return True


def _wait_for_port(port: int, timeout: float = 15.0) -> bool:
    """Wait until a freshly-started sidecar is accepting connections."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), 0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _validator_image_hash() -> str:
    """Fingerprint of the sidecar sources, so dev edits trigger a rebuild."""
    hasher = hashlib.sha256()
    for name in ("Dockerfile", "app.py"):
        source = VALIDATOR_DIR / name
        hasher.update(name.encode())
        hasher.update(b"\0")
        if source.exists():
            hasher.update(source.read_bytes())
        hasher.update(b"\0")
    return hasher.hexdigest()


def _read_validator_state() -> dict[str, Any]:
    try:
        return json.loads(VALIDATOR_STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _validator_token() -> str:
    return os.environ.get("XBOW_VALIDATOR_TOKEN", "")


def _build_validator(image_hash: str) -> None:
    """Build the sidecar image if it is missing or the sources changed."""
    data = _read_validator_state()
    if _image_exists(VALIDATOR_IMAGE) and data.get("image_hash") == image_hash:
        return
    if not VALIDATOR_DIR.is_dir():
        raise RuntimeError(f"validator sources missing: {VALIDATOR_DIR}")
    print("  building flag-validation API image ...")
    result = _run(
        ["docker", "build", "-t", VALIDATOR_IMAGE, str(VALIDATOR_DIR)],
        timeout=BUILD_TIMEOUT,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"validator image build failed:\n{result.stderr or result.stdout}"
        )
    data["image_hash"] = image_hash
    _atomic_write(VALIDATOR_STATE_FILE, json.dumps(data, indent=2))


def ensure_validator(state: dict[str, Any]) -> int | None:
    """Ensure the sidecar is up while at least one target is hosted.

    Returns the listening port, or None when there is nothing to validate.
    Idempotent: reuses a running container and its persisted port.
    """
    if not state:
        return None

    image_hash = _validator_image_hash()
    data = _read_validator_state()
    port = data.get("port")

    if _container_running(VALIDATOR_NAME):
        if data.get("image_hash") == image_hash:
            return int(port) if port else VALIDATOR_DEFAULT_PORT
        # Sources changed: recreate from the fresh image below.
        _run(["docker", "rm", "-f", VALIDATOR_NAME], timeout=60)

    # Reaching here means the sidecar is not running (or is stale).
    _run(["docker", "rm", "-f", VALIDATOR_NAME], timeout=60)
    _build_validator(image_hash)
    _sync_api_state(state)  # the mount must exist before `docker run`

    persisted = int(port) if str(port).isdigit() else None
    if persisted and 1024 <= persisted <= 65535 and _port_available(persisted):
        chosen = persisted
    else:
        chosen = find_free_port()

    cmd = [
        "docker", "run", "-d",
        "--name", VALIDATOR_NAME,
        "--restart", "unless-stopped",
        "--read-only", "--tmpfs", "/tmp",
        "--network", "host",
        "--label", "xbow.role=validator",
        "-e", f"PORT={chosen}",
        "-e", "XBOW_STATE_DIR=/state",
        "-v", f"{API_STATE_DIR}:/state:ro",
    ]
    token = _validator_token()
    if token:
        cmd += ["-e", f"XBOW_VALIDATOR_TOKEN={token}"]
    cmd.append(VALIDATOR_IMAGE)

    result = _run(cmd, timeout=120)
    if result.returncode != 0:
        # A concurrent `up` may have won the race to create the container.
        if _container_running(VALIDATOR_NAME):
            other = _read_validator_state().get("port")
            if other:
                return int(other)
        raise RuntimeError(
            f"failed to start validator:\n{result.stderr or result.stdout}"
        )

    data.update(
        port=chosen,
        image_hash=image_hash,
        started_at=datetime.now(UTC).isoformat(),
    )
    _atomic_write(VALIDATOR_STATE_FILE, json.dumps(data, indent=2))
    if not _wait_for_port(chosen):
        print("  warning: validator started but is not accepting connections yet")
    return chosen


def stop_validator() -> None:
    """Remove the sidecar container (keep its tiny image and chosen port)."""
    data = _read_validator_state()
    _run(["docker", "rm", "-f", VALIDATOR_NAME], timeout=60)
    if data:
        data["stopped_at"] = datetime.now(UTC).isoformat()
        _atomic_write(VALIDATOR_STATE_FILE, json.dumps(data, indent=2))


def validator_url() -> str | None:
    """LAN URL of the running sidecar, or None when it is not up."""
    if not _container_running(VALIDATOR_NAME):
        return None
    port = _read_validator_state().get("port") or VALIDATOR_DEFAULT_PORT
    return f"http://{detect_lan_ip()}:{port}/validate"


def _announce_validator(state: dict[str, Any], *, quiet: bool = False) -> None:
    """Reconcile the sidecar with the current state after a command."""
    if not state:
        stop_validator()
        return
    try:
        port = ensure_validator(state)
    except Exception as e:  # noqa: BLE001 — the target itself is already up
        print(f"  warning: flag-validation API unavailable: {e}")
        return
    if port and not quiet:
        print(f"\nFlag validation API: http://{detect_lan_ip()}:{port}/validate")


# =============================================================================
# Commands
# =============================================================================


def cmd_list(args: argparse.Namespace) -> None:
    catalog = load_catalog()
    source = catalog.get("source") or {}
    selected = _filter_catalog(catalog_benchmarks(), args)

    if args.ids:
        for entry in selected:
            print(entry["id"])
        return

    if args.json:
        payload = {
            "source": source,
            "count": len(selected),
            "benchmarks": selected,
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    if not selected:
        print("No benchmarks matched.")
        return

    limit = 0 if args.full else 200
    for index, entry in enumerate(selected):
        if index:
            print()
        tags = ", ".join(str(t) for t in entry.get("tags") or []) or "-"
        print(f"{entry['id']}  L{entry.get('level', '?')}  {tags}")
        print(f"  {_short_name(entry)}")
        print(f"  {_truncate(str(entry.get('description') or ''), limit)}")

    repo = source.get("repo", "local catalog")
    commit = str(source.get("commit") or "")[:8]
    suffix = f"  ·  source: {repo}@{commit}" if commit else ""
    print(f"\n{len(selected)} benchmark(s){suffix}")


def cmd_info(args: argparse.Namespace) -> None:
    catalog = load_catalog()
    source = catalog.get("source") or {}
    benchmark_id = args.benchmark.strip().upper()
    entry = catalog_by_id().get(benchmark_id)
    if entry is None:
        die(f"unknown benchmark '{benchmark_id}' (use './xbow list')", code=2)

    hosted_info: dict[str, Any] | None = None
    hosted = load_state().get(benchmark_id)
    if hosted:
        running = target_running(
            Path(hosted["target_dir"]),
            Path(hosted["compose_file"]),
            hosted["project"],
        )
        hosted_info = {
            "state": "up" if running else "stopped",
            "uptime": _fmt_uptime(hosted["started_at"]) if running else None,
            "urls": hosted.get("urls", []),
            "primary_url": hosted.get("primary_url", ""),
            "flag": hosted.get("flag") or compute_flag(benchmark_id),
        }

    if args.json:
        payload = {
            "benchmark": entry,
            "source": source,
            "hosted": hosted_info,
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    tags = ", ".join(str(t) for t in entry.get("tags") or []) or "-"
    commit = str(source.get("commit") or "")[:8]
    print(f"{entry['id']}  {_short_name(entry)}")
    print(f"Level:  {entry.get('level')}")
    print(f"Tags:   {tags}")
    print(f"Source: {source.get('repo', 'local catalog')}@{commit}")
    print()
    print(_one_line(str(entry.get("description") or "")))
    print()
    if hosted_info is None:
        print("Status: not hosted")
        return
    print(f"Status: {hosted_info['state']}")
    for url in hosted_info["urls"]:
        print(f"URL:    {url}")
    if hosted_info["uptime"]:
        print(f"Uptime: {hosted_info['uptime']}")
    print(f"Flag:   {hosted_info['flag']}")


def _host_entry(benchmark_id: str, state: dict[str, Any]) -> dict[str, Any]:
    target_dir = clone_benchmark(benchmark_id)
    meta = load_benchmark_metadata(target_dir)
    flag = meta["flag"]
    project = project_name(benchmark_id)

    compose_src = target_dir / "docker-compose.yml"
    if not compose_src.exists():
        shutil.rmtree(target_dir, ignore_errors=True)
        raise RuntimeError(f"{benchmark_id}: no docker-compose.yml in benchmark")

    patched = patch_eol_debian_apt(target_dir)
    if patched:
        print(f"  apt->archive.debian.org fix applied (EOL Debian base): {', '.join(patched)}")

    print(f"  building {benchmark_id} ({meta['name']}) ...")
    compose_file, published = rewrite_compose(compose_src, flag)

    # On any build/start failure, roll back completely so nothing dangles.
    partial = {
        "project": project,
        "target_dir": str(target_dir),
        "compose_file": str(compose_file),
    }
    try:
        build_target(target_dir, compose_file, project, flag)
        print("  starting services ...")
        start_target(target_dir, compose_file, project)
    except Exception:
        print("  failed — rolling back (containers, images, network, files) ...")
        full_teardown(partial)
        raise

    lan_ip = detect_lan_ip()
    urls = [f"http://{lan_ip}:{p['host_port']}" for p in published]

    entry = {
        "id": benchmark_id,
        "name": meta["name"],
        "level": meta["level"],
        "tags": meta["tags"],
        "description": meta["description"],
        "flag": flag,
        "project": project_name(benchmark_id),
        "target_dir": str(target_dir),
        "compose_file": str(compose_file),
        "lan_ip": lan_ip,
        "published": published,
        "urls": urls,
        "primary_url": urls[0] if urls else "",
        "started_at": datetime.now(UTC).isoformat(),
    }
    state[benchmark_id] = entry
    save_state(state)
    return entry


def cmd_up(args: argparse.Namespace) -> None:
    check_prereqs()
    state = load_state()
    for benchmark_id in args.benchmarks:
        print(f"\n=== {benchmark_id} ===")
        if benchmark_id in state and not args.restart:
            existing = state[benchmark_id]
            if target_running(
                Path(existing["target_dir"]),
                Path(existing["compose_file"]),
                existing["project"],
            ):
                print(f"  already up: {existing['primary_url']}")
                print("  (use --restart to rebuild, or 'down' first)")
                continue
        if benchmark_id in state and args.restart:
            full_teardown(state[benchmark_id])
            state.pop(benchmark_id, None)
            save_state(state)

        try:
            entry = _host_entry(benchmark_id, state)
        except Exception as e:  # noqa: BLE001 — report and move on, don't abort the batch
            print(f"  error: {e}")
            continue

        print(f"\n  {benchmark_id} is up:")
        for url in entry["urls"]:
            print(f"    {url}")
        print(f"    flag: {entry['flag']}")

    # Keep the validation sidecar in lock-step with what is hosted: start it
    # after the first success, remove it when nothing is left.
    _announce_validator(state)


def cmd_reset(args: argparse.Namespace) -> None:
    check_prereqs()
    state = load_state()
    if not state:
        die("no hosted targets (use 'up' first)")
    targets = list(state.keys()) if args.all else args.benchmarks
    if not targets:
        die("specify benchmark IDs or --all")
    for benchmark_id in targets:
        print(f"\n=== {benchmark_id} ===")
        entry = state.get(benchmark_id)
        if entry is None:
            print(f"  {benchmark_id}: not hosted, skipping (use 'up' first)")
            continue
        print(f"  resetting {benchmark_id} (same image + port, no rebuild) ...")
        try:
            reset_target(entry)
        except Exception as e:  # noqa: BLE001 — report and move on
            print(f"  error: {e}")
            continue
        entry["started_at"] = datetime.now(UTC).isoformat()
        state[benchmark_id] = entry
        save_state(state)

        print(f"\n  {benchmark_id} reset to a clean state:")
        for url in entry["urls"]:
            print(f"    {url}")
        print(f"    flag: {entry['flag']}")

    _announce_validator(state, quiet=True)


def _fmt_uptime(started_at: str) -> str:
    try:
        started = datetime.fromisoformat(started_at)
    except ValueError:
        return "?"
    delta = datetime.now(UTC) - started
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h{(secs % 3600) // 60}m"
    return f"{secs // 86400}d"


def cmd_ps(_args: argparse.Namespace) -> None:
    state = load_state()
    if not state:
        print("No targets hosted. Use: xbow up <BENCHMARK_ID>")
        return
    rows = []
    for entry in state.values():
        running = target_running(
            Path(entry["target_dir"]),
            Path(entry["compose_file"]),
            entry["project"],
        )
        rows.append(
            (
                entry["id"],
                "up" if running else "stopped",
                _fmt_uptime(entry["started_at"]) if running else "-",
                entry.get("primary_url", ""),
                entry["flag"],
            )
        )
    w_id = max(len("BENCHMARK"), *(len(r[0]) for r in rows))
    w_st = max(len("STATE"), *(len(r[1]) for r in rows))
    w_up = max(len("UPTIME"), *(len(r[2]) for r in rows))
    w_url = max(len("URL"), *(len(r[3]) for r in rows))
    print(f"{'BENCHMARK':<{w_id}}  {'STATE':<{w_st}}  {'UPTIME':<{w_up}}  "
          f"{'URL':<{w_url}}  FLAG")
    for r in rows:
        print(f"{r[0]:<{w_id}}  {r[1]:<{w_st}}  {r[2]:<{w_up}}  "
              f"{r[3]:<{w_url}}  {r[4]}")

    api = validator_url()
    print(f"\nFlag validation API: {api or 'not running'}")


def cmd_down(args: argparse.Namespace) -> None:
    state = load_state()
    purge = getattr(args, "purge", True)

    if not state:
        print("No hosted targets.")
    else:
        targets = list(state.keys()) if args.all else args.benchmarks
        if not targets:
            die("specify benchmark IDs or --all")
        for benchmark_id in targets:
            entry = state.get(benchmark_id)
            if entry is None:
                print(f"  {benchmark_id}: not hosted, skipping")
                continue
            print(f"  tearing down {benchmark_id} ...")
            removed = full_teardown(entry)
            if removed:
                print(f"    removed images: {', '.join(removed)}")
            state.pop(benchmark_id, None)
        save_state(state)

    # The API only exists while at least one target is hosted.
    if not state:
        stop_validator()

    # When nothing is hosted anymore, sweep any leftover xben-* artifacts
    # (stragglers from crashes or earlier runs) so nothing accumulates.
    if not state:
        swept = sweep_xben_orphans()
        extra_imgs = swept["images"]
        if extra_imgs:
            print(f"  swept orphan images: {', '.join(extra_imgs)}")
        leftovers = [k for k, v in swept.items() if v]
        if leftovers:
            print(f"  swept orphan xben artifacts: {', '.join(leftovers)}")

    if purge:
        prune_shared()

    print("done.")


def cmd_flag(args: argparse.Namespace) -> None:
    print(compute_flag(args.benchmark))


def cmd_url(args: argparse.Namespace) -> None:
    state = load_state()
    entry = state.get(args.benchmark)
    if entry is None:
        die(f"{args.benchmark} is not hosted (use 'up' first)")
    for url in entry["urls"]:
        print(url)


def cmd_api(_args: argparse.Namespace) -> None:
    url = validator_url()
    if url is None:
        die("flag-validation API is not running (use 'up' first)")
    print(url)


def cmd_validate(args: argparse.Namespace) -> None:
    """Validate a candidate flag through the running sidecar API."""
    import urllib.error
    import urllib.request

    state = load_state()
    benchmark_id = args.benchmark.strip().upper()
    if benchmark_id not in state:
        die(f"{benchmark_id} is not hosted (use 'up' first)", code=2)
    if not _container_running(VALIDATOR_NAME):
        die("flag-validation API is not running (use 'up' first)", code=2)

    port = _read_validator_state().get("port") or VALIDATOR_DEFAULT_PORT
    payload = json.dumps(
        {"benchmark_id": benchmark_id, "flag": args.flag}
    ).encode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/validate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    token = _validator_token()
    if token:
        request.add_header("Authorization", f"Bearer {token}")

    try:
        with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
            result = json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        die(f"API error {e.code}: {e.read().decode(errors='replace')}", code=2)
    except OSError as e:
        die(f"API request failed: {e}", code=2)

    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get("valid") else 1)


# =============================================================================
# CLI
# =============================================================================


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xbow",
        description="Prep and host XBOW validation benchmarks on the local network.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser(
        "list", help="List benchmarks with descriptions (offline catalog)"
    )
    output = p_list.add_mutually_exclusive_group()
    output.add_argument(
        "--json", action="store_true", help="Print the catalog as JSON"
    )
    output.add_argument(
        "--ids", action="store_true", help="Print benchmark IDs only"
    )
    p_list.add_argument(
        "--full", action="store_true", help="Do not truncate descriptions"
    )
    p_list.add_argument(
        "--level", type=int, metavar="N", help="Only show this level (1-3)"
    )
    p_list.add_argument("--tag", metavar="TAG", help="Only show this tag")
    p_list.add_argument(
        "--search", metavar="TERM", help="Search ID, name, description, and tags"
    )
    p_list.set_defaults(func=cmd_list)

    p_info = sub.add_parser(
        "info", help="Show full details for a benchmark (optionally live state)"
    )
    p_info.add_argument("benchmark", metavar="ID")
    p_info.add_argument("--json", action="store_true", help="Print JSON")
    p_info.set_defaults(func=cmd_info)

    p_up = sub.add_parser("up", help="Build and host one or more targets")
    p_up.add_argument("benchmarks", nargs="+", metavar="ID")
    p_up.add_argument(
        "--restart",
        action="store_true",
        help="Rebuild/restart even if already hosted",
    )
    p_up.set_defaults(func=cmd_up)

    p_reset = sub.add_parser(
        "reset",
        help="Wipe a target to a clean state (same image + port, no rebuild)",
    )
    p_reset.add_argument("benchmarks", nargs="*", metavar="ID")
    p_reset.add_argument(
        "--all", action="store_true", help="Reset every hosted target"
    )
    p_reset.set_defaults(func=cmd_reset)

    sub.add_parser("ps", help="Show hosted targets").set_defaults(func=cmd_ps)

    p_down = sub.add_parser(
        "down",
        help="Stop and fully remove targets (also prunes dangling images + build cache)",
    )
    p_down.add_argument("benchmarks", nargs="*", metavar="ID")
    p_down.add_argument("--all", action="store_true", help="Tear down every target")
    p_down.add_argument(
        "--no-purge",
        dest="purge",
        action="store_false",
        help="Skip the global dangling-image/build-cache prune (keep shared cache)",
    )
    p_down.set_defaults(func=cmd_down, purge=True)

    p_flag = sub.add_parser("flag", help="Print a benchmark's expected flag")
    p_flag.add_argument("benchmark", metavar="ID")
    p_flag.set_defaults(func=cmd_flag)

    p_url = sub.add_parser("url", help="Print a hosted target's URL(s)")
    p_url.add_argument("benchmark", metavar="ID")
    p_url.set_defaults(func=cmd_url)

    sub.add_parser(
        "api", help="Print the flag-validation API URL (if running)"
    ).set_defaults(func=cmd_api)

    p_validate = sub.add_parser(
        "validate",
        help="Validate a captured flag through the local API",
    )
    p_validate.add_argument("benchmark", metavar="ID")
    p_validate.add_argument("flag", metavar="FLAG")
    p_validate.set_defaults(func=cmd_validate)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
