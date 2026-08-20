"""Builds prompts so the unchanging parts come first, which is what lets a provider reuse its cache between calls.

Order: assemble the stable blocks first (system_block, schema_block, section_block,
exemplar_block), then the per-report content (VolatileBlock), into one PromptBundle.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class Stability(IntEnum):
    """How often a layer changes. Lower sorts earlier in the prefix."""

    SYSTEM = 0
    """System prompt + provenance rules. Stable across everything."""

    SCHEMA = 1
    """Compiled template JSON schema. Stable across all reports on a template."""

    SECTION = 2
    """Section instruction. Stable across all reports on (template, section)."""

    EXEMPLARS = 3
    """Few-shot exemplars. Stable **only if pinned** — see module docstring."""


class PromptBlock(BaseModel):
    """One addressable chunk of prompt text."""

    stability: Stability
    text: str
    label: str = ""
    """For tracing and for the cache-hit attribution in `stage_execution`."""


class VolatileBlock(BaseModel):
    """Per-report content. Never cacheable, always last."""

    text: str
    label: str = ""


class PromptBundle(BaseModel):
    """A prompt that cannot be assembled in a cache-hostile order."""

    stable: list[PromptBlock] = Field(default_factory=list)
    volatile: list[VolatileBlock] = Field(default_factory=list)
    cache: bool = True

    @model_validator(mode="after")
    def _require_volatile_content(self) -> PromptBundle:
        if not self.stable and not self.volatile:
            raise ValueError("a prompt bundle needs at least one block")
        return self

    def stable_text(self) -> str:
        return "\n\n".join(b.text for b in sorted(self.stable, key=lambda b: b.stability))

    def volatile_text(self) -> str:
        return "\n\n".join(b.text for b in self.volatile)

    def cache_key(self) -> str:
        """Identifies the cacheable prefix."""
        from radreport.core.hashing import hash_text

        return hash_text(self.stable_text())

    def to_content_blocks(self) -> list[dict[str, Any]]:
        """Anthropic content blocks with `cache_control` on the prefix."""
        blocks: list[dict[str, Any]] = []
        ordered = sorted(self.stable, key=lambda b: b.stability)

        for index, block in enumerate(ordered):
            entry: dict[str, Any] = {"type": "text", "text": block.text}
            is_last_stable = index == len(ordered) - 1
            if self.cache and is_last_stable:
                entry["cache_control"] = {"type": "ephemeral"}
            blocks.append(entry)

        for block in self.volatile:
            blocks.append({"type": "text", "text": block.text})

        return blocks

    def estimated_cacheable_chars(self) -> int:
        """Rough prefix size, for the-item-4 measurement."""
        return len(self.stable_text())


def system_block(text: str, *, label: str = "system") -> PromptBlock:
    return PromptBlock(stability=Stability.SYSTEM, text=text, label=label)


def schema_block(text: str, *, label: str = "template_schema") -> PromptBlock:
    return PromptBlock(stability=Stability.SCHEMA, text=text, label=label)


def section_block(text: str, *, label: str = "section_instruction") -> PromptBlock:
    return PromptBlock(stability=Stability.SECTION, text=text, label=label)


def exemplar_block(text: str, *, pinned: bool, label: str = "exemplars") -> PromptBlock:
    """Few-shot exemplars."""
    if not pinned:
        raise ValueError("unpinned exemplars are per-report content and must go in `volatile`; placing them in the stable prefix invalidates the cache on every call ( Tier 2)")
    return PromptBlock(stability=Stability.EXEMPLARS, text=text, label=label)


Effort = Literal["low", "medium", "high"]
