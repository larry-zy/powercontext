# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Portable archive acceptance through the public built-in Runtime."""

from __future__ import annotations

import asyncio
import importlib.util
import os
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import EmbeddingProfile, MemoryEntryInput
from powercontext.builtin.artifacts.skill import capture_skill_directory, package_file
from powercontext.builtin.inference import EmbeddingResult
from powercontext.builtin.persistence.oceanbase import OceanBaseConfig
from powercontext.builtin.persistence.seekdb import SeekDBConfig
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.persistence.topic_memory import TopicMemoryStorageInvariantError
from powercontext.builtin.portability import PortableBundleService
from powercontext.builtin.records import ArtifactWrite
from powercontext.builtin.runtime import (
    BuiltinConfig,
    CaptureSource,
    HandoffDraft,
    HandoffSourceCitation,
    HandoffStatement,
    RememberMemoryRequest,
    open_builtin_contexts,
    open_builtin_runtime,
)
from powercontext.builtin.scope import ScopeDraft
from powercontext.builtin.tags import MemoryEntryTagTarget, TagFilter
from powercontext.builtin.work import AcknowledgeHandoff, ReceiverChecks


def test_topic_restore_can_retry_after_projection_failure_and_restart(tmp_path: Path) -> None:
    async def authorize(_scopes: tuple[str, ...]) -> None:
        pass

    async def fail_projection(_scopes: tuple[str, ...]) -> None:
        raise RuntimeError("interrupted projection rebuild")  # noqa: TRY003

    async def scenario() -> None:
        archive = tmp_path / "topic.pcb"
        target_config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'target.db'}"))
        async with open_builtin_contexts(BuiltinConfig()) as source:
            scope = await source.scopes.create(ScopeDraft(title="Recovery", summary="Retry", idempotency_key="retry"))
            topic = await source.records.create_artifact(
                scope.scope_id,
                "topic-memory",
                ArtifactWrite(content={"title": "Recovery", "summary": "Retry", "detail": "Retry after interruption."}),
            )
            await source.portability.export([scope.scope_id], archive, authorize=authorize)
        async with open_builtin_contexts(target_config) as target:
            with pytest.raises(RuntimeError, match="interrupted"):
                await PortableBundleService(target.database, projection_rebuilder=fail_projection).restore(archive)
        with pytest.raises(TopicMemoryStorageInvariantError):
            async with open_builtin_contexts(target_config):
                pass
        async with open_builtin_contexts(target_config, _archive_recovery=True) as target:
            assert (await target.portability.restore(archive)).projections_ready
        async with open_builtin_contexts(target_config) as target:
            restored = await target.records.get_artifact(scope.scope_id, "topic-memory", topic.artifact_id)
            assert restored.artifact_id == topic.artifact_id

    asyncio.run(scenario())


def test_sqlite_portable_archive_restores_revisions_lineage_and_handoff_receipts(tmp_path: Path) -> None:
    async def scenario() -> None:
        archive_path = tmp_path / "portable.pcb"
        source_config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'source.db'}"))
        target_config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'target.db'}"))

        async with open_builtin_runtime(source_config) as source_runtime:
            assert source_runtime.scopes is not None
            scope = await source_runtime.scopes.create(
                ScopeDraft(
                    title="Portable archive", summary="Archive recovery fixture", idempotency_key="portable-archive"
                )
            )
            scope_id = scope.scope_id
            source = await source_runtime.sources.for_scope(scope_id).capture(
                CaptureSource(
                    source_id="turn-1",
                    content="The archive must preserve the handoff citation.",
                    metadata={"origin": "portable-archive-e2e"},
                )
            )
            memory = await source_runtime.memory.for_scope(scope_id).remember(
                RememberMemoryRequest(
                    entries=(MemoryEntryInput(kind="decision", text="Use the portable archive for migration."),)
                )
            )
            assert memory.entry is not None
            handoffs = source_runtime.handoff.for_scope(scope_id)
            prepared = await handoffs.finalize(
                HandoffDraft(
                    objective="Restore an exact logical handoff.",
                    state=(
                        HandoffStatement(
                            text="The source citation is durable.",
                            citations=(HandoffSourceCitation(source_ref=source.source_ref),),
                        ),
                    ),
                    disposition="continuable",
                    next_action=HandoffStatement(
                        text="Open the restored handoff.",
                        citations=(HandoffSourceCitation(source_ref=source.source_ref),),
                    ),
                )
            )
            committed = await handoffs.commit(prepared)
            acknowledgement = await source_runtime.work.for_scope(scope_id).acknowledge(
                AcknowledgeHandoff(
                    source_id="handoff-receipt-1",
                    receiver="portable-target",
                    status="accepted",
                    selection="exact",
                    revision=committed.as_ref(),
                    receiver_checks=ReceiverChecks(
                        live_state="confirmed",
                        capability="confirmed",
                        authorization="confirmed",
                    ),
                )
            )
            assert source_runtime.archive is not None

            async def authorize(scopes: tuple[str, ...]) -> None:
                assert scopes == (scope_id,)

            exported = await source_runtime.archive.export([scope_id], archive_path, authorize=authorize)

        async with open_builtin_runtime(target_config) as target_runtime:
            assert target_runtime.archive is not None
            restored = await target_runtime.archive.restore(archive_path)
            latest = await target_runtime.handoff.for_scope(scope_id).latest()
            continuity = await target_runtime.work.for_scope(scope_id).continuity()

            assert restored.record_count == exported.record_count
            assert restored.projections_ready is True
            assert latest == committed
            assert latest is not None
            assert latest.revision == 1
            assert latest.lineage.sources == (source.source_ref,)
            assert continuity.coverage.acknowledgement_records == 1
            assert continuity.coverage.active_receipt_ref == acknowledgement.receipt.source_ref

    asyncio.run(scenario())


