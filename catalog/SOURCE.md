# Benchmark catalog source

- Repository: https://github.com/ulissescastro/validation-benchmarks
- Ref: `xbow-snapshot-2026-10-27`
- Commit: `7f64b30838acae11673d812204ee149172044114`
- Generated: 2026-10-27T22:35:50.784582+00:00
- Benchmarks: 104
- License: Apache-2.0

`benchmarks.json` is derived from `benchmarks/*/benchmark.json` in the pinned
fork snapshot above. The `canaries` field is intentionally omitted; all other
display fields are preserved (whitespace trimmed). `LICENSE` and `NOTICE` are
copied verbatim from the upstream project and must be kept with the catalog.

Regenerate with:

```bash
python3 scripts/sync_catalog.py
```
