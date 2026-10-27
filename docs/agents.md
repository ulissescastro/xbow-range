# AI agent operation

Back to the [README](../README.md) · See also [usage](usage.md) and the full
skill at [`.agents/skills/xbow-range/SKILL.md`](../.agents/skills/xbow-range/SKILL.md).

Every xbow command is non-interactive, so an AI coding CLI can operate the
range directly. The bundled Agent Skill provides the workflows, golden rules,
lifecycle, failure recovery, and reporting format.

## Load the skill

- **Pi** — project `.agents/skills/` is discovered automatically. Force it with
  `/skill:xbow-range`. After editing the skill during a session, run `/reload`.
- **Claude Code** — `.claude/skills/xbow-range` is a relative symlink to the
  skill. Start Claude Code from the repo root in a new session.
- **Other harnesses** — add `SKILL.md` to the harness's skills directory or
  include it in the system prompt. Keep the directory name and frontmatter
  `name` identical.

For a user-level install:

```bash
mkdir -p ~/.agents/skills
ln -s /path/to/xbow-range/.agents/skills/xbow-range ~/.agents/skills/xbow-range
```

## Ask in plain language

The skill routes these to the right commands:

```text
Host XBEN-032-24 and report its URL and expected flag.

List level 3 benchmarks tagged ssti.

Validate FLAG{...} for XBEN-093-24.

Reset XBEN-093-24 and confirm its flag still validates.

Host XBEN-001-24 and XBEN-004-24 together, then show ./xbow ps.

Tear down everything when I'm done.
```

## Machine interfaces

```bash
./xbow list --json                  # source + benchmark catalog
./xbow info <ID> --json             # metadata + hosted state
./xbow validate <ID> <FLAG>         # exit 0 valid, 1 invalid, 2 error
./xbow ps                           # human-readable table + API URL
```

Agents can also use the local HTTP API directly:

```bash
curl -s "$(./xbow api)" -H 'Content-Type: application/json' \
  -d '{"benchmark_id":"XBEN-093-24","flag":"FLAG{...}"}'
```

## Integrity while benchmarking

When acting as the solver or test subject, do not read the expected flag from
`targets/`, `docker inspect` env, `.xbow/state.json`, `.xbow/api/state.json`, or
`.xbow/validator.json`. Attack the published URL. As the operator, use
`./xbow flag` and `./xbow validate` for scoring.

Host on trusted LANs only; the benchmarks are intentionally vulnerable.
