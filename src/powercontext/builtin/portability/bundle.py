"""Portable, verified logical bundles for built-in relational storage.

The archive deliberately contains domain rows only.  Database files, search
indexes, trigger cursors, usage statistics, and host-local skill registrations
are deployment details and are not portable state.
"""

# Error reason strings are the public inspection/validation contract.
# ruff: noqa: TRY003

from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import sqlite3
import tempfile
import zipfile
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

from sqlalchemy import insert, select, tuple_
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.builtin.persistence.database import AsyncDatabase
from powercontext.builtin.persistence.tables import (
    ARTIFACT_CANDIDATE_HEADS_TABLE,
    ARTIFACT_CANDIDATE_VERSIONS_TABLE,
    ARTIFACT_HEADS_TABLE,
    ARTIFACT_LINEAGE_ARTIFACTS_TABLE,
    ARTIFACT_LINEAGE_SOURCES_TABLE,
    ARTIFACTS_TABLE,
    MEMORY_ENTRY_HEADS_TABLE,
    MEMORY_ENTRY_VERSIONS_TABLE,
    SOURCE_JOURNAL_HEADS_TABLE,
    SOURCES_TABLE,
)
from powercontext.builtin.portability.schema import BINARY_FIELDS, INTEGER_FIELDS, NULLABLE_FIELDS, RECORD_FIELDS

FORMAT_VERSION = 1
_RECORDS_NAME = "records.ndjson"
_MANIFEST_NAME = "manifest.json"
_SHA256 = "sha256:"
_MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
_MAX_RECORDS_BYTES = 2 * 1024 * 1024 * 1024
_MAX_RECORDS = 2_000_000
_MAX_LINE_BYTES = 4 * 1024 * 1024
_HEX_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")

RecordType = Literal[
    "source_journal_head",
    "source",
    "artifact_revision",
    "artifact_lineage_source",
    "artifact_lineage_artifact",
    "artifact_head",
    "memory_entry_version",
    "memory_entry_head",
    "candidate_version",
    "candidate_head",
]
ProjectionRebuilder = Callable[[tuple[str, ...]], Awaitable[None]]


class BundleFormatError(ValueError):
    """The supplied archive is malformed, corrupt, or unsupported."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)


class BundleConflictError(ValueError):
    """A target immutable identity exists with different canonical content."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class BundleInspection:
    """Content-free facts established by parsing and verifying a bundle."""

    bundle_id: str
    scopes: tuple[str, ...]
    record_count: int
    records_by_type: Mapping[str, int]
    total_digest: str
    format_version: int


@dataclass(frozen=True, slots=True)
class BundleReceipt:
    """Content-free result of an export or restore."""

    bundle_id: str
    record_count: int
    total_digest: str
    inserted: int = 0
    already_present: int = 0
    projections_ready: bool = False


@dataclass(frozen=True, slots=True)
class _Record:
    record_type: RecordType
    identity: Mapping[str, str | int]
    payload: Mapping[str, Any]
    digest: str


@dataclass(frozen=True, slots=True)
class _ParsedBundle:
    inspection: BundleInspection


_TABLES: dict[RecordType, Any] = {
    "source_journal_head": SOURCE_JOURNAL_HEADS_TABLE,
    "source": SOURCES_TABLE,
    "artifact_revision": ARTIFACTS_TABLE,
    "artifact_lineage_source": ARTIFACT_LINEAGE_SOURCES_TABLE,
    "artifact_lineage_artifact": ARTIFACT_LINEAGE_ARTIFACTS_TABLE,
    "artifact_head": ARTIFACT_HEADS_TABLE,
    "memory_entry_version": MEMORY_ENTRY_VERSIONS_TABLE,
    "memory_entry_head": MEMORY_ENTRY_HEADS_TABLE,
    "candidate_version": ARTIFACT_CANDIDATE_VERSIONS_TABLE,
    "candidate_head": ARTIFACT_CANDIDATE_HEADS_TABLE,
}