def test_archive_restores_tagged_memory_vector_search_and_survives_restart(
    tmp_path: Path, target_config: BuiltinConfig
) -> None:
    class Embedding:
        profile = EmbeddingProfile(profile_id="archive-test", model="archive-test", dimension=3)

        async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
            return EmbeddingResult(vectors=tuple((1.0, 0.0, 0.0) for _ in texts))

    async def scenario() -> None:
        archive = tmp_path / "tagged.pcb"
        source_config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'source.db'}"))
        async with open_builtin_contexts(source_config, embedding_model=Embedding()) as source:
            scope = await source.scopes.create(
                ScopeDraft(title="Archive", summary="Vector recovery", idempotency_key="archive-vector")
            )
            service = (await source.get(scope.scope_id)).artifacts.memory
            memory = await service.remember(
                memory=None, entries=(MemoryEntryInput(kind="fact", text="Portable vector recovery."),), mode="append"
            )
            assert memory is not None
            entry = memory.content.manifest.entries[0]
            tag_target = MemoryEntryTagTarget(artifact_id=memory.artifact_id, entry_id=entry.entry_id)
            empty = await source.records.get_tags(scope.scope_id, tag_target)
            tagged = await source.records.replace_tags(
                scope.scope_id, tag_target, ("archive",), expected_etag=empty.etag
            )

            async def authorize(_scopes: tuple[str, ...]) -> None:
                pass

            await source.portability.export([scope.scope_id], archive, authorize=authorize)
        async with open_builtin_contexts(target_config, embedding_model=Embedding()) as target:
            receipt = await target.portability.restore(archive)
            assert receipt.projections_ready
            assert await target.records.get_tags(scope.scope_id, tag_target) == tagged
        async with open_builtin_contexts(target_config, embedding_model=Embedding()) as target:
            service = (await target.get(scope.scope_id)).artifacts.memory
            for mode in ("fts", "vector", "hybrid"):
                result = await service.search(
                    "portable", memories=(memory,), mode=mode, tag_filter=TagFilter(tags=("archive",))
                )
                assert [hit.entry_id for hit in result.hits] == [entry.entry_id]

    asyncio.run(scenario())


