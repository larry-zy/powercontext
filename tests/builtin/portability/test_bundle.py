# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Observable contracts for portable logical bundles."""

from __future__ import annotations

import asyncio
import hashlib
import json
import tracemalloc
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import delete, insert, select

from powercontext.builtin.persistence.sqlite import SQLiteConfig, SQLiteProfile
from powercontext.builtin.persistence.tables import (
    ARTIFACT_CANDIDATE_HEADS_TABLE,
    ARTIFACT_CANDIDATE_VERSIONS_TABLE,
    ARTIFACT_HEADS_TABLE,
    ARTIFACTS_TABLE,
    BUILTIN_TABLES,
    SOURCE_JOURNAL_HEADS_TABLE,
    SOURCES_TABLE,
)
from powercontext.builtin.portability import BundleConflictError, BundleFormatError, PortableBundleService


def test_bundle_round_trips_authoritative_scope_data_and_excludes_projections(tmp_path: Path) -> None:
    async def scenario() -> None:
        archive = tmp_path / "scope.pcb"
        source_path = tmp_path / "source.db"
        target_path = tmp_path / "target.db"
        async with SQLiteProfile.open(
            SQLiteConfig(url=f"sqlite+aiosqlite:///{source_path}"), tables=BUILTIN_TABLES
        ) as source:
            async with source.database.transaction() as connection:
                await connection.execute(insert(SOURCE_JOURNAL_HEADS_TABLE).values(scope_id="project:one", position=1))
                await connection.execute(
                    insert(SOURCES_TABLE).values(
                        scope_id="project:one",
                        source_type="content",
                        source_id="source-1",
                        payload=b'{"name":"source-1"}',
                        journal_position=1,
                    )
                )
                await connection.execute(
                    insert(ARTIFACTS_TABLE).values(
                        scope_id="project:one",
                        family="handoff",
                        artifact_id="handoff",
                        revision=1,
                        content=b'{"content":{"summary":"hello"}}',
                    )
                )
                await connection.execute(
                    insert(ARTIFACT_HEADS_TABLE).values(
                        scope_id="project:one",
                        family="handoff",
                        artifact_id="handoff",
                        revision=1,
                        searchable_text="not portable",
                    )
                )
            receipt = await PortableBundleService(source.database).export(["project:one"], archive)
            assert receipt.record_count == 4

        async with SQLiteProfile.open(
            SQLiteConfig(url=f"sqlite+aiosqlite:///{target_path}"), tables=BUILTIN_TABLES
        ) as target:
            service = PortableBundleService(target.database)
            inspection = await service.validate(archive, supported_source_types=("content",))
            assert inspection.scopes == ("project:one",)
            restored = await service.restore(archive, supported_source_types=("content",))
            assert restored.inserted == 4
            repeated = await service.restore(archive, supported_source_types=("content",))
            assert repeated.already_present == 4
            async with target.database.transaction() as connection:
                assert await connection.scalar(select(ARTIFACT_HEADS_TABLE.c.searchable_text)) is None

    asyncio.run(scenario())


def test_restore_rejects_divergent_immutable_identity_without_overwrite(tmp_path: Path) -> None:
    async def scenario() -> None:
        archive = tmp_path / "scope.pcb"
        source_path = tmp_path / "source.db"
        target_path = tmp_path / "target.db"
        async with SQLiteProfile.open(
            SQLiteConfig(url=f"sqlite+aiosqlite:///{source_path}"), tables=BUILTIN_TABLES
        ) as source:
            async with source.database.transaction() as connection:
                await connection.execute(insert(SOURCE_JOURNAL_HEADS_TABLE).values(scope_id="project:one", position=1))
                await connection.execute(
                    insert(SOURCES_TABLE).values(
                        scope_id="project:one",
                        source_type="content",
                        source_id="source-1",
                        payload=b"original",
                        journal_position=1,
                    )
                )
            await PortableBundleService(source.database).export(["project:one"], archive)
        async with SQLiteProfile.open(
            SQLiteConfig(url=f"sqlite+aiosqlite:///{target_path}"), tables=BUILTIN_TABLES
        ) as target:
            async with target.database.transaction() as connection:
                await connection.execute(insert(SOURCE_JOURNAL_HEADS_TABLE).values(scope_id="project:one", position=1))
                await connection.execute(
                    insert(SOURCES_TABLE).values(
                        scope_id="project:one",
                        source_type="content",
                        source_id="source-1",
                        payload=b"different",
                        journal_position=1,
                    )
                )
            service = PortableBundleService(target.database)
            with pytest.raises(BundleConflictError):
                await service.restore(archive)
            async with target.database.transaction() as connection:
                assert await connection.scalar(select(SOURCES_TABLE.c.payload)) == b"different"

    asyncio.run(scenario())