# Parent rows always precede their dependent rows.  This matters for SQLite as
# well as for MySQL/OceanBase foreign-key enforcement.
_EXPORT_ORDER: tuple[RecordType, ...] = tuple(_TABLES)


class PortableBundleService:
    """Export and restore complete scopes without depending on a DB dialect.

    The service is intentionally application-facing rather than a database-file
    utility.  Callers must perform authorization before calling :meth:`export`;
    records are never enumerated before the scope arguments are validated.
    """

    def __init__(
        self,
        database: AsyncDatabase,
        /,
        *,
        projection_rebuilder: ProjectionRebuilder | None = None,
        supported_source_types: Iterable[str] | None = None,
        supported_artifact_families: Iterable[str] | None = None,
    ) -> None:
        self._database = database
        self._projection_rebuilder = projection_rebuilder
        self._supported_source_types = None if supported_source_types is None else frozenset(supported_source_types)
        self._supported_artifact_families = (
            None if supported_artifact_families is None else frozenset(supported_artifact_families)
        )

    async def export(self, scopes: Iterable[str], output: Path, /) -> BundleReceipt:
        """Write a verified archive containing all portable records for scopes.

        A single read transaction provides a consistent snapshot on supported
        relational backends.  The resulting ZIP is written only after the
        snapshot has been completely read, so a failed export never presents a
        partially finalized archive at ``output``.
        """

        selected = _validate_scopes(scopes)
        output.parent.mkdir(parents=True, exist_ok=True)
        records_path = _temporary_path(output.parent, suffix=".records.ndjson")
        archive_path = _temporary_path(output.parent, suffix=".pcb")
        try:
            with records_path.open("wb") as stream:
                async with self._database.transaction() as connection:
                    count, records_by_type, digest = await self._stream_export(connection, selected, stream)
            bundle_id = str(uuid4())
            manifest = {
                "format_version": FORMAT_VERSION,
                "bundle_id": bundle_id,
                "producer": {"name": "powercontext", "version": _producer_version()},
                "scopes": list(selected),
                "record_count": count,
                "records_by_type": dict(sorted(records_by_type.items())),
                "excluded_record_classes": [
                    "search_projections",
                    "source_cursors",
                    "external_skill_registrations",
                    "usage_statistics",
                ],
                "total_digest": digest,
            }
            with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
                archive.write(records_path, _RECORDS_NAME)
                archive.writestr(_MANIFEST_NAME, _canonical_json(manifest))
            archive_path.replace(output)
            return BundleReceipt(bundle_id=bundle_id, record_count=count, total_digest=digest)
        finally:
            records_path.unlink(missing_ok=True)
            archive_path.unlink(missing_ok=True)

    @staticmethod
    async def inspect(source: Path, /) -> BundleInspection:
        """Verify structure and checksums without touching the target database."""

        return _parse_bundle(source).inspection

    async def validate(
        self, source: Path, /, *, supported_source_types: Iterable[str] | None = None
    ) -> BundleInspection:
        """Verify bundle integrity, dependencies, and optional adapter support."""

        return await self.validate_archive(
            source,
            supported_source_types=(
                self._supported_source_types if supported_source_types is None else supported_source_types
            ),
            supported_artifact_families=self._supported_artifact_families,
        )

    @staticmethod
    async def validate_archive(
        source: Path,
        /,
        *,
        supported_source_types: Iterable[str] | None = None,
        supported_artifact_families: Iterable[str] | None = None,
    ) -> BundleInspection:
        """Validate a wire bundle without opening or initializing any database."""
        parsed = _parse_bundle(source)
        _validate_dependencies(source)
        configured = supported_source_types
        if configured is not None:
            supported = set(configured)
            missing = sorted(
                {
                    str(record.identity["source_type"])
                    for record in _iter_records(source)
                    if record.record_type == "source"
                }
                - supported
            )
            if missing:
                raise BundleFormatError(f"target does not support source types: {', '.join(missing)}")
        if supported_artifact_families is not None:
            required_families = _required_artifact_families(_iter_records(source))
            missing_families = sorted(required_families - set(supported_artifact_families))
            if missing_families:
                raise BundleFormatError(f"target does not support artifact families: {', '.join(missing_families)}")
        return parsed.inspection

    async def restore(
        self,
        source: Path,
        /,
        *,
        supported_source_types: Iterable[str] | None = None,
    ) -> BundleReceipt:
        """Restore a validated bundle atomically, skipping identical records.

        Existing rows are compared using the same portable record encoding.  A
        difference at the same primary identity aborts the entire transaction;
        no existing revision or head is overwritten.
        """

        # Own a stable input across validation and replay, even if the caller
        # replaces the original archive while a restore is running.
        with tempfile.TemporaryDirectory(prefix="powercontext-restore-") as directory:
            snapshot = Path(directory) / "bundle.pcb"
            try:
                shutil.copyfile(source, snapshot)
            except OSError as error:
                raise BundleFormatError("cannot read portable bundle") from error
            inspection = await self.validate(snapshot, supported_source_types=supported_source_types)
            with _staged_records(snapshot) as staged:
                async with self._database.transaction() as connection:
                    inserted, existing = await self._write_records(connection, staged)
        projections_ready = False
        if self._projection_rebuilder is not None:
            await self._projection_rebuilder(inspection.scopes)
            projections_ready = True
        return BundleReceipt(
            bundle_id=inspection.bundle_id,
            record_count=inspection.record_count,
            total_digest=inspection.total_digest,
            inserted=inserted,
            already_present=existing,
            projections_ready=projections_ready,
        )

    async def _write_records(self, connection: AsyncConnection, staged: sqlite3.Connection, /) -> tuple[int, int]:
        """Replay records in dependency order inside the caller transaction."""
        inserted = 0
        existing = 0
        for record_type in _EXPORT_ORDER:
            cursor = staged.execute("SELECT document FROM records WHERE kind = ? ORDER BY sequence", (record_type,))
            while documents := cursor.fetchmany(100):
                records = tuple(_parse_record(json.loads(row[0])) for row in documents)
                table = _TABLES[record_type]
                keys = RECORD_FIELDS[record_type][0]
                identities = [tuple(record.identity[key] for key in keys) for record in records]
                rows = (
                    await connection.execute(
                        select(table).where(tuple_(*(table.c[key] for key in keys)).in_(identities))
                    )
                ).mappings()
                present = {
                    _identity_key(record.identity): record.digest
                    for row in rows
                    for record in (_record_from_row(record_type, cast(Mapping[str, Any], dict(row))),)
                }
                values = []
                for record in records:
                    digest = present.get(_identity_key(record.identity))
                    if digest is not None:
                        if digest != record.digest:
                            raise BundleConflictError(f"immutable identity conflict: {record_type}")
                        existing += 1
                    else:
                        values.append(_row_values(record))
                if values:
                    await connection.execute(insert(table), values)
                    inserted += len(values)
        return inserted, existing

    async def _stream_export(
        self,
        connection: AsyncConnection,
        scopes: tuple[str, ...],
        stream: Any,
    ) -> tuple[int, Counter[str], str]:
        count = 0
        records_by_type: Counter[str] = Counter()
        digest = _DigestAccumulator()
        for record_type in _EXPORT_ORDER:
            table = _TABLES[record_type]
            statement = select(table).where(table.c.scope_id.in_(scopes)).order_by(*table.primary_key.columns)
            rows = await connection.stream(statement)
            async for row in rows.mappings():
                record = _record_from_row(record_type, cast(Mapping[str, Any], dict(row)))
                stream.write(_canonical_json(_record_document(record)) + b"\n")
                digest.add(record.digest)
                count += 1
                records_by_type[record.record_type] += 1
        return count, records_by_type, digest.value()


