# xbow-range

Host XBOW validation benchmarks on your local network.

`xbow-range` clones a benchmark, bakes in its deterministic flag, builds the
vulnerable container(s), and publishes the web service on this host's LAN IP at
a random high port. It is a **target host only** — it does not solve, scan, or
run any agent.

## Requirements

Docker + Docker Compose, `git`, `python3`, and PyYAML
(`pip install -r requirements.txt`).

## Quick start

```bash
./xbow list                       # browse the offline catalog
./xbow info XBEN-032-24           # metadata + live state
./xbow up XBEN-032-24             # build and host a target
./xbow ps                         # URL, flag, uptime
./xbow validate XBEN-032-24 'FLAG{...}'
./xbow down XBEN-032-24           # stop and remove it
```

`up` prints the target URL, its deterministic flag, and the validation API URL.

## Commands

| Command | Purpose |
|---|---|
| `list` | Browse benchmarks (`--json`, `--ids`, `--full`, `--level`, `--tag`, `--search`) |
| `info <ID>` | Full metadata + live hosting state |
| `up <ID> [<ID> …]` | Build and host targets |
| `ps` | Show hosted targets |
| `flag <ID>` | Print the expected flag |
| `url <ID>` | Print target URL(s) |
| `reset <ID> [<ID> …]` | Wipe runtime state; same image and port |
| `down <ID> [<ID> …]` | Stop and remove targets |
| `api` / `validate <ID> <FLAG>` | Validation API URL / score a flag |

Use `down --all` to remove every target. Full reference:
[docs/usage.md](docs/usage.md).

## Flag validation API

One endpoint scores captured flags while at least one target is up:

```bash
./xbow validate XBEN-032-24 'FLAG{...}'
```

See [docs/validation-api.md](docs/validation-api.md) for the HTTP contract,
client, permissions, and lifecycle.

## Using an AI coding agent

A portable Agent Skill ships at
[`.agents/skills/xbow-range/SKILL.md`](.agents/skills/xbow-range/SKILL.md).
Pi discovers it automatically, and `.claude/skills/xbow-range` links to it for
Claude Code. Load it and ask in plain language:

> Host XBEN-032-24, give me its URL and expected flag, then validate
> `FLAG{...}` for it.

Machine-readable commands are `./xbow list --json`, `./xbow info <ID> --json`,
and `./xbow validate <ID> <FLAG>`. See [docs/agents.md](docs/agents.md).

## Documentation

- [Usage and workflows](docs/usage.md)
- [Flag validation API](docs/validation-api.md)
- [Benchmark catalog](docs/catalog.md)
- [Operations and lifecycle](docs/operations.md)
- [Development](docs/development.md)
- [AI agent operation](docs/agents.md)

## Notes

- Flags are deterministic — `FLAG{sha256(BENCHMARK_ID)}` — and the target
  URLs are solution keys. Host on trusted LANs only.
- Benchmarks are intentionally vulnerable. Do not expose them to the internet.

## License

Apache-2.0. Benchmark metadata is derived from the XBOW Validation Benchmarks
project; see [NOTICE](NOTICE) and [`catalog/`](catalog/).
