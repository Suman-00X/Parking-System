"""Cache keys that always carry the lab they belong to, so a cached value cannot be served to another lab.

Defines: GLOBAL (the scope of values that belong to no lab), CacheKey, and key (the only way to
build one).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final

#: The scope of a value that belongs to no lab, such as an admin session. Spelled out so a missing tenant cannot pass for it.
GLOBAL: Final = "global"


@dataclass(frozen=True, slots=True)
class CacheKey:
    """`namespace` + the owning lab + the parts that pick the value inside it."""

    namespace: str
    scope: str
    parts: tuple[str, ...]

    def render(self) -> str:
        """The flat string a shared store keys on."""
        return ":".join(("radreport", self.namespace, self.scope, *self.parts))


def key(namespace: str, tenant_id: uuid.UUID | str, *parts: object) -> CacheKey:
    """Build a key; `tenant_id` is required, and GLOBAL must be passed explicitly for values that belong to no lab."""
    if tenant_id is None or tenant_id == "":
        raise ValueError(f"cache key {namespace!r} has no tenant; RLS cannot catch a cache that mixes labs, so every key names one (or GLOBAL)")
    if isinstance(tenant_id, str) and tenant_id != GLOBAL:
        tenant_id = uuid.UUID(tenant_id)
    return CacheKey(namespace=namespace, scope=str(tenant_id), parts=tuple(str(p) for p in parts))
