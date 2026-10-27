# Operations and lifecycle

Back to the [README](../README.md) · See also [usage](usage.md) and
[validation-api](validation-api.md).

## How `up` works

1. **Clone** — sparse checkout of one benchmark from
   `github.com/ulissescastro/validation-benchmarks` at the pinned
   `xbow-snapshot-2026-10-27` tag. The shared clone is cached under
   `.xbow/repo`; only the requested benchmark is materialized into
   `targets/<ID>/`.
2. **Compute flag** — `FLAG{sha256(ID_UPPERCASE)}`.
3. **Rewrite compose** — forces `linux/amd64`, injects the flag as a build-arg
   and environment variable, and republishes every `ports:` entry on `0.0.0.0`
   at a fresh random high port. Internal services that use only `expose:`
   (databases, for example) stay private.
4. **Build + start** — `docker compose build`, then `up -d --wait`.
5. **Report** — detects the host's LAN IP, prints
   `http://<lan-ip>:<port>` plus the flag, and records state so `ps`/`down`
   work across runs and for multiple concurrent targets.
6. **Validation API** — ensures the `xbow-validator` sidecar is running and
   prints its `/validate` URL. The sidecar is removed when the last target goes
   down.

The source cache records the pinned repo and commit. If either changes, the
next `up` rebuilds `.xbow/repo` automatically.

### End-of-life Debian base images

Many benchmarks build on Debian releases that are now end-of-life (buster,
bullseye, …). Once a release is archived, `deb.debian.org` and
`security.debian.org` prune it and `apt-get install` fails with 404s. Before
building, `up` automatically repoints apt to `archive.debian.org` and disables
the stale `Check-Valid-Until` check in any Dockerfile on an EOL Debian base.
This is faithful: the base image and installed packages are unchanged, only the
mirror URL is. It edits the throwaway `targets/<ID>/` copy only; the cached repo
stays pristine. When it fires, `up` prints:

```text
  apt->archive.debian.org fix applied (EOL Debian base): app/Dockerfile
```

## State files

| Path | Meaning |
|---|---|
| `.xbow/state.json` | Hosted targets: URL, flag, ports, compose paths, timestamps |
| `.xbow/validator.json` | Sidecar port, image hash, start/stop times |
| `.xbow/api/state.json` | Minimal read-only view mounted into the sidecar |
| `.xbow/repo/` | Sparse cached clone of the benchmark repo |
| `targets/<ID>/` | Per-target throwaway working copy while hosted |
| `catalog/benchmarks.json` | Committed offline metadata (not runtime state) |

Only `.xbow/` and `targets/` are runtime state; both are gitignored. The
catalog is committed.

## Lifecycle

| Command | Image | Port | Runtime changes | Speed |
|---|---|---|---|---|
| `reset` | kept | **same** | wiped | seconds |
| `up --restart` | rebuilt | **new** | wiped | minutes |
| `down` | target image removed | released | wiped | — |

### Reset

`reset` runs `compose down -v` (drops the container, its volumes, and the
network, so both writable-layer changes and DB/volume data are discarded),
then recreates the services from the image already on disk. It reuses the same
rewritten compose file, so the published host port and URL are unchanged, and
because the image is kept there is no rebuild or pull.

It does not re-clone, re-patch, or re-build. If `targets/<ID>/` has been
removed, `reset` cannot recreate from it and tells you to use `up <ID>
--restart`.

### Restart

`up <ID> --restart` performs a full teardown and rebuild. It picks a new random
port and rewrites the flag in a fresh image.

### Down

`down` removes the target's containers, volumes, network, built images,
working copy, and state entry. See below for exactly what is kept.

## Disk and cleanup

`down` fully reclaims everything a target created:

- containers, volumes, and the compose network — removed (`down -v`)
- the images built for that benchmark (`xben-<id>-*`) — removed
  (`--rmi local` plus a name-based pass)
- the `targets/<ID>/` working copy — deleted
- the state entry — removed
- the `xbow-validator` container, when the **last** target goes down — removed
  (its small image is kept)

When no targets remain, `down` also sweeps leftover `xben-*` artifacts from
crashes or interrupted runs. A failed `up` rolls itself back the same way
(containers, images, network, files) and records no state.

By default `down` also prunes dangling images and the build cache globally.
Pass `--no-purge` to keep the shared cache for faster rebuilds:

```bash
./xbow down XBEN-001-24
./xbow down --all
./xbow down XBEN-001-24 --no-purge
```

Only base images (`mysql:5.7.15`, `debian:bullseye-slim`, `python:*`, …), the
single `xbow-validator:latest` image, and the `.xbow/repo` clone cache are
kept — all bounded and useful for fast re-hosting. Target images are always
removed. The build-cache prune never removes tagged images, only dangling
layers.

## Networking

- Target ports publish on `0.0.0.0`, so a target is reachable at the host's
  LAN IP (auto-detected from the default route) and on localhost.
- Target host ports are random high ports from the OS ephemeral range
  (32768–60999 on Linux).
- The validation API listens on its own port (initially random, persisted in
  `.xbow/validator.json`, reused while free) and uses host networking so it can
  probe targets on `127.0.0.1`. It is reachable on the same LAN IP.
- No firewall rules are changed. If the host has a firewall, allow the printed
  ports for LAN clients.

## Recovery and troubleshooting

- **`up` reports an error** — it already rolled back that target's containers,
  images, network, files, and state. Check the error, then `docker info` and
  available disk. Retry once before escalating.
- **`apt->archive.debian.org fix applied`** — expected for EOL Debian bases.
  Do not edit Dockerfiles; let the build continue.
- **`healthcheck wait failed; starting without --wait`** — the target was
  started detached anyway. Confirm with `./xbow ps` and
  `curl <url>/`.
- **Unknown benchmark** — run `./xbow list` (offline) or
  `./xbow list --search TERM` and use the exact ID.
- **`validate` exits 2** — the API is not running or is unreachable. Run
  `./xbow up <ID>` or `./xbow reset <ID>`, then `./xbow api`.
- **Build looks stuck** — builds can legitimately take minutes. The build
  timeout is 600 seconds and the start timeout 180 seconds. Do not start a
  second build.
- **Port or API URL changed** — re-read `./xbow ps` / `./xbow api`; do not
  cache a port across `--restart`.
- **After Docker cleanup or reboot** — host state is just files, so the range
  self-heals. Run `./xbow up <ID>` or `./xbow reset <ID>`. See
  [validation-api.md](validation-api.md#surviving-docker-prune) for sidecar
  recovery.

`ps`, `api`, and `validate` only report; they do not start containers.