def test_failed_restore_rolls_back_earlier_records_and_can_be_retried(tmp_path: Path) -> None:
    """A later immutable conflict must not strand a partially restored scope."""

    async def scenario() -> None:
        archive = tmp_path / "scope.pcb"
        source_path = tmp_path / "source.db"
        target_path = tmp_path / "target.db"
        async with SQLiteProfile.open(
            SQLiteConfig(url=f"sqlite+aiosqlite:///{source_path}"), tables=BUILTIN_TABLES
        ) as source:
            async with source.database.transaction() as connection:
                await connection.execute(insert(SOURCE_JOURNAL_HEADS_TABLE).values(scope_id="project:one", position=1))
                await connection.execute(
                    insert(SOURCES_TABLE).values(
                        scope_id="project:one",
                        source_type="content",
                        source_id="source-1",
                        payload=b"new source",
                        journal_position=1,
                    )
                )
                await connection.execute(
                    insert(ARTIFACTS_TABLE).values(
                        scope_id="project:one",
                        family="handoff",
                        artifact_id="handoff",
                        revision=1,
                        content=b"archive revision",
                    )
                )
                await connection.execute(
                    insert(ARTIFACT_HEADS_TABLE).values(
                        scope_id="project:one", family="handoff", artifact_id="handoff", revision=1
                    )
                )
            await PortableBundleService(source.database).export(["project:one"], archive)

        async with SQLiteProfile.open(
            SQLiteConfig(url=f"sqlite+aiosqlite:///{target_path}"), tables=BUILTIN_TABLES
        ) as target:
            async with target.database.transaction() as connection:
                await connection.execute(
                    insert(ARTIFACTS_TABLE).values(
                        scope_id="project:one",
                        family="handoff",
                        artifact_id="handoff",
                        revision=1,
                        content=b"conflicting revision",
                    )
                )
            service = PortableBundleService(target.database)
            with pytest.raises(BundleConflictError):
                await service.restore(archive)

            # Source rows precede revisions in the archive, yet none can survive
            # the failed transaction.
            async with target.database.transaction() as connection:
                assert await connection.scalar(select(SOURCES_TABLE.c.source_id)) is None
                await connection.execute(delete(ARTIFACTS_TABLE))

            retried = await service.restore(archive)
            assert retried.inserted == 4

    asyncio.run(scenario())


def test_restore_reports_projection_readiness_from_runtime_rebuilder(tmp_path: Path) -> None:
    async def scenario() -> None:
        archive = tmp_path / "scope.pcb"
        source_path = tmp_path / "source.db"
        target_path = tmp_path / "target.db"
        async with SQLiteProfile.open(
            SQLiteConfig(url=f"sqlite+aiosqlite:///{source_path}"), tables=BUILTIN_TABLES
        ) as source:
            async with source.database.transaction() as connection:
                await connection.execute(insert(SOURCE_JOURNAL_HEADS_TABLE).values(scope_id="project:one", position=1))
            await PortableBundleService(source.database).export(["project:one"], archive)
        rebuilt: list[tuple[str, ...]] = []

        async def rebuild(scopes: tuple[str, ...]) -> None:
            rebuilt.append(scopes)

        async with SQLiteProfile.open(
            SQLiteConfig(url=f"sqlite+aiosqlite:///{target_path}"), tables=BUILTIN_TABLES
        ) as target:
            receipt = await PortableBundleService(target.database, projection_rebuilder=rebuild).restore(archive)
        assert rebuilt == [("project:one",)]
        assert receipt.projections_ready is True

    asyncio.run(scenario())


def test_restore_accepts_a_valid_bundle_with_records_in_a_different_physical_order(tmp_path: Path) -> None:
    async def scenario() -> None:
        archive = tmp_path / "ordered.pcb"
        reordered = tmp_path / "reordered.pcb"
        database = tmp_path / "source.db"
        async with SQLiteProfile.open(
            SQLiteConfig(url=f"sqlite+aiosqlite:///{database}"), tables=BUILTIN_TABLES
        ) as source:
            async with source.database.transaction() as connection:
                await connection.execute(insert(SOURCE_JOURNAL_HEADS_TABLE).values(scope_id="project:one", position=1))
                await connection.execute(
                    insert(SOURCES_TABLE).values(
                        scope_id="project:one",
                        source_type="content",
                        source_id="source-1",
                        payload=b"source",
                        journal_position=1,
                    )
                )
                await connection.execute(
                    insert(ARTIFACTS_TABLE).values(
                        scope_id="project:one",
                        family="handoff",
                        artifact_id="handoff",
                        revision=1,
                        content=b"artifact",
                    )
                )
                await connection.execute(
                    insert(ARTIFACT_HEADS_TABLE).values(
                        scope_id="project:one", family="handoff", artifact_id="handoff", revision=1
                    )
                )
            await PortableBundleService(source.database).export(["project:one"], archive)

        with zipfile.ZipFile(archive) as input_archive:
            manifest = json.loads(input_archive.read("manifest.json"))
            records = list(reversed(input_archive.read("records.ndjson").splitlines()))
        digests = [json.loads(record)["digest"] for record in records]
        manifest["total_digest"] = "sha256:" + hashlib.sha256("\n".join(digests).encode()).hexdigest()
        with zipfile.ZipFile(reordered, "w") as output_archive:
            output_archive.writestr("manifest.json", json.dumps(manifest))
            output_archive.writestr("records.ndjson", b"\n".join(records) + b"\n")

        async with SQLiteProfile.open(SQLiteConfig(), tables=BUILTIN_TABLES) as target:
            receipt = await PortableBundleService(target.database).restore(reordered)
        assert receipt.inserted == 4

    asyncio.run(scenario())


