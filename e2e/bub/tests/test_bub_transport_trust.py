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

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from powercontext_bub import plugin as plugin_module
from powercontext_bub.plugin import PowerContextPlugin, PowerContextSettings


def _plugin_with(settings: PowerContextSettings, monkeypatch, tmp_path: Path) -> PowerContextPlugin:
    monkeypatch.setattr(plugin_module, "ensure_config", lambda _: settings)
    return PowerContextPlugin(SimpleNamespace(workspace=tmp_path))


def test_plugin_refuses_a_plaintext_non_loopback_server_by_default(monkeypatch, tmp_path: Path) -> None:
    settings = PowerContextSettings(base_url="http://host-gateway:8000", scope_id="test:scope")
    plugin = _plugin_with(settings, monkeypatch, tmp_path)

    async def prepare() -> None:
        async with plugin._client():
            pass

    with pytest.raises(ValueError, match="non-loopback"):
        asyncio.run(prepare())


def test_trusting_the_transport_supplies_the_client_an_explicit_vouched_transport(
    monkeypatch,
    tmp_path: Path,
) -> None:
    constructions: list[dict[str, Any]] = []

    class RecordingClient:
        def __init__(
            self,
            base_url: str,
            *,
            http_client: httpx.AsyncClient | None = None,
            trust_transport_security: bool = False,
            timeout: float | None = None,
        ) -> None:
            constructions.append({
                "base_url": base_url,
                "http_client": http_client,
                "trust_transport_security": trust_transport_security,
                "timeout": timeout,
            })

        async def __aenter__(self) -> RecordingClient:
            return self

        async def __aexit__(self, *exc_info: object) -> None:
            del exc_info

    settings = PowerContextSettings(
        base_url="http://host-gateway:8000",
        scope_id="test:scope",
        trust_transport_security=True,
    )
    monkeypatch.setattr(plugin_module, "PowerContextClient", RecordingClient)
    plugin = _plugin_with(settings, monkeypatch, tmp_path)

    async def open_client() -> None:
        async with plugin._client():
            pass

    asyncio.run(open_client())

    assert len(constructions) == 1
    construction = constructions[0]
    assert construction["base_url"] == "http://host-gateway:8000"
    assert isinstance(construction["http_client"], httpx.AsyncClient)
    assert construction["trust_transport_security"] is True
