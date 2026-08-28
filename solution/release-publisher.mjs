#!/usr/bin/env node
// Reconciles the build manifest, signs each publishable bundle with the current
// code-signing key, and submits it to the distribution gateway.
//
// Usage: npm run report   (gateway must be up on 127.0.0.1:7070)

import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import duckdb from 'duckdb';

const APP_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

const MANIFEST_CSV = path.join(APP_ROOT, 'fixtures', 'build_manifest.csv');
const DB_FILE = path.join(APP_ROOT, 'releases.duckdb');
const KEY_PEM = path.join(APP_ROOT, 'keys', 'current', 'current.key.pem');
const CERT_PEM = path.join(APP_ROOT, 'keys', 'current', 'current.cert.pem');
const GATEWAY = process.env.GATEWAY_BASE_URL || 'http://127.0.0.1:7070';

// duckdb's node binding is callback-based; wrap the two calls we need.
function connect(file) {
  const db = new duckdb.Database(file);
  const conn = db.connect();

  return {
    run: (sql, ...params) =>
      new Promise((resolve, reject) =>
        conn.run(sql, ...params, (err) => (err ? reject(err) : resolve()))
      ),
    all: (sql, ...params) =>
      new Promise((resolve, reject) =>
        conn.all(sql, ...params, (err, rows) => (err ? reject(err) : resolve(rows || [])))
      ),
    close: () =>
      new Promise((resolve) => {
        try {
          conn.close(() => db.close(() => resolve()));
        } catch {
          resolve();
        }
      }),
  };
}

// COUNT/SUM come back as BigInt, which JSON.stringify refuses to serialise.
const num = (v) => Number(v);

const quote = (s) => `'${String(s).replace(/'/g, "''")}'`;

async function createTables(db) {
  await db.run(`
    CREATE TABLE IF NOT EXISTS publications (
      bundle_id       VARCHAR PRIMARY KEY,
      request_token   VARCHAR NOT NULL,
      publication_id  VARCHAR NOT NULL,
      status          VARCHAR NOT NULL,
      key_id          VARCHAR NOT NULL,
      descriptor      VARCHAR NOT NULL,
      artifact_count  BIGINT  NOT NULL,
      total_bytes     BIGINT  NOT NULL,
      attempts        INTEGER NOT NULL,
      published_at    TIMESTAMP NOT NULL
    )
  `);

  await db.run(`
    CREATE TABLE IF NOT EXISTS publication_attempts (
      bundle_id     VARCHAR NOT NULL,
      request_token VARCHAR NOT NULL,
      attempt_no    INTEGER NOT NULL,
      outcome       VARCHAR NOT NULL,
      detail        VARCHAR,
      attempted_at  TIMESTAMP NOT NULL
    )
  `);
}

async function loadManifest(db) {
  await db.run(`
    CREATE OR REPLACE TABLE manifest_raw AS
    SELECT
      CAST(entry_id      AS VARCHAR) AS entry_id,
      CAST(bundle_id     AS VARCHAR) AS bundle_id,
      CAST(component_id  AS VARCHAR) AS component_id,
      CAST(version       AS VARCHAR) AS version,
      CAST(size_bytes    AS BIGINT)  AS size_bytes,
      CAST(record_type   AS VARCHAR) AS record_type,
      CAST(supersedes_id AS VARCHAR) AS supersedes_id,
      CAST(recorded_at   AS VARCHAR) AS recorded_at
    FROM read_csv_auto(${quote(MANIFEST_CSV)}, header = true, all_varchar = true)
  `);
}

// Two things to undo before a bundle can be published: rows the manifest emitted
// twice, and builds a later WITHDRAWAL cancelled. Whatever BUILD rows are left
// decide which bundles ship. Split into named tables so each step can be
// inspected on its own when the numbers look wrong.
async function reconcile(db) {
  await db.run(`
    CREATE OR REPLACE TABLE manifest_deduped AS
    SELECT DISTINCT
      entry_id, bundle_id, component_id, version,
      size_bytes, record_type, supersedes_id, recorded_at
    FROM manifest_raw
  `);

  await db.run(`
    CREATE OR REPLACE TABLE withdrawn_entries AS
    SELECT DISTINCT supersedes_id AS entry_id
    FROM manifest_deduped
    WHERE record_type = 'WITHDRAWAL'
      AND supersedes_id IS NOT NULL
      AND supersedes_id <> ''
  `);

  await db.run(`
    CREATE OR REPLACE TABLE surviving_builds AS
    SELECT d.*
    FROM manifest_deduped d
    LEFT JOIN withdrawn_entries w ON w.entry_id = d.entry_id
    WHERE d.record_type = 'BUILD'
      AND w.entry_id IS NULL
  `);

  // A bundle whose builds were all withdrawn has no rows here, so it drops out.
  const rows = await db.all(`
    SELECT bundle_id, COUNT(*) AS artifact_count, SUM(size_bytes) AS total_bytes
    FROM surviving_builds
    GROUP BY bundle_id
    ORDER BY bundle_id
  `);

  return rows.map((r) => ({
    bundle_id: r.bundle_id,
    artifact_count: num(r.artifact_count),
    total_bytes: num(r.total_bytes),
  }));
}