@contextmanager
def _staged_records(source: Path) -> Iterator[sqlite3.Connection]:
    """Index validated records on disk before acquiring the target write transaction."""
    with tempfile.TemporaryDirectory(prefix="powercontext-records-") as directory:
        connection = sqlite3.connect(Path(directory) / "records.db")
        try:
            connection.execute("CREATE TABLE records (sequence INTEGER PRIMARY KEY, kind TEXT, document BLOB)")
            connection.executemany(
                "INSERT INTO records(kind, document) VALUES (?, ?)",
                ((record.record_type, _canonical_json(_record_document(record))) for record in _iter_records(source)),
            )
            connection.execute("CREATE INDEX records_kind ON records(kind, sequence)")
            connection.commit()
            yield connection
        finally:
            connection.close()


def _record_from_row(record_type: RecordType, row: Mapping[str, Any]) -> _Record:
    identity_fields, payload_fields = RECORD_FIELDS[record_type]
    identity = {name: _json_value(row[name]) for name in identity_fields}
    payload = {name: _json_value(row[name]) for name in payload_fields}
    canonical = {"record_type": record_type, "schema_version": 1, "identity": identity, "payload": payload}
    return _Record(record_type=record_type, identity=identity, payload=payload, digest=_digest(canonical))


def _record_document(record: _Record) -> dict[str, Any]:
    return {
        "record_type": record.record_type,
        "schema_version": 1,
        "identity": dict(record.identity),
        "payload": dict(record.payload),
        "digest": record.digest,
    }


