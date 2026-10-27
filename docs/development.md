# Development

Back to the [README](../README.md) · See also [catalog](catalog.md) and
[operations](operations.md).

## Layout

```text
xbow.py                  # CLI: catalog, compose rewrite, lifecycle, sidecar
xbow                     # ./xbow wrapper (python3 xbow.py "$@")
validate_flag.py         # standalone flag-validation client (stdlib only)
validator/               # independent flag-validation sidecar
  Dockerfile
  app.py
catalog/                 # committed offline benchmark metadata
  benchmarks.json
  LICENSE
  NOTICE
  SOURCE.md
scripts/sync_catalog.py  # maintainer-only catalog regeneration via gh
.agents/skills/xbow-range/SKILL.md   # agent skill for AI coding CLIs
requirements.txt         # pyyaml
.xbow/                   # runtime state (gitignored)
targets/<ID>/            # per-target working copy (gitignored)
```

## Local checks

```bash
python3 -m py_compile xbow.py validate_flag.py validator/app.py scripts/sync_catalog.py

./xbow list --ids | wc -l          # 104
./xbow list --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["count"])'
./xbow info XBEN-093-24
python3 scripts/sync_catalog.py --check
```

Smoke test the full lifecycle with a small benchmark:

```bash
./xbow up XBEN-093-24
./xbow ps
./xbow validate XBEN-093-24 "$(./xbow flag XBEN-093-24)"
./xbow reset XBEN-093-24
./xbow validate XBEN-093-24 "$(./xbow flag XBEN-093-24)"
./xbow down XBEN-093-24 --no-purge
```

Check that `list` does not depend on the repo cache:

```bash
mv .xbow/repo /tmp/xbow-repo-cache
./xbow list --json >/dev/null && echo "offline catalog OK"
mv /tmp/xbow-repo-cache .xbow/repo
```

## Catalog

The catalog is generated, committed, and static. Regenerate it only when you
intentionally move the pinned fork snapshot:

```bash
python3 scripts/sync_catalog.py
python3 scripts/sync_catalog.py --check
```

This requires authenticated `gh`, but `gh` is not a runtime dependency of
xbow. See [catalog.md](catalog.md) for the schema, source tag/commit, and
attribution requirements.

## Validation sidecar

The sidecar is built from `validator/`. `xbow.py` fingerprints
`validator/app.py` and `validator/Dockerfile`; if either changes, the next
`up` (or `reset`) rebuilds the image automatically. The container runs with
host networking, a read-only root, and a read-only mount of `.xbow/api`.

The HTTP server is stdlib-only, so it can also be run directly for
development:

```bash
PORT=8800 XBOW_STATE_DIR=.xbow/api python3 validator/app.py
```

## Publishing

The repository is published under `ulissescastro/xbow-range`, `main` branch.
The first commit is the initial import; subsequent changes are committed
directly to `main`.

```bash
git status
git add <files>
git commit -m "..."
git push
```

Release hygiene:

- Keep `catalog/LICENSE` and `catalog/NOTICE` with the catalog.
- Keep the root `LICENSE` (Apache-2.0) and `NOTICE`.
- Do not commit `.xbow/`, `targets/`, `.pi/`, or `__pycache__/`.
- When moving the benchmark snapshot: update `BENCHMARKS_REPO`,
  `BENCHMARKS_REF`, and `BENCHMARKS_COMMIT` in `xbow.py`, regenerate the
  catalog, and verify `up` still builds.
