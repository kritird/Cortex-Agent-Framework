"""In-process registry for code-node handlers.

Code nodes registered via :meth:`cortex.builder.CortexBuilder.node` are plain
Python callables. They usually cannot be addressed by a dotted import path —
they may be defined in ``__main__``, a notebook cell, or a closure — so they
are stored here and referenced from :attr:`TaskTypeConfig.handler` using the
sentinel scheme ``cortex:node:<id>``.

The framework's :meth:`GenericMCPAgent._call_handler` checks
:func:`is_registered_handler` first and falls back to dotted-path import for
handlers authored as ``"my_module.my_function"`` in cortex.yaml.
"""
from typing import Callable, Dict

_SENTINEL_PREFIX = "cortex:node:"

# {handler_id: callable}. Module-global on purpose — a handler registered by a
# CortexBuilder must stay resolvable for the lifetime of the process.
_REGISTRY: Dict[str, Callable] = {}


def register_handler(handler_id: str, fn: Callable) -> str:
    """Register *fn* under *handler_id* and return its sentinel handler path.

    Re-registering the same id (e.g. rebuilding an agent in the same process)
    overwrites the previous callable — last writer wins.
    """
    _REGISTRY[handler_id] = fn
    return _SENTINEL_PREFIX + handler_id


def is_registered_handler(handler_path: str) -> bool:
    """True if *handler_path* refers to an in-process registered code node."""
    return isinstance(handler_path, str) and handler_path.startswith(_SENTINEL_PREFIX)


def resolve_handler(handler_path: str) -> Callable:
    """Return the callable for a sentinel handler path.

    Raises KeyError if the id is unknown (handler registered in a different
    process — e.g. config persisted to YAML then reloaded).
    """
    handler_id = handler_path[len(_SENTINEL_PREFIX):]
    return _REGISTRY[handler_id]