def _row_values(record: _Record) -> dict[str, Any]:
    values = {key: _database_value(value) for key, value in record.identity.items()}
    values.update({key: _database_value(value) for key, value in record.payload.items()})
    # Projections are intentionally excluded from logical bundles.  Heads may
    # use NULL searchable text; Memory projections are rebuilt by the runtime.
    if record.record_type == "artifact_head":
        values["searchable_text"] = None
    elif record.record_type == "memory_entry_head":
        values["searchable_text"] = str(record.payload["entry_content_hash"])
    return values


def _parse_bundle(source: Path) -> _ParsedBundle:
    try:
        with zipfile.ZipFile(source) as archive:
            _validate_container(archive)
            manifest = json.loads(archive.read(_MANIFEST_NAME))
            _validate_manifest(manifest)
    except (OSError, json.JSONDecodeError, zipfile.BadZipFile) as error:
        raise BundleFormatError("cannot read portable bundle") from error
    count = 0
    counts: Counter[str] = Counter()
    digest = _DigestAccumulator()
    for record in _iter_records(source):
        if record.identity["scope_id"] not in manifest["scopes"]:
            raise BundleFormatError("record scope is not declared in manifest")
        count += 1
        counts[record.record_type] += 1
        digest.add(record.digest)
    expected_count = manifest["record_count"]
    if count != expected_count:
        raise BundleFormatError("manifest record count does not match records")
    if digest.value() != manifest["total_digest"]:
        raise BundleFormatError("bundle total digest does not match records")
    sorted_counts = dict(sorted(counts.items()))
    if sorted_counts != manifest["records_by_type"]:
        raise BundleFormatError("manifest record classes do not match records")
    inspection = BundleInspection(
        bundle_id=manifest["bundle_id"],
        scopes=tuple(manifest["scopes"]),
        record_count=count,
        records_by_type=sorted_counts,
        total_digest=digest.value(),
        format_version=manifest["format_version"],
    )
    return _ParsedBundle(inspection=inspection)


def _iter_records(source: Path, /) -> Iterator[_Record]:
    """Yield verified records without retaining the archive body in memory."""

    try:
        with zipfile.ZipFile(source) as archive, archive.open(_RECORDS_NAME) as stream:
            count = 0
            while line := stream.readline(_MAX_LINE_BYTES + 1):
                if len(line) > _MAX_LINE_BYTES:
                    raise BundleFormatError("record line exceeds size limit")
                if not line.strip():
                    continue
                count += 1
                if count > _MAX_RECORDS:
                    raise BundleFormatError("record count exceeds limit")
                yield _parse_record(json.loads(line))
    except (OSError, json.JSONDecodeError, zipfile.BadZipFile) as error:
        raise BundleFormatError("cannot read portable bundle") from error


