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

"""Shared network transport-policy contract across Client, CLI, and Server."""

from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from powercontext.client import PowerContextClient
from powercontext.client.settings import ClientSettings
from powercontext.server.settings import BearerAuthConfig, HttpConfig, ServerSettings
from powercontext.transport import is_loopback_host, is_plaintext_non_loopback

_ALL_INTERFACES = "0.0.0.0"  # noqa: S104 - a non-loopback bind used to exercise the policy.


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
            http={"host": _ALL_INTERFACES},
            auth={"enabled": False},
            mcp={"enabled": False},
            dashboard={"enabled": False},
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
