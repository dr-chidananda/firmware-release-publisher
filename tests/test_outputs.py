"""Verifier tests for the Firmware Release Publisher task.

Each test maps to a functional_criteria[] entry in scaffold_plan.yaml. The
suite starts the provided distribution gateway as a background process,
drives `npm run report` (the candidate's/reference publisher) twice from
/app, and checks its stdout, the gateway's own ledger, and the candidate's
DuckDB file -- without assuming any particular internal schema beyond what
instruction.md makes binding.

Run via tests/test.sh, which writes /logs/verifier/reward.txt.

Note for reviewers: this file previously shipped as a leftover from a
different task's scaffold (a RiftArena cartridge-decode test importing
`riftarena.playthrough`), which could never pass or fail meaningfully against
this task. It has been replaced with the checks below, which mirror what
CANDIDATE_GUIDE.md SS5-6 and run-local.sh already describe as "how to check
yourself."
"""

from __future__ import annotations

import csv
import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path

import duckdb
import requests

APP_ROOT = Path.cwd()
GATEWAY_DIR = APP_ROOT / "distribution-gateway"
PUBLISHER_FILE = APP_ROOT / "publisher" / "release-publisher.mjs"
MANIFEST_CSV = APP_ROOT / "fixtures" / "build_manifest.csv"
GOLDEN_FILE = APP_ROOT / "reports" / "publications.expected.txt"
DB_FILE = APP_ROOT / "releases.duckdb"
LEDGER_FILE = GATEWAY_DIR / "data" / "gateway.json"

GATEWAY_URL = "http://127.0.0.1:7070"
RECEIPT_RE = re.compile(r"RECEIPT=[^ ]+")

EXPECTED_BUNDLE_IDS = ["BND-101", "BND-102", "BND-103"]


def _mask(text: str) -> str:
    return RECEIPT_RE.sub("RECEIPT=<id>", text)


def _reconcile_manifest_independently():
    """Recomputes publishable bundles straight from the raw CSV, independent
    of any candidate code, per instruction.md's binding reconciliation rules:
    collapse rows identical across every column, drop BUILD rows cancelled by
    a WITHDRAWAL's supersedes_id, keep bundles with >=1 surviving BUILD row."""
    with open(MANIFEST_CSV, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    seen = set()
    deduped = []
    for row in rows:
        key = tuple(row[col] for col in row)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)

    withdrawn_entry_ids = {
        row["supersedes_id"]
        for row in deduped
        if row["record_type"] == "WITHDRAWAL" and row.get("supersedes_id")
    }

    surviving_builds = [
        row
        for row in deduped
        if row["record_type"] == "BUILD" and row["entry_id"] not in withdrawn_entry_ids
    ]

    bundles: dict[str, dict[str, int]] = {}
    for row in surviving_builds:
        b = bundles.setdefault(row["bundle_id"], {"artifact_count": 0, "total_bytes": 0})
        b["artifact_count"] += 1
        b["total_bytes"] += int(row["size_bytes"])

    return bundles


def _wait_for_gateway(proc, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"gateway process exited early (code {proc.returncode})"
            )
        try:
            r = requests.get(f"{GATEWAY_URL}/healthz", timeout=1)
            if r.ok:
                return
        except requests.RequestException:
            pass
        time.sleep(0.25)
    raise TimeoutError("gateway did not become healthy in time")