def _validate_container(archive: zipfile.ZipFile) -> None:
    names = {item.filename for item in archive.infolist()}
    if names != {_MANIFEST_NAME, _RECORDS_NAME}:
        raise BundleFormatError("bundle must contain only manifest.json and records.ndjson")
    if any(
        item.is_dir() or item.file_size > _MAX_RECORDS_BYTES or item.compress_size > _MAX_ARCHIVE_BYTES
        for item in archive.infolist()
    ):
        raise BundleFormatError("bundle exceeds resource limits")
    if archive.getinfo(_RECORDS_NAME).file_size > _MAX_RECORDS_BYTES:
        raise BundleFormatError("records exceed resource limit")


def _validate_manifest(manifest: object) -> None:
    if not isinstance(manifest, dict):
        raise BundleFormatError("manifest must be an object")
    manifest = cast(dict[str, object], manifest)
    required = {"format_version", "bundle_id", "scopes", "record_count", "records_by_type", "total_digest"}
    if not required <= set(manifest):
        raise BundleFormatError("manifest misses required fields")
    if manifest["format_version"] != FORMAT_VERSION:
        raise BundleFormatError("unsupported bundle format version")
    if not isinstance(manifest["bundle_id"], str) or not isinstance(manifest["record_count"], int):
        raise BundleFormatError("manifest has invalid identity or record count")
    _validate_scopes(cast(Iterable[str], manifest["scopes"]))
    if not isinstance(manifest["records_by_type"], dict) or not _valid_digest(manifest["total_digest"]):
        raise BundleFormatError("manifest has invalid checksums")


def _parse_record(value: object) -> _Record:
    if not isinstance(value, dict):
        raise BundleFormatError("record must be an object")
    value = cast(dict[str, object], value)
    record_type = value.get("record_type")
    if record_type not in _TABLES or value.get("schema_version") != 1:
        raise BundleFormatError("unsupported record type or schema")
    identity = value.get("identity")
    payload = value.get("payload")
    digest = value.get("digest")
    if not isinstance(identity, dict) or not isinstance(payload, dict) or not isinstance(digest, str):
        raise BundleFormatError("record has invalid fields")
    identity = cast(dict[str, str | int], identity)
    payload = cast(dict[str, Any], payload)
    typed = cast(RecordType, record_type)
    identity_fields, payload_fields = RECORD_FIELDS[typed]
    if set(identity) != set(identity_fields) or not _valid_digest(digest):
        raise BundleFormatError("record has invalid identity or digest")
    if set(payload) != set(payload_fields):
        raise BundleFormatError("record has invalid payload fields")
    _validate_field_values(identity, payload)
    canonical = {"record_type": typed, "schema_version": 1, "identity": identity, "payload": payload}
    if _digest(canonical) != digest:
        raise BundleFormatError("record digest does not match content")
    return _Record(record_type=typed, identity=identity, payload=payload, digest=digest)


def _validate_field_values(identity: Mapping[str, object], payload: Mapping[str, object]) -> None:
    for name, field in {**identity, **payload}.items():
        if field is None and name in NULLABLE_FIELDS and name not in identity:
            continue
        if name in BINARY_FIELDS:
            if not isinstance(_database_value(field), bytes):
                raise BundleFormatError("record has invalid binary field")
        elif name in INTEGER_FIELDS:
            if not isinstance(field, int) or isinstance(field, bool) or field < 0:
                raise BundleFormatError("record has invalid integer field")
        elif not isinstance(field, str):
            raise BundleFormatError("record has invalid text field")


