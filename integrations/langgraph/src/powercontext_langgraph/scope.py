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

"""Run-scoped configuration passed through the LangGraph ``context_schema``."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PowerContextScope:
    """Durable scope for one graph run.

    LangGraph 1.x passes run-scoped configuration through ``context_schema``, which nodes receive as
    ``Runtime[ContextT]``. Supplying this dataclass makes the scope an explicit invocation argument::

        graph.invoke(state, context=PowerContextScope(scope_id="git:github.com/acme/api"))

    Separate invocations of one compiled graph can carry separate scopes, which supports multi-tenant
    deployment directly. Every field is optional; unset fields fall back to the environment settings.
    """

    scope_id: str | None = None
    base_url: str | None = None
    token: str | None = None
    timeout: float | None = None
