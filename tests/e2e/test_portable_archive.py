# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Portable archive acceptance through the public built-in Runtime."""

from __future__ import annotations

import asyncio
from pathlib import Path

from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    CaptureSource,
    HandoffDraft,
    HandoffSourceCitation,
    HandoffStatement,
    RememberMemoryRequest,
    open_builtin_runtime,
)


def test_sqlite_portable_archive_restores_revisions_lineage_and_handoff_receipts(tmp_path: Path) -> None:
    async def scenario() -> None:
        scope_id = "project:portable"
        archive_path = tmp_path / "portable.pcb"
        source_config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'source.db'}"))
        target_config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'target.db'}"))

        async with open_builtin_runtime(source_config) as source_runtime:
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
            assert source_runtime.archive is not None
            exported = await source_runtime.archive.export([scope_id], archive_path)

        async with open_builtin_runtime(target_config) as target_runtime:
            assert target_runtime.archive is not None
            restored = await target_runtime.archive.restore(archive_path)
            latest = await target_runtime.handoff.for_scope(scope_id).latest()

            assert restored.record_count == exported.record_count
            assert restored.projections_ready is True
            assert latest == committed
            assert latest is not None
            assert latest.revision == 1
            assert latest.lineage.sources == (source.source_ref,)

    asyncio.run(scenario())