def _validate_dependencies(source: Path, /) -> None:
    """Check references with a temporary on-disk identity index.

    The index deliberately stores only canonical identities, never record
    payloads.  This permits dependency validation for large bundles without
    retaining their content in the importing process.
    """

    with _identity_index(source) as contains:
        for record in _iter_records(source):
            _validate_record_dependencies(record, contains)


def _validate_record_dependencies(
    record: _Record,
    contains: Callable[[RecordType, Mapping[str, object]], bool],
    /,
) -> None:
    payload = record.payload
    identity = record.identity
    scope = identity["scope_id"]
    if record.record_type == "source":
        if not contains("source_journal_head", {"scope_id": scope}):
            raise BundleFormatError("source has no journal head")
    elif record.record_type in {"artifact_lineage_source"}:
        if not contains(
            "source",
            {"scope_id": scope, "source_type": payload["source_type"], "source_id": payload["source_id"]},
        ):
            raise BundleFormatError("artifact lineage references missing source")
    elif record.record_type in {"artifact_lineage_artifact", "memory_entry_version"}:
        if not contains(
            "artifact_revision",
            {
                "scope_id": scope,
                "family": payload.get("upstream_family", payload.get("family")),
                "artifact_id": payload.get("upstream_artifact_id", identity.get("memory_artifact_id")),
                "revision": payload.get("upstream_revision", payload.get("created_in_revision")),
            },
        ):
            raise BundleFormatError("record references missing artifact revision")
    elif record.record_type == "artifact_head":
        if not contains("artifact_revision", {**identity, "revision": payload["revision"]}):
            raise BundleFormatError("artifact head references missing revision")
    elif record.record_type == "memory_entry_head" and not contains(
        "memory_entry_version",
        {
            "scope_id": scope,
            "memory_artifact_id": identity["memory_artifact_id"],
            "entry_version_id": payload["entry_version_id"],
        },
    ):
        raise BundleFormatError("memory head references missing entry version")
    _validate_embedded_references((record,), contains)


@contextmanager
def _identity_index(source: Path, /) -> Iterator[Callable[[RecordType, Mapping[str, object]], bool]]:
    index_path = _temporary_path(Path(tempfile.gettempdir()), suffix=".identities.sqlite3")
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(index_path)
        connection.execute(
            "CREATE TABLE identities (record_type TEXT NOT NULL, identity TEXT NOT NULL, "
            "PRIMARY KEY (record_type, identity))"
        )
        for record in _iter_records(source):
            try:
                connection.execute(
                    "INSERT INTO identities (record_type, identity) VALUES (?, ?)",
                    (record.record_type, _identity_key(record.identity)),
                )
            except sqlite3.IntegrityError as error:
                raise BundleFormatError("bundle contains duplicate immutable identity") from error
        connection.commit()

        def contains(record_type: RecordType, identity: Mapping[str, object]) -> bool:
            return (
                connection.execute(
                    "SELECT 1 FROM identities WHERE record_type = ? AND identity = ?",
                    (record_type, _identity_key(identity)),
                ).fetchone()
                is not None
            )

        yield contains
    finally:
        if connection is not None:
            connection.close()
        index_path.unlink(missing_ok=True)


def _validate_embedded_references(
    records: Iterable[_Record],
    contains: Callable[[RecordType, Mapping[str, object]], bool],
) -> None:
    for record in records:
        if record.record_type not in {"memory_entry_version", "candidate_version"}:
            continue
        scope_id = record.identity["scope_id"]
        for reference in _reference_items(record.payload["source_refs"], "source"):
            if not contains(
                "source",
                {
                    "scope_id": scope_id,
                    "source_type": reference["source_type"],
                    "source_id": reference["source_id"],
                },
            ):
                raise BundleFormatError("record references missing source")
        for reference in _reference_items(record.payload["artifact_refs"], "artifact"):
            if not contains(
                "artifact_revision",
                {
                    "scope_id": scope_id,
                    "family": reference["family"],
                    "artifact_id": reference["artifact_id"],
                    "revision": reference["revision"],
                },
            ):
                raise BundleFormatError("record references missing artifact revision")