def test_archive_restores_package_files_and_topic_search_after_restart(
    tmp_path: Path, target_config: BuiltinConfig
) -> None:
    async def scenario() -> None:
        package_dir = tmp_path / "portable"
        package_dir.mkdir()
        (package_dir / "SKILL.md").write_text("---\nname: portable\ndescription: Portable guide\n---\nRead guide.md.\n")
        (package_dir / "guide.md").write_text("Exact portable instructions.\n")
        package = capture_skill_directory(package_dir)
        archive = tmp_path / "families.pcb"
        source_config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'source.db'}"))
        async with open_builtin_contexts(source_config) as source:
            scope = await source.scopes.create(
                ScopeDraft(title="Families", summary="Portable families", idempotency_key="families")
            )
            await source.upload_skill_package(scope.scope_id, package.archive_bytes, None, None)
            skill = await source.records.create_artifact(
                scope.scope_id, "skill", ArtifactWrite(content=package.as_skill_content().model_dump(mode="json"))
            )
            topic = await source.records.create_artifact(
                scope.scope_id,
                "topic-memory",
                ArtifactWrite(
                    content={
                        "title": "Portable archives",
                        "summary": "Recovery",
                        "detail": "Portable archive recovery details.",
                    }
                ),
            )
            profile = await source.records.create_artifact(
                scope.scope_id, "profile", ArtifactWrite(content={"content": "# Profile\nUse portable archives.\n"})
            )
            prompt = await source.records.create_artifact(
                scope.scope_id,
                "prompt",
                ArtifactWrite(
                    prompt_key="memory.extract",
                    content={
                        "schema_version": "powercontext.prompt.v1",
                        "mode": "auto",
                        "instructions": "",
                        "demonstrations": [],
                    },
                ),
            )
            expected_profile = await source.records.get_artifact(scope.scope_id, "profile", profile.artifact_id)
            expected_prompt = await source.records.get_artifact(scope.scope_id, "prompt", prompt.artifact_id)

            async def authorize(_scopes: tuple[str, ...]) -> None:
                pass

            exported = await source.portability.export([scope.scope_id], archive, authorize=authorize)
        if target_config.database.kind != "sqlite":
            async with (
                open_builtin_contexts(target_config) as legacy,
                legacy.database.transaction() as connection,
            ):
                await connection.exec_driver_sql(
                    "ALTER TABLE pc_topic_memory_revision_publications MODIFY COLUMN published_at DATETIME NOT NULL"
                )
                await connection.exec_driver_sql(
                    "ALTER TABLE pc_profile_policies MODIFY COLUMN updated_at DATETIME NOT NULL"
                )
        async with open_builtin_contexts(target_config) as target:
            receipt = await target.portability.restore(archive)
            assert receipt.projections_ready
            repeated = await target.portability.restore(archive)
            assert repeated.inserted == 0
        async with open_builtin_contexts(target_config) as target:
            restored = await target.skill_package(
                scope.scope_id, ArtifactRef(family="skill", artifact_id=skill.artifact_id, revision=skill.revision)
            )
            assert restored.archive_bytes == package.archive_bytes
            assert package_file(restored, "guide.md") == b"Exact portable instructions.\n"
            assert await target.records.get_artifact(scope.scope_id, "profile", profile.artifact_id) == expected_profile
            assert await target.records.get_artifact(scope.scope_id, "prompt", prompt.artifact_id) == expected_prompt
            async with target.database.transaction() as connection:
                found = await target.repositories.topic_memories.search(connection, scope.scope_id, "portable", limit=5)
            assert [hit.artifact_ref.artifact_id for hit in found.hits] == [topic.artifact_id]
            roundtrip = tmp_path / "roundtrip.pcb"
            reexported = await target.portability.export([scope.scope_id], roundtrip, authorize=authorize)
            assert reexported.total_digest == exported.total_digest
        reverse_config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'reverse.db'}"))
        async with open_builtin_contexts(reverse_config) as reverse:
            assert (await reverse.portability.restore(roundtrip)).projections_ready
            assert (
                await reverse.records.get_artifact(scope.scope_id, "profile", profile.artifact_id) == expected_profile
            )

    asyncio.run(scenario())


@pytest.fixture(params=["sqlite", "seekdb", "server"])
def target_config(tmp_path: Path, short_tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[BuiltinConfig]:
    if request.param == "seekdb":
        if importlib.util.find_spec("pylibseekdb") is None:
            pytest.skip("install powercontext[seekdb] for the embedded database")
        yield BuiltinConfig(database=SeekDBConfig(path=short_tmp_path / "seekdb"))
    elif request.param == "server":
        live_url = os.environ.get("POWERCONTEXT_TEST_OCEANBASE_URL")
        if not live_url:
            pytest.skip("set POWERCONTEXT_TEST_OCEANBASE_URL for an isolated server database")
        url = make_url(live_url)
        name = "pc_archive_" + uuid4().hex
        engine = create_engine(url.set(drivername="mysql+pymysql"), hide_parameters=True)
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql(f"CREATE DATABASE `{name}`")
            try:
                yield BuiltinConfig(
                    database=OceanBaseConfig(
                        url=SecretStr(url.set(database=name).render_as_string(hide_password=False))
                    )
                )
            finally:
                with engine.begin() as connection:
                    connection.exec_driver_sql(f"DROP DATABASE `{name}`")
        finally:
            engine.dispose()
    else:
        yield BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'target.db'}"))
