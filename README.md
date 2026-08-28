# Firmware Release Publisher

The publisher lives in `environment/publisher/release-publisher.mjs`. Run it with
`npm run report` from `environment/`, with the gateway up on 127.0.0.1:7070.

## What was wrong

The signing key was rotated and the old certificate revoked, but the publisher
kept signing with the old key, so the gateway rejected every bundle with
`UNTRUSTED_SIGNATURE`. Fixing that means signing with `keys/current/` and asking
`GET /v1/signing-key/current` for the key id instead of assuming one.

## Reconciliation

The manifest needs two things cleaned up before anything can be published:

- three rows are emitted twice (`MFR-0001`, `MFR-0007`, `MFR-0014`), identical in
  every column, so a `SELECT DISTINCT` collapses them
- `WITHDRAWAL` rows cancel the build named in `supersedes_id`

What's left after that is the set of `BUILD` rows that actually ship. Grouping
those by bundle gives the counts and byte totals that go into the descriptor:

| bundle  | artifacts | total bytes |
| ------- | --------: | ----------: |
| BND-101 |         9 |   1,201,575 |
| BND-102 |        10 |   2,188,075 |
| BND-103 |         8 |   2,079,625 |

BND-104 doesn't appear — both of its builds were withdrawn, so nothing survives
the join and it never reaches the output. None of these numbers are hardcoded;
edit the CSV and they move.

I kept the intermediate tables (`manifest_deduped`, `withdrawn_entries`,
`surviving_builds`) instead of writing one big query, mostly so I could look at
each step separately while I was getting the counts to line up.

## Signing

The descriptor is `{artifact_count, bundle_id, total_bytes}` with sorted keys and
no whitespace, signed with `openssl cms -sign` as a detached PEM signature.

The fiddly part is that the bytes signed and the bytes sent have to match
exactly. The gateway will happily re-encode a descriptor sent as an object, and
if its encoding differs from yours by a single character the signature fails
verification. So the descriptor goes over the wire as the canonical string.

## Idempotency

Each bundle is submitted under `token-<bundle_id>`, and the receipt is stored in
DuckDB along with the token, key id, descriptor and totals. Re-running finds the
stored receipt and replays it without submitting anything.

Deleting `releases.duckdb` and re-running is also safe — the gateway recognises
the token and replays its own receipt — so either way there's never more than one
publication per bundle. Every attempt, successful or not, is appended to
`publication_attempts`.

## Checking it

`run-local.sh` sets up what the Dockerfile would (both keypairs, deps, gateway)
and then checks:

```
bash run-local.sh .
```

- output matches `reports/publications.expected.txt` with the receipt masked
- a second run is byte-identical
- the gateway ends up with exactly 3 publications
- a descriptor signed with the revoked key is still rejected

All four pass. The gateway's own tests pass too
(`node --test distribution-gateway/tests/publications.test.js`).

Needs Node 20 — `duckdb@1.1.3` has no prebuilt binary for Node 22 and falls back
to a source build that fails. On macOS you also need real OpenSSL 3, since the
bundled LibreSSL has no working `cms` subcommand.

## Ground rules I stuck to

Everything goes through HTTP; `distribution-gateway/data/gateway.json` is never
touched. Verification isn't bypassed, the revoked key is never used to sign, and
no expected value is hardcoded.

## Where the reference implementation actually lives

`environment/publisher/` ships empty on purpose. The working publisher is
`solution/release-publisher.mjs`; `solution/publish.sh` installs it to
`/app/publisher/release-publisher.mjs` before the grader runs `npm run report`.
See `AUTHOR_NOTES.md` for the full author-facing writeup, including the fix to
`tests/test_outputs.py` (it previously shipped as a leftover from an unrelated
task's scaffold and could not have graded this one).