// Sorted keys, no whitespace. JSON.stringify won't guarantee key order for us,
// hence doing it by hand.
function canonical(value) {
  if (Array.isArray(value)) {
    return '[' + value.map(canonical).join(',') + ']';
  }
  if (value !== null && typeof value === 'object') {
    const pairs = Object.keys(value)
      .sort()
      .map((k) => JSON.stringify(k) + ':' + canonical(value[k]));
    return '{' + pairs.join(',') + '}';
  }
  return JSON.stringify(value);
}

// Detached CMS, PEM. keys/current/ only — the revoked pair is what broke the old
// publisher and it still won't verify against the gateway's trust store.
function sign(descriptor) {
  const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'fw-sign-'));
  const payload = path.join(scratch, 'descriptor.bin');

  try {
    fs.writeFileSync(payload, Buffer.from(descriptor, 'utf8'));

    return execFileSync('openssl', [
      'cms', '-sign',
      '-in', payload,
      '-signer', CERT_PEM,
      '-inkey', KEY_PEM,
      '-md', 'sha256',
      '-outform', 'PEM',
      '-binary',
    ], { encoding: 'utf8', maxBuffer: 8 * 1024 * 1024 });
  } finally {
    fs.rmSync(scratch, { recursive: true, force: true });
  }
}

async function currentKey() {
  const res = await fetch(`${GATEWAY}/v1/signing-key/current`);
  if (!res.ok) {
    throw new Error(`signing-key lookup failed: HTTP ${res.status}`);
  }
  return res.json();
}

async function publish(descriptor, signature, requestToken) {
  const res = await fetch(`${GATEWAY}/v1/publications`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    // Send the descriptor as a string so the gateway verifies the same bytes we
    // signed. Passing an object would make it re-encode, and any difference
    // there breaks the signature.
    body: JSON.stringify({ descriptor, signature, request_token: requestToken }),
  });

  const payload = await res.json().catch(() => ({}));
  if (!res.ok || payload.error) {
    throw new Error(payload.error || `publication failed: HTTP ${res.status}`);
  }
  return payload;
}

async function main() {
  const db = connect(DB_FILE);

  try {
    await createTables(db);
    await loadManifest(db);

    const bundles = await reconcile(db);
    const { key_id: keyId } = await currentKey();
    const lines = [];

    for (const bundle of bundles) {
      const requestToken = `token-${bundle.bundle_id}`;
      const descriptor = canonical({
        artifact_count: bundle.artifact_count,
        bundle_id: bundle.bundle_id,
        total_bytes: bundle.total_bytes,
      });

      const [stored] = await db.all(
        'SELECT publication_id, request_token, status, attempts FROM publications WHERE bundle_id = ?',
        bundle.bundle_id
      );

      let receipt;

      if (stored && stored.status === 'PUBLISHED') {
        // Already published on an earlier run — replay the receipt we kept
        // rather than sending it again.
        receipt = {
          publication_id: stored.publication_id,
          request_token: stored.request_token,
          status: stored.status,
        };
      } else {
        const attempt = (stored ? num(stored.attempts) : 0) + 1;
        const signature = sign(descriptor);

        try {
          receipt = await publish(descriptor, signature, requestToken);
        } catch (err) {
          await db.run(
            "INSERT INTO publication_attempts VALUES (?, ?, ?, 'FAILED', ?, now())",
            bundle.bundle_id, requestToken, attempt, String(err.message)
          );
          throw err;
        }

        await db.run(
          "INSERT INTO publication_attempts VALUES (?, ?, ?, 'PUBLISHED', NULL, now())",
          bundle.bundle_id, requestToken, attempt
        );
        await db.run('DELETE FROM publications WHERE bundle_id = ?', bundle.bundle_id);
        await db.run(
          'INSERT INTO publications VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, now())',
          bundle.bundle_id,
          receipt.request_token,
          receipt.publication_id,
          receipt.status,
          keyId,
          descriptor,
          bundle.artifact_count,
          bundle.total_bytes,
          attempt
        );
      }

      lines.push(`BUNDLE ${bundle.bundle_id} SIGNED KEY=${keyId}`);
      lines.push(
        `BUNDLE ${bundle.bundle_id} PUBLISHED RECEIPT=${receipt.publication_id} ` +
        `TOKEN=${receipt.request_token} STATUS=${receipt.status}`
      );
    }

    process.stdout.write(lines.join('\n') + '\n');
  } finally {
    await db.close();
  }
}

main().catch((err) => {
  process.stderr.write(`release-publisher: ${err.message}\n`);
  process.exit(1);
});
