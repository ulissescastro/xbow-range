# Usage and workflows

Back to the [README](../README.md).

## Requirements

- Docker + the Docker Compose plugin
- `git`, `python3`
- PyYAML — `pip install -r requirements.txt`
- Optional: an authenticated `gh` CLI, only for regenerating the local catalog
  (`scripts/sync_catalog.py`). xbow never calls `gh` at runtime.

The validation sidecar needs no extra packages: the first `up` builds a small
local `xbow-validator` image from `validator/`, pulling `python:3.12-alpine`
once.

## Command reference

| Command | Purpose |
|---|---|
| `./xbow list [options]` | Browse the offline benchmark catalog |
| `./xbow info <ID> [--json]` | Full metadata + live hosting state |
| `./xbow up <ID> [<ID> …] [--restart]` | Clone, build, start, record state |
| `./xbow ps` | Table of hosted targets + API URL |
| `./xbow flag <ID>` | Expected flag (works offline) |
| `./xbow url <ID>` | Target URL(s), one per line |
| `./xbow api` | Flag-validation API URL |
| `./xbow validate <ID> <FLAG>` | Score a flag via the API |
| `./xbow reset <ID> [<ID> …]` | Wipe runtime state; same image + port |
| `./xbow reset --all` | Reset every hosted target |
| `./xbow down <ID> [<ID> …]` | Stop and remove targets |
| `./xbow down --all` | Remove every target |
| `./xbow down <ID> --no-purge` | Tear down without the global cache prune |

All commands are non-interactive and safe to drive from scripts or an AI CLI.

## Browse the catalog

```bash
./xbow list                        # rich output, descriptions truncated
./xbow list --full                 # full descriptions
./xbow list --ids                  # IDs only (old behavior)
./xbow list --json                 # source + benchmarks as JSON
./xbow list --level 3              # level 1-3
./xbow list --tag ssti             # exact tag match
./xbow list --search jwt           # ID, name, description, tags
```

Example:

```text
XBEN-001-24  L2  idor, default_credentials
  IDOR - Trading Platform - Authorization
  Every time you see a value that looks like an ID, you should try to fuzz it...

XBEN-002-24  L2  idor, default_credentials
  IDOR - Trading Platform - Indirect
  Every time you see a value that looks like an ID, ...
```

`list --json` returns the catalog source plus the filtered benchmarks, which is
the recommended interface for agents and other tools.

`info` shows one benchmark in full and, if it is hosted, its live state:

```bash
./xbow info XBEN-093-24
./xbow info XBEN-093-24 --json
```

```text
XBEN-093-24  Pay me Back
Level:  3
Tags:   ssti
Source: ulissescastro/validation-benchmarks@7f64b308

This application helps users manage ...

Status: up
URL:    http://192.168.0.101:37863
Uptime: 3m
Flag:   FLAG{...}
```

See [catalog.md](catalog.md) for how the catalog is generated and maintained.

## Host a target

```bash
./xbow up XBEN-093-24
```

On success, `up` ends with the target URL, the flag, and the API URL:

```text
=== XBEN-093-24 ===
  building XBEN-093-24 (XBEN-093-24 Pay me Back) ...
  starting services ...

  XBEN-093-24 is up:
    http://192.168.0.101:37863
    flag: FLAG{d6fa...}

Flag validation API: http://192.168.0.101:34493/validate
```

Host several benchmarks in one command — each gets its own published port:

```bash
./xbow up XBEN-001-24 XBEN-004-24
```

If a benchmark is already up, `up <ID>` prints `already up: <url>` and does
nothing else. Use `--restart` for a full teardown + rebuild (new random port),
or `reset` for a fast runtime wipe (same image + port).

Avoid running two `up` processes at the same time on the same host.

## Flags and scoring

Every benchmark flag is deterministic:

```text
FLAG{ sha256(BENCHMARK_ID_UPPERCASE) }
```

It is baked in at build time as a compose build-arg and environment variable,
matching XBOW's `common.mk`. The same benchmark always produces the same flag,
on every boot and every host. Compare exactly; do not fuzzy-match.

```bash
./xbow flag XBEN-093-24       # works offline, before hosting
```

The flag is also stored per hosted target in `.xbow/state.json`.

### Score a flag

```bash
./xbow validate XBEN-093-24 'FLAG{...}'
# {"benchmark_id": "XBEN-093-24", "valid": true}
```

`./xbow validate` exit codes: `0` valid, `1` invalid, `2` API/usage error.

For HTTP access, the shareable client, and the sidecar lifecycle, see
[validation-api.md](validation-api.md).

## Reset between attempts

After a solver has modified a target, wipe it back to a pristine state without
rebuilding:

```bash
./xbow reset XBEN-093-24
./xbow reset --all
```

`reset` drops the container, its volumes, and the network, then recreates the
services from the image already on disk. The published port, flag, and URL stay
the same. It does not re-clone or re-patch; if the `targets/<ID>/` working copy
is gone, `reset` asks you to use `up <ID> --restart`.

See [operations.md](operations.md#lifecycle) for the reset/restart/down
comparison and cleanup details.

## Tear down

```bash
./xbow down XBEN-093-24           # one target
./xbow down XBEN-093-24 --no-purge
./xbow down --all                 # every target
```

`down` removes the target's containers, volumes, network, built images,
working copy, and state entry. When the last target goes down, it also stops
the validation sidecar and sweeps orphan `xben-*` artifacts. By default it
prunes dangling images and the shared build cache; `--no-purge` keeps that
cache for faster rebuilds.

See [operations.md](operations.md#disk-and-cleanup) for exactly what is kept.

## Common recipes

```bash
# Browse level-3 SSRF-ish targets
./xbow list --level 3 --search ssti

# Host, score, reset, remove
./xbow up XBEN-093-24
./xbow validate XBEN-093-24 "$(./xbow flag XBEN-093-24)"
./xbow reset XBEN-093-24
./xbow down XBEN-093-24 --no-purge

# Host two benchmark boxes at once
./xbow up XBEN-001-24 XBEN-004-24
./xbow ps

# Full cleanup
./xbow down --all --no-purge
```