def _run_report():
    return subprocess.run(
        ["npm", "run", "--silent", "report"],
        cwd=APP_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _read_ledger() -> dict:
    if not LEDGER_FILE.exists():
        return {"publications": {}, "tokenIndex": {}}
    return json.loads(LEDGER_FILE.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Session-wide setup: fresh state, gateway up, two consecutive report runs.
# --------------------------------------------------------------------------

import pytest  # noqa: E402  (kept after helpers for readability)


@pytest.fixture(scope="session")
def report_runs():
    DB_FILE.unlink(missing_ok=True)
    LEDGER_FILE.unlink(missing_ok=True)

    proc = subprocess.Popen(
        ["node", "server.js"],
        cwd=GATEWAY_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        _wait_for_gateway(proc)
        run1 = _run_report()
        run2 = _run_report()
        yield {"run1": run1, "run2": run2}
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        proc.wait(timeout=10)


# --------------------------------------------------------------------------
# report_output_matches
# --------------------------------------------------------------------------

def test_report_output_matches_golden(report_runs):
    """functional_criteria[id=report_output_matches]: `npm run report`
    reproduces reports/publications.expected.txt, RECEIPT masked."""
    run1 = report_runs["run1"]
    assert run1.returncode == 0, (
        f"npm run report exited {run1.returncode}\nstdout:\n{run1.stdout}\n"
        f"stderr:\n{run1.stderr}"
    )
    golden = GOLDEN_FILE.read_text(encoding="utf-8")
    assert _mask(run1.stdout.strip()) == _mask(golden.strip())


# --------------------------------------------------------------------------
# withdrawals_and_duplicates_reconciled
# --------------------------------------------------------------------------

def test_reconciliation_matches_independent_recompute(report_runs):
    """functional_criteria[id=withdrawals_and_duplicates_reconciled]: the
    published bundle set (and each bundle's artifact_count/total_bytes, as
    recorded in the gateway's own ledger) matches an independent recompute
    from the raw CSV -- not whatever the candidate's own code claims."""
    expected = _reconcile_manifest_independently()
    assert set(expected.keys()) == set(EXPECTED_BUNDLE_IDS), (
        "fixture sanity check failed -- update EXPECTED_BUNDLE_IDS if "
        "fixtures/build_manifest.csv changed"
    )

    ledger = _read_ledger()
    publications = ledger.get("publications", {})
    seen_bundle_ids = set()

    for pub in publications.values():
        descriptor = json.loads(pub["descriptor"])
        bundle_id = descriptor["bundle_id"]
        seen_bundle_ids.add(bundle_id)
        assert bundle_id in expected, f"gateway published unexpected bundle {bundle_id}"
        assert descriptor["artifact_count"] == expected[bundle_id]["artifact_count"], bundle_id
        assert descriptor["total_bytes"] == expected[bundle_id]["total_bytes"], bundle_id

    assert seen_bundle_ids == set(expected.keys()), (
        f"published bundles {sorted(seen_bundle_ids)} != "
        f"reconciled publishable bundles {sorted(expected.keys())}"
    )


# --------------------------------------------------------------------------
# bundles_signed_with_current_key_accepted
# --------------------------------------------------------------------------

def test_all_submissions_published_with_current_key(report_runs):
    """functional_criteria[id=bundles_signed_with_current_key_accepted]:
    every bundle line reports STATUS=PUBLISHED (never UNTRUSTED_SIGNATURE),
    signed with whatever key_id the gateway currently reports."""
    current = requests.get(f"{GATEWAY_URL}/v1/signing-key/current", timeout=5).json()
    key_id = current["key_id"]

    stdout = report_runs["run1"].stdout
    assert "UNTRUSTED_SIGNATURE" not in stdout

    for bundle_id in EXPECTED_BUNDLE_IDS:
        assert f"BUNDLE {bundle_id} SIGNED KEY={key_id}" in stdout
        assert (
            f"STATUS=PUBLISHED" in stdout
            and f"BUNDLE {bundle_id} PUBLISHED" in stdout
        ), f"no PUBLISHED line for {bundle_id}"


# --------------------------------------------------------------------------
# receipts_and_tokens_persisted_in_duckdb
# --------------------------------------------------------------------------

def test_receipts_and_tokens_persisted_in_duckdb(report_runs):
    """functional_criteria[id=receipts_and_tokens_persisted_in_duckdb]: after
    a run, releases.duckdb contains the gateway receipts and request tokens
    for each publishable bundle. Schema-agnostic: dumps every table rather
    than assuming particular table/column names, since instruction.md does
    not mandate a schema."""
    assert DB_FILE.exists(), "releases.duckdb was not created"

    ledger = _read_ledger()
    expected_tokens = set(ledger.get("tokenIndex", {}).keys())
    expected_pub_ids = {pub["publication_id"] for pub in ledger.get("publications", {}).values()}
    assert expected_tokens, "gateway ledger has no recorded tokens -- did the run publish anything?"

    con = duckdb.connect(str(DB_FILE), read_only=True)
    try:
        tables = [r[0] for r in con.execute("SHOW TABLES").fetchall()]
        assert tables, "releases.duckdb has no tables"

        dump = ""
        for table in tables:
            rows = con.execute(f'SELECT * FROM "{table}"').fetchall()
            dump += "\n".join(str(row) for row in rows)

        for token in expected_tokens:
            assert token in dump, f"request token {token} not found anywhere in releases.duckdb"
        for pub_id in expected_pub_ids:
            assert pub_id in dump, f"publication id {pub_id} not found anywhere in releases.duckdb"
    finally:
        con.close()


# --------------------------------------------------------------------------
# idempotent_rerun_no_duplicate_publications
# --------------------------------------------------------------------------

def test_idempotent_rerun_no_duplicate_publications(report_runs):
    """functional_criteria[id=idempotent_rerun_no_duplicate_publications]: a
    second run produces byte-identical stdout and the gateway ends up with
    exactly one publication per publishable bundle."""
    run1, run2 = report_runs["run1"], report_runs["run2"]
    assert run2.returncode == 0, run2.stderr
    assert run1.stdout == run2.stdout, "second run's output differs from the first"

    ledger = _read_ledger()
    assert len(ledger.get("publications", {})) == len(EXPECTED_BUNDLE_IDS), (
        f"expected exactly {len(EXPECTED_BUNDLE_IDS)} publications on the gateway, "
        f"found {len(ledger.get('publications', {}))} -- possible duplicate publish"
    )


# --------------------------------------------------------------------------
# revoked_key_signature_rejected
# --------------------------------------------------------------------------

def test_revoked_key_signature_rejected(report_runs, tmp_path):
    """functional_criteria[id=revoked_key_signature_rejected]: a descriptor
    signed with the revoked key is rejected as UNTRUSTED_SIGNATURE and
    creates no publication. Verifier-owned: signs and posts independently of
    whatever the candidate's publisher does, so it also guards against a
    tampered or bypassed gateway."""
    before = len(_read_ledger().get("publications", {}))

    descriptor = '{"artifact_count":1,"bundle_id":"BND-TRAP","total_bytes":100}'
    descriptor_file = tmp_path / "trap.bin"
    descriptor_file.write_bytes(descriptor.encode("utf-8"))
    sig_file = tmp_path / "trap-sig.pem"

    subprocess.run(
        [
            "openssl", "cms", "-sign",
            "-in", str(descriptor_file),
            "-signer", str(APP_ROOT / "keys" / "revoked" / "revoked.cert.pem"),
            "-inkey", str(APP_ROOT / "keys" / "revoked" / "revoked.key.pem"),
            "-md", "sha256", "-outform", "PEM", "-binary",
        ],
        stdout=sig_file.open("wb"),
        check=True,
        timeout=30,
    )

    resp = requests.post(
        f"{GATEWAY_URL}/v1/publications",
        json={
            "descriptor": descriptor,
            "signature": sig_file.read_text(encoding="utf-8"),
            "request_token": "token-BND-TRAP",
        },
        timeout=5,
    )
    body = resp.json()
    assert body.get("error") == "UNTRUSTED_SIGNATURE", body

    after = len(_read_ledger().get("publications", {}))
    assert after == before, "a revoked-key signature was accepted as a publication"
