# Benchmark catalog

Back to the [README](../README.md) · See also [usage](usage.md) and
[development](development.md).

`./xbow list` and `./xbow info` read a committed, offline catalog. They make no
git clone, GitHub API, or network calls; the catalog ships with the repo.

## Files

```text
catalog/
  benchmarks.json   # 104 entries: name, description, level, tags, win_condition
  LICENSE           # Apache-2.0, copied from upstream
  NOTICE            # upstream attribution
  SOURCE.md         # repo, tag, commit, generation time
```

## Source

The catalog is generated from the pinned fork snapshot:

```text
repo   ulissescastro/validation-benchmarks
tag    xbow-snapshot-2026-10-27
commit 7f64b30838acae11673d812204ee149172044114
```

That tag is also what `up` clones, so the catalog and the source trees stay in
lock-step. The snapshot is static by design; upstream changes do not appear
until the snapshot and catalog are intentionally updated.

## Schema

```json
{
  "version": 1,
  "generated_at": "2026-10-27T22:35:50.784582+00:00",
  "source": {
    "repo": "ulissescastro/validation-benchmarks",
    "url": "https://github.com/ulissescastro/validation-benchmarks",
    "ref": "xbow-snapshot-2026-10-27",
    "commit": "7f64b30838acae11673d812204ee149172044114",
    "license": "Apache-2.0"
  },
  "benchmarks": [
    {
      "id": "XBEN-001-24",
      "name": "XBEN-001-24 IDOR - Trading Platform - Authorization",
      "description": "Every time you see a value that looks like an ID, ...",
      "level": 2,
      "tags": ["idor", "default_credentials"],
      "win_condition": "flag"
    }
  ]
}
```

The upstream `canaries` field is intentionally omitted. All display fields are
preserved with surrounding whitespace trimmed. Names keep their `XBEN-…`
prefix in the file; the CLI strips it for display.

## Browsing

```bash
./xbow list                        # rich output, descriptions truncated
./xbow list --full                 # full descriptions
./xbow list --ids                  # IDs only
./xbow list --json                 # source + benchmarks as JSON
./xbow list --level 3              # level filter
./xbow list --tag ssti             # tag filter
./xbow list --search jwt           # search ID/name/description/tags
./xbow info XBEN-093-24            # full metadata + live state
```

`list --json` is the recommended interface for agents and other tools. See
[usage](usage.md#browse-the-catalog) for output examples.

## Regenerating

This is a maintainer action, not part of the runtime. It requires an
authenticated `gh` CLI with access to the fork:

```bash
python3 scripts/sync_catalog.py            # rewrite catalog/
python3 scripts/sync_catalog.py --check    # verify catalog matches the fork
python3 scripts/sync_catalog.py --help
```

The script resolves the ref to a commit, reads every
`benchmarks/*/benchmark.json` through the GitHub API, validates that there are
104 entries with the required fields, and writes `benchmarks.json`,
`LICENSE`, `NOTICE`, and `SOURCE.md`.

Useful options:

```bash
--repo owner/repo
--ref  <branch|tag|commit>
--expect 104          # 0 disables the count check
--jobs 8              # parallel metadata fetches
--check               # compare, do not write
```

`--check` ignores `generated_at` and compares the source commit plus benchmark
entries, so it is safe to use in CI or before a release.

If the catalog commit differs from the commit hardcoded in `xbow.py`
(`BENCHMARKS_COMMIT`), `list`/`info` print a warning until you regenerate.

## Attribution

The benchmark metadata is derived from the XBOW Validation Benchmarks project
(Copyright 2024- XBOW USA Inc., Apache-2.0). `catalog/LICENSE` and
`catalog/NOTICE` are copied verbatim from upstream and must be kept with the
catalog. The project-level `LICENSE` and `NOTICE` at the repo root cover
xbow-range itself.
