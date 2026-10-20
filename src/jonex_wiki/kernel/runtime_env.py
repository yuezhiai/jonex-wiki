"""Runtime environment override for engine modules.

Core engine helpers read configuration through ``current_env()`` instead of
``os.environ`` directly.  ``current_env()`` returns ``os.environ`` by default
(so ``monkeypatch.setenv`` in engine-level tests keeps working), and returns an
injected mapping when ``runtime_environment(...)`` is active (the production
path — dev runners inject ``settings.runtime_env()``).

This lives in ``kernel`` (not ``domain``) so the ``os.environ`` reference stays
out of ``domain/``/``adapters/``, satisfying the convergence grep.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Mapping

_runtime_env: ContextVar[Mapping[str, str] | None] = ContextVar(
    "jonex_wiki_runtime_env", default=None
)


def current_env() -> Mapping[str, str]:
    """The active runtime environment (injected mapping, else ``os.environ``)."""
    environment = _runtime_env.get()
    return os.environ if environment is None else environment


@contextmanager
def runtime_environment(environment: Mapping[str, str] | None) -> Iterator[None]:
    """Temporarily override the runtime environment for the current context."""
    token = _runtime_env.set(environment)
    try:
        yield
    finally:
        _runtime_env.reset(token)


__all__ = ["current_env", "runtime_environment"]
