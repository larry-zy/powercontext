"""CLI commands owned by the ready-to-run service entry point."""

from __future__ import annotations

from typing import Annotated, Any

import typer

from powercontext.server.factory import create_server_app
from powercontext.server.logging import configure_server_logging
from powercontext.server.settings import (
    HttpConfig,
    ServerSettings,
    is_unauthenticated_non_loopback_bind,
)
from powercontext.server.tracing import configure_server_tracing

HELP_OPTION_NAMES = ("-h", "--help")

app = typer.Typer(
    name="server",
    context_settings={"help_option_names": HELP_OPTION_NAMES},
    help="Run a configured PowerContext service.",
    no_args_is_help=True,
)


@app.callback()
def main() -> None:
    """Manage the PowerContext service process."""


@app.command()
def run(
    host: Annotated[str | None, typer.Option(help="Address to bind.")] = None,
    port: Annotated[int | None, typer.Option(min=1, max=65535, help="Port to bind.")] = None,
) -> None:
    """Run the ASGI service in the foreground."""

    environment = ServerSettings()
    http = HttpConfig(
        host=environment.http.host if host is None else host,
        port=environment.http.port if port is None else port,
    )
    settings = environment.model_copy(update={"http": http})
    if is_unauthenticated_non_loopback_bind(
        host=settings.http.host,
        auth_enabled=settings.auth.enabled,
        allow_unauthenticated_non_loopback=settings.allow_unauthenticated_non_loopback,
    ):
        raise typer.BadParameter(  # noqa: TRY003
            "refusing to bind an unauthenticated Server to a non-loopback address; "
            "enable authentication, keep the bind on loopback, or set "
            "POWERCONTEXT_SERVER_ALLOW_UNAUTHENTICATED_NON_LOOPBACK=true to opt in",
            param_hint="--host",
        )
    configure_server_logging(settings.logging)
    tracing = configure_server_tracing(settings.tracing)
    try:
        _run_server(
            create_server_app(settings=settings, tracing=tracing),
            host=settings.http.host,
            port=settings.http.port,
        )
    finally:
        tracing.shutdown()


def _run_server(application: Any, *, host: str, port: int) -> None:
    import uvicorn

    uvicorn.run(application, host=host, port=port, access_log=False, log_config=None)
