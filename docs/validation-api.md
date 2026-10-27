# Flag validation API

Back to the [README](../README.md) · See also [usage](usage.md) and
[operations](operations.md).

While at least one target is hosted, xbow runs an independent sidecar
container, `xbow-validator`. It exposes **one endpoint** so anyone on the LAN
can check a captured flag without shell access to the host.

## Contract

```text
POST /validate
{"benchmark_id": "XBEN-093-24", "flag": "FLAG{d6fa...}"}

200 {"benchmark_id": "XBEN-093-24", "valid": true}
200 {"benchmark_id": "XBEN-093-24", "valid": false}
200 {"benchmark_id": "XBEN-093-24", "valid": false, "reason": "not_hosted"}
200 {"benchmark_id": "XBEN-093-24", "valid": false, "reason": "not_running"}
```

`GET /validate?benchmark_id=...&flag=...` is equivalent and handy with `curl`.
`GET /validate` with no parameters lists the hosted benchmarks:

```json
{"hosted": ["XBEN-093-24", "XBEN-032-24"], "count": 2}
```

The response is always a boolean plus an optional `reason`:

| Result | Meaning |
|---|---|
| `{"valid": true}` | Exact match for a hosted, running target |
| `{"valid": false}` | Wrong flag |
| `reason: "not_hosted"` | Benchmark is not currently hosted |
| `reason: "not_running"` | Hosted entry exists, but the target port is down |
| `reason: "unknown_benchmark"` | Malformed ID (not `XBEN-NNN-NN`) |

The API URL is printed by `up`, shown by `ps`, and available via `./xbow api`.

## Score with curl

```bash
API=$(./xbow api)
FLAG=$(./xbow flag XBEN-093-24)

curl -s "$API" -H 'Content-Type: application/json' \
  -d "{\"benchmark_id\":\"XBEN-093-24\",\"flag\":\"$FLAG\"}"
# {"benchmark_id": "XBEN-093-24", "valid": true}

curl -s "$API"
# {"hosted": ["XBEN-093-24"], "count": 1}
```

`./xbow validate <ID> <FLAG>` does the same from the host and exits `0` valid,
`1` invalid, `2` API/usage error.

## Shareable client

Hand out **`validate_flag.py`** — one self-contained file, standard library
only:

```bash
./validate_flag.py --api http://192.168.0.101:34493/validate XBEN-093-24 "$FLAG"
# VALID   XBEN-093-24        (exit 0)

./validate_flag.py --api 192.168.0.101:34493 --list
XBOW_API_URL=http://192.168.0.101:34493/validate ./validate_flag.py XBEN-093-24 "$FLAG"
```

Exit codes are `0` valid, `1` invalid, `2` connection/usage error. `--json`
prints the raw response. Pass `--token` (or set `XBOW_VALIDATOR_TOKEN`) if the
API requires auth.

## Authentication

Auth is off by default. To enable a bearer token, export it before the sidecar
first starts:

```bash
export XBOW_VALIDATOR_TOKEN=change-me
./xbow up XBEN-093-24
```

Changing the token later requires recreating the container:

```bash
docker rm -f xbow-validator
./xbow up XBEN-093-24
```

Loopback (the container healthcheck) is always exempt.

## Behaviour and limits

- **Independent** — not part of any target's compose project, so `reset`,
  `down -v`, or `--rmi local` on a target never touches it.
- **Dynamic** — reads `.xbow/api/state.json`, which xbow rewrites atomically on
  every `up`/`down`/`reset`. Adding or removing targets needs no API restart.
- **Only while needed** — started after the first target comes up, removed
  when the last one goes down.
- **Liveness-aware** — TCP-probes the target's published port before answering;
  a stopped container returns `not_running`.
- **No leak** — the expected flag is never returned, submitted flags are never
  logged, and comparison is constant-time (`hmac.compare_digest`).
- **Bounded** — 4 KB request bodies (1 KB max flag), 30 req/min per source IP
  (loopback exempt), optional bearer auth.

The sidecar runs with `--network host` (so it can probe targets), `--read-only`
root, and a read-only mount of `.xbow/api`. It is Linux-only, like the rest of
the tool. Its port is persisted in `.xbow/validator.json` and reused across
restarts while free.

## Surviving `docker prune`

The sidecar is disposable and self-healing. Host state (`.xbow/state.json`,
`.xbow/validator.json`, `.xbow/api/`, `targets/`) is plain files and survives any
Docker cleanup. The next `up` or `reset` notices the container is missing and
recreates it — rebuilding the image from `validator/` if it was removed too:

| What was pruned | Next `./xbow up <ID>` / `reset <ID>` |
|---|---|
| running sidecar | no (running containers are never pruned) |
| stopped sidecar container | recreates it, same port and image |
| sidecar image (`prune -a`) | rebuilds it locally, same port |
| target containers/images | rebuilds the target from the cached repo |

The only rebuild input fetched from outside is the base image
(`python:3.12-alpine`); if that was pruned too, Docker pulls it. Note that
`ps`, `api`, and `validate` only report — run `up <ID>` (a no-op for an
already-running target) or `reset <ID>` to heal the sidecar.
