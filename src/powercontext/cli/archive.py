"""Operator-facing portable bundle commands using a controlled built-in Runtime."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Coroutine
from pathlib import Path
from typing import Annotated, Any, TypeVar

import typer

from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.portability import (
    BundleConflictError,
    BundleFormatError,
    BundleInspection,
    BundleReceipt,
    PortableBundleService,
)
from powercontext.builtin.runtime import BuiltinConfig, open_builtin_runtime
from powercontext.builtin.artifacts.handoff import Handoff
from powercontext.builtin.artifacts.memory import Memory
from powercontext.builtin.artifacts.experience import Experience
from powercontext.builtin.artifacts.skill import Skill
from powercontext.builtin.sources import BUILTIN_SOURCE_REGISTRY
from powercontext.paths import default_database_path, default_scheduler_path, sqlite_url

HELP_OPTION_NAMES = ("-h", "--help")
_ResultT = TypeVar("_ResultT", BundleInspection, BundleReceipt)

archive_app = typer.Typer(
    name="archive",
    context_settings={"help_option_names": HELP_OPTION_NAMES},
    help="Create, inspect, validate, and restore portable logical bundles.",
    no_args_is_help=True,
)


@archive_app.command("export")
def export_bundle(
    scope_id: Annotated[
        list[str], typer.Option("--scope-id", min=1, help="Complete scope to export; repeat as needed.")
    ],
    output: Annotated[Path, typer.Option("--output", help="New portable bundle file to create.")],
) -> None:
    """Create a verified logical bundle from complete scopes."""

    _emit_archive_result(lambda archive: archive.export(scope_id, output))


@archive_app.command("inspect")
def inspect_bundle(
    source: Annotated[Path, typer.Argument(help="Portable bundle file to inspect.")],
) -> None:
    """Verify archive structure and checksums without writing domain data."""

    _emit_result(lambda: PortableBundleService.inspect(source))


@archive_app.command("restore")
def restore_bundle(
    source: Annotated[Path, typer.Argument(help="Portable bundle file to validate or restore.")],
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Validate only; never write domain data.")] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Confirm a write restore.")] = False,
) -> None:
    """Validate or restore a portable bundle into the configured deployment."""

    if dry_run:
        _emit_result(lambda: _validate_only(source))
        return
    if not yes:
        raise typer.BadParameter(  # noqa: TRY003
            "restore writes immutable records; re-run with --yes to confirm",
            param_hint="--yes",
        )
    _emit_archive_result(lambda archive: archive.restore(source))


async def _run_archive(operation: Callable[[PortableBundleService], Awaitable[_ResultT]], /) -> _ResultT:
    config = BuiltinConfig(database=SQLiteConfig(url=sqlite_url(default_database_path())))
    async with open_builtin_runtime(config, scheduler_path=default_scheduler_path()) as runtime:
        if runtime.archive is None:
            raise RuntimeError("portable archive service is unavailable")  # noqa: TRY003
        return await operation(runtime.archive)


def _emit_archive_result(operation: Callable[[PortableBundleService], Awaitable[_ResultT]], /) -> None:
    """Render expected archive failures without leaking stack traces or records."""

    _emit_result(lambda: _run_archive(operation))


async def _validate_only(source: Path) -> BundleInspection:
    # Validation checks the wire format and built-in adapters, not live Runtime
    # projections. No database needs to be created or initialized for this.
    return await PortableBundleService.validate_archive(
        source,
        supported_source_types=tuple(definition.name for definition in BUILTIN_SOURCE_REGISTRY.definitions),
        supported_artifact_families=(Handoff.family, Memory.family, Experience.family, Skill.family),
    )


def _emit_result(operation: Callable[[], Coroutine[Any, Any, _ResultT]], /) -> None:
    try:
        _emit(asyncio.run(operation()))
    except BundleFormatError as error:
        typer.echo(f"error: archive validation failed: {error}", err=True)
        raise typer.Exit(code=2) from error
    except BundleConflictError as error:
        typer.echo(f"error: archive restore conflict: {error}", err=True)
        raise typer.Exit(code=3) from error


def _emit(value: BundleInspection | BundleReceipt, /) -> None:
    payload = (
        value.__dict__
        if hasattr(value, "__dict__")
        else {field: getattr(value, field) for field in value.__dataclass_fields__}
    )
    typer.echo(json.dumps(payload, default=list, sort_keys=True))
