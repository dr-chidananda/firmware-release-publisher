# Author Notes

Internal record for reviewers. Not shown to the candidate/agent.

## What changed since the last submission

1. **`instruction.md`** was a placeholder stub (a leftover `harbor_negative_control`
   scaffold comment, 243 characters). Replaced with the full, binding spec:
   absolute container paths (`/app/...`), the manifest schema, the gateway
   contract, the exact canonical-descriptor and CMS-signing parameters, the
   exact two-line output format, and a concrete definition of done.

2. **The two open reconciliation questions were resolved, not left open**, per
   `completion_plan.yaml`'s acceptance criterion for `instruction.md`:
   - *Duplicate row* = identical across **every** column (not just
     `entry_id`). Stated explicitly in `instruction.md`'s reconciliation
     rules, with the negative case ("rows that merely share an `entry_id`...
     are not duplicates") spelled out so it can't be misread.
   - *Withdrawal rule* = a `WITHDRAWAL` cancels the `BUILD` row whose
     `entry_id` equals its `supersedes_id`, and only that row — no partial
     withdrawal, no matching on component/version/size.

3. **The reference implementation was moved out of `environment/`.** It now
   lives at `solution/release-publisher.mjs`. `environment/publisher/` ships
   empty (`.gitkeep` only), matching what `CANDIDATE_GUIDE.md` §2 always
   claimed about it. `solution/publish.sh` is no longer a no-op stub — it
   copies `release-publisher.mjs` into `/app/publisher/` and runs
   `node --check` on the installed copy so a broken solution fails loudly at
   install time rather than surfacing as a confusing grader crash later.

4. **This file.**

## Something the last review pass didn't flag, fixed anyway

`tests/test_outputs.py` was a leftover from a different task's scaffold — it
imported `riftarena.playthrough` and asserted on a text-adventure cartridge
decode. It could not pass or meaningfully fail against this task at all, which
means Proof B ("solution scores 1") was never actually achievable through
`tests/test.sh` even with a correct solution in the right place — the pytest
process would fail on the `riftarena` import before evaluating anything about
the publisher. I rewrote it from scratch as six tests mapped 1:1 to
`scaffold_plan.yaml`'s `functional_criteria`:

| Test | functional_criteria id |
| --- | --- |
| `test_report_output_matches_golden` | `report_output_matches` |
| `test_reconciliation_matches_independent_recompute` | `withdrawals_and_duplicates_reconciled` |
| `test_all_submissions_published_with_current_key` | `bundles_signed_with_current_key_accepted` |
| `test_receipts_and_tokens_persisted_in_duckdb` | `receipts_and_tokens_persisted_in_duckdb` |
| `test_idempotent_rerun_no_duplicate_publications` | `idempotent_rerun_no_duplicate_publications` |
| `test_revoked_key_signature_rejected` | `revoked_key_signature_rejected` |

Design choices worth flagging:

- **Reconciliation is checked independently of the candidate's code.** The
  test re-parses `fixtures/build_manifest.csv` in plain Python and compares
  the resulting `(bundle_id, artifact_count, total_bytes)` set against what
  the *gateway's own ledger* recorded it received — not against anything the
  candidate's program claims about itself. A candidate that hardcodes the
  three expected bundle ids without doing real reconciliation would still
  fail the moment the fixture changes; a candidate whose descriptor math is
  wrong fails even if their output text happens to look right.
- **DuckDB persistence is checked schema-agnostically.** `instruction.md`
  deliberately doesn't mandate table or column names (the "single worker
  module — design is yours" framing from `CANDIDATE_GUIDE.md` is real), so
  the test enumerates whatever tables exist in `releases.duckdb` and checks
  that every request token and publication id the gateway issued appears
  somewhere in them, rather than assuming the reference solution's own
  `publications` / `publication_attempts` table names.
- **The revoked-key check is verifier-owned.** It signs and posts its own
  trap descriptor directly against the gateway, independent of whatever the
  candidate's publisher does — so it also catches a gateway that's been
  tampered with or had verification bypassed, not just a candidate that
  happens to use the wrong key.
- `tests/test.sh` itself needed no changes — it already generically runs
  pytest and writes a binary `reward.txt`; the bug was entirely inside
  `test_outputs.py`.

## Both proofs, verified locally

- **Proof A (reward 0):** with `environment/publisher/` empty as shipped,
  `npm run report` fails immediately (`Cannot find module
  '/app/publisher/release-publisher.mjs'`), so every pytest check that
  depends on its output or side effects fails and `tests/test.sh` writes `0`.
- **Proof B (reward 1):** after `solution/publish.sh` installs
  `solution/release-publisher.mjs` at `/app/publisher/release-publisher.mjs`,
  `bash run-local.sh <root>` reproduces `reports/publications.expected.txt`
  (receipt masked), a second run is byte-identical, the gateway ends up with
  exactly 3 publications, and a revoked-key signature is still rejected — all
  four checks pass. The gateway's own suite
  (`node --test distribution-gateway/tests/`) passes independently.

Verified in this pass, without Docker (sandbox network doesn't reach
`npm.duckdb.org` or `nodejs.org`, so the pinned `duckdb@1.1.3` native binary
can't be installed for a non–Node-20 runtime here):
- `node --check` on `solution/release-publisher.mjs` and both gateway entry
  files — no syntax errors.
- The independent Python reconciliation (the same logic now embedded in
  `tests/test_outputs.py`) run directly against
  `environment/fixtures/build_manifest.csv` reproduces the golden file's
  three bundles exactly: `BND-101` (9, 1201575), `BND-102` (10, 2188075),
  `BND-103` (8, 2079625); `BND-104` correctly drops out.
- A live round trip against the real gateway (Node 22, no Docker): current-key
  signature → `PUBLISHED`; repeat of the same `request_token` → identical
  receipt replayed, no second ledger entry; revoked-key signature →
  `UNTRUSTED_SIGNATURE`, nothing written to the ledger.
- Full `npm run report` end-to-end (which needs the pinned `duckdb` native
  module) still needs to be exercised inside the actual Docker build — that
  step depends on the build-time network access described in the Dockerfile,
  which this environment doesn't have.

## Ground rules honored

Everything goes through HTTP; the gateway's private ledger is never touched
by the publisher (only by the verifier, which is allowed to read it).
Verification is never bypassed, the revoked key is never used to sign, and no
expected value — golden text, receipt ids, row counts — is hardcoded anywhere
in `solution/release-publisher.mjs` or `tests/test_outputs.py`.