def test_restore_keeps_large_bundle_payloads_out_of_process_memory(tmp_path: Path) -> None:
    """The import budget is independent of aggregate NDJSON payload size."""

    async def scenario() -> None:
        archive = tmp_path / "large.pcb"
        source_path = tmp_path / "source.db"
        target_path = tmp_path / "target.db"
        payload = b"x" * 8_192
        async with SQLiteProfile.open(
            SQLiteConfig(url=f"sqlite+aiosqlite:///{source_path}"), tables=BUILTIN_TABLES
        ) as source:
            async with source.database.transaction() as connection:
                await connection.execute(insert(SOURCE_JOURNAL_HEADS_TABLE).values(scope_id="project:one", position=1_500))
                await connection.execute(
                    insert(SOURCES_TABLE),
                    [
                        {
                            "scope_id": "project:one",
                            "source_type": "content",
                            "source_id": f"source-{number}",
                            "payload": payload,
                            "journal_position": number,
                        }
                        for number in range(1, 1_501)
                    ],
                )
            await PortableBundleService(source.database).export(["project:one"], archive)

        tracemalloc.start()
        try:
            async with SQLiteProfile.open(
                SQLiteConfig(url=f"sqlite+aiosqlite:///{target_path}"), tables=BUILTIN_TABLES
            ) as target:
                receipt = await PortableBundleService(target.database).restore(archive)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        assert receipt.inserted == 1_501
        assert peak < 10 * 1024 * 1024

    asyncio.run(scenario())


def test_validate_rejects_an_artifact_family_the_target_runtime_cannot_restore(tmp_path: Path) -> None:
    async def scenario() -> None:
        archive = tmp_path / "scope.pcb"
        source_path = tmp_path / "source.db"
        async with SQLiteProfile.open(
            SQLiteConfig(url=f"sqlite+aiosqlite:///{source_path}"), tables=BUILTIN_TABLES
        ) as source:
            async with source.database.transaction() as connection:
                await connection.execute(
                    insert(ARTIFACTS_TABLE).values(
                        scope_id="project:one",
                        family="unavailable-family",
                        artifact_id="record-1",
                        revision=1,
                        content=b"payload",
                    )
                )
                await connection.execute(
                    insert(ARTIFACT_HEADS_TABLE).values(
                        scope_id="project:one",
                        family="unavailable-family",
                        artifact_id="record-1",
                        revision=1,
                    )
                )
            await PortableBundleService(source.database).export(["project:one"], archive)
        async with SQLiteProfile.open(SQLiteConfig(), tables=BUILTIN_TABLES) as target:
            with pytest.raises(BundleFormatError, match="artifact families"):
                await PortableBundleService(
                    target.database,
                    supported_artifact_families=("handoff",),
                ).validate(archive)

    asyncio.run(scenario())


def test_validate_rejects_candidate_evidence_that_is_not_in_the_bundle(tmp_path: Path) -> None:
    async def scenario() -> None:
        archive = tmp_path / "scope.pcb"
        database = tmp_path / "source.db"
        async with SQLiteProfile.open(
            SQLiteConfig(url=f"sqlite+aiosqlite:///{database}"), tables=BUILTIN_TABLES
        ) as source:
            async with source.database.transaction() as connection:
                await connection.execute(
                    insert(ARTIFACT_CANDIDATE_VERSIONS_TABLE).values(
                        scope_id="project:one",
                        candidate_id="candidate-1",
                        version=1,
                        family="handoff",
                        proposal=b"{}",
                        source_refs=b'[{"source_type":"content","source_id":"missing"}]',
                        artifact_refs=b"[]",
                        target_family=None,
                        target_artifact_id=None,
                        target_revision=None,
                        reason=None,
                    )
                )
                await connection.execute(
                    insert(ARTIFACT_CANDIDATE_HEADS_TABLE).values(
                        scope_id="project:one",
                        candidate_id="candidate-1",
                        family="handoff",
                        version=1,
                        status="pending",
                    )
                )
            await PortableBundleService(source.database).export(["project:one"], archive)
        async with SQLiteProfile.open(SQLiteConfig(), tables=BUILTIN_TABLES) as target:
            with pytest.raises(BundleFormatError, match="missing source"):
                await PortableBundleService(target.database).validate(archive)

    asyncio.run(scenario())
