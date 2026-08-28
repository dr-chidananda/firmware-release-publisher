# Firmware Release Publisher

Release engineering rotated the firmware code-signing key. Since the rotation,
every release bundle the publisher submits to the distribution gateway is
rejected with `UNTRUSTED_SIGNATURE`, because bundles are still signed with the
now-revoked key.

Write a publisher that reconciles the build manifest, signs each publishable
release bundle with the key that is currently in force, submits it to the
provided distribution gateway, and records what it published so a re-run
never double-publishes.

## Deliverable

Create exactly one file:

```
/app/publisher/release-publisher.mjs
```

It must run with:

```
cd /app && npm run report
```

which is defined in `/app/package.json` as
`node publisher/release-publisher.mjs --report`. Do not rename the script,
change the npm script, or add other entry points -- the grader invokes
`npm run report` and nothing else.

## Environment (already in place under `/app`)

| Path | What it is |
| --- | --- |
| `/app/fixtures/build_manifest.csv` | The raw input you must reconcile. |
| `/app/reports/publications.expected.txt` | The golden output your program must reproduce (see "Output format" -- the `RECEIPT` value is masked when compared). |
| `/app/package.json` | Defines `npm run report` and the pinned `duckdb` dependency (already installed). |
| `/app/distribution-gateway/` | The provided Express service. **Do not modify it.** |
| `/app/keys/current/current.key.pem`, `/app/keys/current/current.cert.pem` | The signing keypair currently in force. |
| `/app/keys/revoked/revoked.key.pem`, `/app/keys/revoked/revoked.cert.pem` | The old, rotated-out keypair. Signing with it will always be rejected -- never use it. |
| `/app/publisher/` | Empty. This is where `release-publisher.mjs` goes. |

You create `/app/releases.duckdb` at run time; it does not exist beforehand.

During grading, the gateway is already running at `http://127.0.0.1:7070`
before `npm run report` is invoked. While developing, start it yourself in a
second shell:

```
cd /app/distribution-gateway && node server.js
curl -s http://127.0.0.1:7070/healthz
```

### Manifest schema (`build_manifest.csv`)

```
entry_id,bundle_id,component_id,version,size_bytes,record_type,supersedes_id,recorded_at
```

- `record_type` is `BUILD` or `WITHDRAWAL`.
- On a `WITHDRAWAL` row, `supersedes_id` holds the `entry_id` of the `BUILD`
  row it cancels.

### Gateway contract

Base URL `http://127.0.0.1:7070`.

- `GET /v1/signing-key/current` -> `{ key_id, algorithm, certificate_ref, status }`.
  Use `key_id` in your output; don't hardcode it.
- `POST /v1/publications` with body `{ descriptor, signature, request_token }` ->
  on success `{ publication_id, request_token, status: "PUBLISHED" }`; on a
  signature that doesn't verify, `{ error: "UNTRUSTED_SIGNATURE" }`. Re-posting
  a previously-accepted `request_token` returns the original receipt instead
  of creating a new publication.
  `descriptor` may be sent as the exact canonical JSON string you signed
  (recommended) or as an object the gateway will re-canonicalize itself --
  sending it as a string is the only way to guarantee the bytes verified are
  the bytes you signed.

Full endpoint details: `environment/distribution-gateway/README.md`.

## Reconciliation rules (binding)

Derive the set of publishable bundles from the manifest with SQL:

1. **Collapse exact duplicates.** Two rows identical across *every* column
   (`entry_id, bundle_id, component_id, version, size_bytes, record_type,
   supersedes_id, recorded_at`) are the same record emitted twice -- keep one.
   Rows that merely share an `entry_id` but differ in any other column are
   **not** duplicates and must not be collapsed.
2. **Apply withdrawals.** A `WITHDRAWAL` row cancels exactly the `BUILD` row
   whose `entry_id` equals the `WITHDRAWAL`'s `supersedes_id`. There is no
   partial withdrawal and no other matching rule (not by component, version,
   or size) -- match on `entry_id` only.