def _reference_items(value: object, kind: Literal["source", "artifact"], /) -> tuple[Mapping[str, object], ...]:
    encoded = cast(dict[str, object], value).get("base64") if isinstance(value, dict) else None
    if not isinstance(value, dict) or set(value) != {"base64"} or not isinstance(encoded, str):
        raise BundleFormatError(f"{kind} references are not encoded bytes")
    try:
        decoded = json.loads(base64.b64decode(encoded, validate=True))
    except (ValueError, json.JSONDecodeError) as error:
        raise BundleFormatError(f"{kind} references are not valid JSON") from error
    if not isinstance(decoded, list):
        raise BundleFormatError(f"{kind} references must be an array")
    fields = {"source_type", "source_id"} if kind == "source" else {"family", "artifact_id", "revision"}
    references: list[Mapping[str, object]] = []
    for item in decoded:
        if not isinstance(item, dict) or not fields <= set(item):
            raise BundleFormatError(f"{kind} reference has invalid fields")
        references.append(cast(Mapping[str, object], item))
    return tuple(references)


def _required_artifact_families(records: Iterable[_Record]) -> set[str]:
    families: set[str] = set()
    for record in records:
        identity = record.identity
        payload = record.payload
        if record.record_type in {
            "artifact_revision",
            "artifact_lineage_source",
            "artifact_lineage_artifact",
            "artifact_head",
        }:
            families.add(str(identity["family"]))
        if record.record_type in {"artifact_lineage_artifact", "candidate_version", "candidate_head"}:
            for field in ("upstream_family", "target_family", "result_family"):
                value = payload.get(field)
                if value is not None:
                    families.add(str(value))
        if record.record_type in {"memory_entry_version", "memory_entry_head", "candidate_version", "candidate_head"}:
            families.add(str(payload["family"]))
    return families


def _validate_scopes(scopes: Iterable[str]) -> tuple[str, ...]:
    selected = tuple(sorted(set(scopes)))
    if not selected or any(
        not isinstance(scope, str) or not scope.strip() or scope != scope.strip() for scope in selected
    ):
        raise ValueError("scopes must contain at least one non-empty trimmed scope ID")
    return selected


def _identity_key(identity: Mapping[str, object], /) -> str:
    return _canonical_json(identity).decode("utf-8")


class _DigestAccumulator:
    """Incrementally hash a deterministic ordered record-digest sequence."""

    def __init__(self) -> None:
        self._hash = hashlib.sha256()
        self._first = True

    def add(self, digest: str, /) -> None:
        if not self._first:
            self._hash.update(b"\n")
        self._hash.update(digest.encode("ascii"))
        self._first = False

    def value(self) -> str:
        return _SHA256 + self._hash.hexdigest()


def _temporary_path(directory: Path, /, *, suffix: str) -> Path:
    with tempfile.NamedTemporaryFile(
        prefix=".powercontext-archive-", suffix=suffix, dir=directory, delete=False
    ) as handle:
        return Path(handle.name)


def _digest(value: object) -> str:
    return _SHA256 + hashlib.sha256(_canonical_json(value)).hexdigest()


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _json_value(value: object) -> Any:
    if isinstance(value, bytes | bytearray | memoryview):
        return {"base64": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value


def _database_value(value: object) -> object:
    encoded = cast(dict[str, object], value).get("base64") if isinstance(value, dict) else None
    if isinstance(value, dict) and set(value) == {"base64"} and isinstance(encoded, str):
        try:
            return base64.b64decode(encoded, validate=True)
        except ValueError as error:
            raise BundleFormatError("invalid base64 column value") from error
    return value


def _valid_digest(value: object) -> bool:
    return isinstance(value, str) and _HEX_DIGEST.fullmatch(value) is not None


def _producer_version() -> str:
    try:
        return version("powercontext")
    except PackageNotFoundError:
        return "unknown"
