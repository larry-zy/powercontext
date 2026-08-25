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

"""Shared network transport-policy contract across Client, CLI, Server, and the agent plugins."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from powercontext.client import PowerContextClient
from powercontext.client.settings import ClientSettings
from powercontext.server.settings import BearerAuthConfig, HttpConfig, ServerSettings
from powercontext.transport import LOOPBACK_HOSTS, is_loopback_host, is_plaintext_non_loopback

_ALL_INTERFACES = "0.0.0.0"  # noqa: S104 - a non-loopback bind used to exercise the policy.

# Both agent plugins ship isolated (they do not depend on powercontext) and each vendors its own
# copy of the loopback policy. Load every copy by path and pin it to the shared contract so drift in
# any one implementation is caught. Loading is defensive: a moved path or a plugin import error
# skips that plugin's drift guard rather than failing collection for the whole module.
_INTEGRATIONS = Path(__file__).resolve().parent.parent / "integrations"
_VENDORED_PLUGIN_PATHS = {
    "codex": _INTEGRATIONS / "codex" / "plugins" / "powercontext" / "settings.py",
    "claude-code": _INTEGRATIONS / "claude-code" / "plugins" / "powercontext" / "claude_code_settings.py",
}


def _load_plugin_module(name: str, path: Path) -> tuple[ModuleType | None, str]:
    module_name = f"vendored_plugin_{name}".replace("-", "_")
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before executing: a slotted dataclass (the Claude Code plugin) resolves its own
    # module via sys.modules during class creation and fails to import otherwise.
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as error:  # pragma: no cover - exercised only when a plugin is unavailable.
        sys.modules.pop(module_name, None)
        return None, repr(error)
    return module, ""


_VENDORED_PLUGINS = {name: _load_plugin_module(name, path) for name, path in _VENDORED_PLUGIN_PATHS.items()}
_VENDORED_PLUGIN_PARAMS = [
    pytest.param(
        module,
        id=name,
        marks=pytest.mark.skipif(module is None, reason=f"{name} plugin unavailable: {error}"),
    )
    for name, (module, error) in _VENDORED_PLUGINS.items()
]


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "127.0.0.2", "127.1.2.3", "localhost", "LOCALHOST", "::1", "[::1]"],
)
def test_loopback_hosts_are_recognized(host: str) -> None:
    assert is_loopback_host(host)


@pytest.mark.parametrize(
    "host",
    [_ALL_INTERFACES, "memory.example", "192.168.1.10", "::", "", None],
)
def test_non_loopback_hosts_are_rejected(host: str | None) -> None:
    assert not is_loopback_host(host)


def test_plaintext_non_loopback_detects_only_remote_http() -> None:
    assert is_plaintext_non_loopback("http://memory.example")
    assert not is_plaintext_non_loopback("http://127.0.0.1:8000")
    assert not is_plaintext_non_loopback("https://memory.example")


@pytest.mark.parametrize(
    "server_url",
    ["http://memory.example", "http://192.168.1.10:8000"],
)
def test_client_settings_reject_non_loopback_plaintext_urls(server_url: str) -> None:
    with pytest.raises(ValidationError):
        ClientSettings(server_url=server_url)


@pytest.mark.parametrize(
    "server_url",
    ["http://127.0.0.1:8000", "http://localhost:8000", "https://memory.example"],
)
def test_client_settings_accept_loopback_or_tls_urls(server_url: str) -> None:
    assert ClientSettings(server_url=server_url).server_url.startswith(("http://", "https://"))


def test_client_refuses_a_bearer_token_over_non_loopback_plaintext() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"status": "ok"}))
    with httpx.Client(transport=transport):  # noqa: SIM117 - guard runs before any request.
        with pytest.raises(ValueError, match="bearer token"):
            PowerContextClient("http://memory.example", token="probe-token")  # noqa: S106 - test credential.


def test_client_allows_a_bearer_token_over_loopback_plaintext() -> None:
    client = PowerContextClient("http://127.0.0.1:8000", token="probe-token")  # noqa: S106 - test credential.
    assert client is not None


def test_client_allows_a_bearer_token_over_a_caller_supplied_transport() -> None:
    # A caller-supplied http_client owns its transport (here an in-process ASGI/mock transport), so
    # the base_url scheme is only a routing label and the plaintext-token guard must not fire.
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"status": "ok"})),
    )
    client = PowerContextClient("http://testserver", token="probe-token", http_client=http_client)  # noqa: S106 - test credential.
    assert client is not None


def test_client_allows_an_unauthenticated_non_loopback_transport() -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok"})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    client = PowerContextClient("http://testserver", http_client=http_client)
    assert client is not None


def test_server_rejects_an_unauthenticated_non_loopback_bind() -> None:
    with pytest.raises(ValidationError):
        ServerSettings(
            http=HttpConfig(host=_ALL_INTERFACES),
            auth=BearerAuthConfig(enabled=False),
        )


def test_server_allows_a_non_loopback_bind_with_authentication() -> None:
    settings = ServerSettings(
        http=HttpConfig(host=_ALL_INTERFACES),
        auth=BearerAuthConfig(enabled=True, token=SecretStr("server-secret")),
    )
    assert settings.http.host == _ALL_INTERFACES


def test_server_allows_a_non_loopback_bind_with_an_explicit_opt_in() -> None:
    settings = ServerSettings(
        http=HttpConfig(host=_ALL_INTERFACES),
        auth=BearerAuthConfig(enabled=False),
        allow_unauthenticated_non_loopback=True,
    )
    assert settings.allow_unauthenticated_non_loopback is True


@pytest.mark.parametrize("plugin", _VENDORED_PLUGIN_PARAMS)
def test_vendored_plugin_shares_the_loopback_host_set(plugin: ModuleType) -> None:
    assert plugin._LOOPBACK_HOSTS == LOOPBACK_HOSTS


@pytest.mark.parametrize("plugin", _VENDORED_PLUGIN_PARAMS)
@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "127.0.0.2",
        "localhost",
        "[::1]",
        _ALL_INTERFACES,
        "memory.example",
        "192.168.1.10",
    ],
)
def test_vendored_plugin_matches_the_shared_plaintext_policy(plugin: ModuleType, host: str) -> None:
    """Each plugin's vendored loopback check must agree with the shared transport contract."""

    base_url = f"http://{host}:8000"
    transport_rejects = is_plaintext_non_loopback(base_url)
    try:
        plugin._http_base_url(f"{base_url}/mcp")
        plugin_rejects = False
    except ValueError:
        plugin_rejects = True
    assert plugin_rejects == transport_rejects
