# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

from typer.testing import CliRunner

from powercontext.cli.app import create_cli
from powercontext.cli.archive import archive_app


def test_archive_cli_exposes_a_safe_restore_confirmation_and_dry_run() -> None:
    cli = create_cli([archive_app])
    runner = CliRunner()

    help_result = runner.invoke(cli, ["archive", "--help"])
    restore_help = runner.invoke(cli, ["archive", "restore", "--help"])

    assert help_result.exit_code == 0
    assert all(command in help_result.output for command in ("export", "inspect", "restore"))
    assert restore_help.exit_code == 0
    assert "--dry-run" in restore_help.output
    assert "--yes" in restore_help.output


def test_archive_cli_can_export_inspect_and_dry_run_an_empty_scope(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("POWERCONTEXT_HOME", str(tmp_path / "data"))
    output = tmp_path / "scope.pcb"
    cli = create_cli([archive_app])
    runner = CliRunner()

    exported = runner.invoke(cli, ["archive", "export", "--scope-id", "project:one", "--output", str(output)])
    inspected = runner.invoke(cli, ["archive", "inspect", str(output)])
    validated = runner.invoke(cli, ["archive", "restore", str(output), "--dry-run"])

    assert exported.exit_code == 0, exported.output
    assert output.is_file()
    assert inspected.exit_code == 0, inspected.output
    assert validated.exit_code == 0, validated.output


def test_archive_cli_reports_corrupt_or_missing_bundles_without_a_traceback() -> None:
    result = CliRunner().invoke(create_cli([archive_app]), ["archive", "inspect", "missing.pcb"])

    assert result.exit_code == 2
    assert isinstance(result.exception, SystemExit)
    assert "error: archive validation failed: cannot read portable bundle" in result.output
    assert "Traceback" not in result.output
