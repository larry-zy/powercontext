---
title: Export and restore a portable archive
description: Create, validate, and restore a verified PowerContext logical bundle.
---

# Export and restore a portable archive

A portable archive is a `.pcb` file for moving or recovering complete PowerContext scopes. It is a logical archive:
it preserves domain identities and immutable history rather than copying a SQLite file. Use it for a controlled local
backup, an offline transfer, or a future backend migration.

This command is a local operator command. It opens the default SQLite database in the PowerContext user-data directory;
it does not call a remote Server API. Run it only on a machine whose local PowerContext data you control.

## Prerequisites

Install the CLI from the same PowerContext revision that created the local data. The archive command uses the persistent
SQLite database at `POWERCONTEXT_HOME/powercontext.db`; without `POWERCONTEXT_HOME`, PowerContext uses its operating
system user-data directory.

```bash
uv tool install "powercontext[cli] @ git+https://github.com/oceanbase/powercontext.git@master"
export POWERCONTEXT_HOME="$HOME/.local/share/powercontext"
```

The archive contains content and metadata needed for recovery. Store it in a protected location with access controls
and retention appropriate for the underlying project data. The format deliberately omits credentials and configured
provider secrets, but it does not encrypt the project content.

## Create and inspect an archive

Pass every complete scope to include. A scope cannot be selected by prefix or partially by record type.

```bash
powercontext archive export \
  --scope-id project:payments \
  --output ./backups/payments.pcb

powercontext archive inspect ./backups/payments.pcb
```

`export` writes JSON containing the bundle ID, record count, and checksum. `inspect` verifies the ZIP structure,
per-record digests, total digest, record counts, and selected scopes without opening the target database. Keep the
reported checksum with the backup inventory if an external backup system needs an independent verification record.

## Validate before a restore

Always validate the archive against the destination first:

```bash
powercontext archive restore ./backups/payments.pcb --dry-run
```

Dry-run performs no domain writes. It verifies checksums, required source and Artifact dependencies, and whether the
configured Runtime can restore the archive's source types and Artifact families. A validation failure exits with code
`2` and reports only a content-free reason; record bodies are not printed.

To perform the write, repeat the command with explicit confirmation:

```bash
powercontext archive restore ./backups/payments.pcb --yes
```

The result is JSON with `inserted`, `already_present`, and `projections_ready`. Treat the restore as ready for search
only when `projections_ready` is `true`. The Runtime rebuilds portable Memory and Experience search projections after
the authoritative rows have been restored.

## What the bundle preserves

Format version 1 carries the portable relational representation of these supported records:

| Preserved | Not portable |
| --- | --- |
| Source journal heads and Source records | Search projections and indexes |
| Artifact Revisions, lineage, and heads | Source cursors and scheduler state |
| Memory entry versions and heads | External Skill registrations and host-local installation state |
| Candidate versions, decision heads, and their evidence references | Usage statistics, credentials, bearer tokens, and provider secrets |

The archive has a versioned manifest (`format_version`, producer version, scopes, counts, exclusions, and total
checksum) plus NDJSON records. It is the authoritative round-trip format. CSV may be produced separately for bounded
analysis, but it cannot preserve immutable revisions, lineage, or evidence references and must not be used for restore.

## Recovery and conflicts

Restore is idempotent. Replaying the same archive recognizes identical records as `already_present`. A different
payload for an existing immutable identity never overwrites it: restore exits with code `3` and leaves the write
transaction rolled back. Resolve the conflicting target data or choose a clean destination, then run the same restore
command again.

If the command is interrupted before completion, do not claim recovery succeeded. Authoritative records are
transactionally applied, so a failed write does not leave a successful-looking partial restore. Re-run the same archive
against the same destination after the previous command has stopped.

## Current boundaries

Export streams database rows to a temporary NDJSON file. Validation stores only record identities in a temporary
on-disk index, and restore replays dependency levels as streams, so aggregate archive payloads are not retained in
process memory. The local CLI always opens the default SQLite database under `POWERCONTEXT_HOME` (or the operating
system user-data directory); it is not a remote Server API and does not use Server authentication or non-SQLite
database settings. For a live production database, use the database provider's coordinated backup procedures.