3. A bundle is **publishable** if, after steps 1-2, at least one `BUILD` row
   for it survives. A bundle whose every build was withdrawn is skipped
   entirely -- it must not appear anywhere in your output, your submissions to
   the gateway, or your database.

For each publishable bundle, compute:
- `artifact_count` -- the number of surviving `BUILD` rows.
- `total_bytes` -- the sum of their `size_bytes`.

Nothing about the manifest, the golden output, or these numbers may be
hardcoded: your program must derive them from `fixtures/build_manifest.csv` at
run time, so it stays correct if the CSV's contents change.

## Signing

For each publishable bundle, build the **descriptor**:

```
{"artifact_count": <int>, "bundle_id": "<string>", "total_bytes": <int>}
```

encoded as canonical UTF-8 JSON: object keys sorted lexicographically, no
insignificant whitespace (e.g.
`{"artifact_count":9,"bundle_id":"BND-101","total_bytes":1201575}`).

Sign that exact byte string as a detached CMS signature with the **current**
key:

```
openssl cms -sign \
  -in <file containing the exact descriptor bytes> \
  -signer /app/keys/current/current.cert.pem \
  -inkey  /app/keys/current/current.key.pem \
  -md sha256 -outform PEM -binary
```

The bytes you sign and the bytes you send as `descriptor` must be identical.
If you send the descriptor as a JSON object instead of the exact string you
signed, the gateway's own re-encoding may differ by even one character and
verification will fail.

## Submitting and persisting

For each publishable bundle, in ascending `bundle_id` order:

1. `POST /v1/publications` with `{ descriptor, signature, request_token }`,
   where `request_token` is the deterministic value `token-<bundle_id>`
   (e.g. `token-BND-101`).
2. Persist, in `/app/releases.duckdb`, enough state to answer "did I already
   publish this bundle, and what receipt did I get?" on a later run --
   at minimum the `bundle_id`, `request_token`, the gateway's
   `publication_id`, and the `key_id` you signed with.
3. On a re-run, a bundle you already have a stored `PUBLISHED` receipt for
   must **not** be signed or submitted again -- reuse the stored receipt. This
   must hold whether `releases.duckdb` still exists or was deleted and
   recreated: the gateway also recognizes a repeated `request_token` and
   replays its own receipt, so re-submitting the same token is safe even
   without local state, but you should still avoid unnecessary re-signing and
   re-submission when your own database already has the answer.

You may only interact with the gateway over HTTP. Do not read or write
`distribution-gateway/data/gateway.json` directly, and do not add any code
path that skips or short-circuits signature verification.

## Output format

Print exactly two lines per publishable bundle, in ascending `bundle_id`
order, and nothing else on stdout:

```
BUNDLE <bundle_id> SIGNED KEY=<key_id>
BUNDLE <bundle_id> PUBLISHED RECEIPT=<publication_id> TOKEN=<request_token> STATUS=<status>
```

`<key_id>` is whatever `GET /v1/signing-key/current` returned. `<status>`
should be `PUBLISHED` for every line -- if a submission is ever rejected, that
is a bug in your key handling, not an outcome to print and move past.

## Definition of done

- `npm run report`, run from `/app`, reproduces
  `reports/publications.expected.txt` line-for-line and in the same order,
  except for the `RECEIPT=` value (the gateway mints a new random one each
  time the underlying ledger is empty; the grader masks this field before
  comparing).
- The publishable-bundle set is exactly the bundles that still have at least
  one surviving `BUILD` row after reconciliation -- no bundle whose every
  build was withdrawn appears anywhere in the output.
- Every submission is `PUBLISHED`, not `UNTRUSTED_SIGNATURE` -- you signed with
  `keys/current/`, never `keys/revoked/`.
- `/app/releases.duckdb` exists after a run and contains the receipts and
  request tokens for every publishable bundle.
- Running `npm run report` a second time, without deleting anything, produces
  byte-identical stdout and does not create any additional publication on the
  gateway.
