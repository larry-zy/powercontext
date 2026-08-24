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

"""Shared network transport-policy contract across Client, CLI, Server, and the Codex plugin."""

from __future__ import annotations

import importlib.util
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

# The Codex plugin ships as an isolated package (it only depends on pydantic-settings) and cannot
# import powercontext, so it keeps its own copy of the loopback policy. Load it by path to pin that
# copy to the shared contract and catch drift the two implementations could otherwise hide.
_CODEX_SETTINGS_PATH = (
    Path(__file__).resolve().parent.parent
    / "integrations"
    / "codex"
    / "plugins"
    / "powercontext"
    / "settings.py"
)


def _load_codex_settings() -> ModuleType:
    spec = importlib.util.spec_from_file_location("codex_plugin_settings", _CODEX_SETTINGS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_CODEX_SETTINGS = _load_codex_settings()


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "localhost", "LOCALHOST", "::1", "[::1]"],
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


def test_codex_plugin_shares_the_loopback_host_set() -> None:
    assert _CODEX_SETTINGS._LOOPBACK_HOSTS == LOOPBACK_HOSTS


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "localhost", "[::1]", _ALL_INTERFACES, "memory.example", "192.168.1.10"],
)
def test_codex_plugin_matches_the_shared_plaintext_policy(host: str) -> None:
    """The Codex plugin's own loopback check must agree with the shared transport contract."""

    base_url = f"http://{host}:8000"
    transport_rejects = is_plaintext_non_loopback(base_url)
    try:
        _CODEX_SETTINGS._http_base_url(f"{base_url}/mcp")
        codex_rejects = False
    except ValueError:
        codex_rejects = True
    assert codex_rejects == transport_rejects
