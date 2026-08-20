"""The prompt's stable-before-volatile ordering is enforced by the types, not by convention."""

from __future__ import annotations

import pytest

from radreport.adapters.llm.prompt import PromptBundle, Stability, VolatileBlock, exemplar_block, schema_block, section_block, system_block


def _bundle(*, pinned_exemplars: bool = True) -> PromptBundle:
    stable = [schema_block("{json schema}"), system_block("You are a radiology extraction assistant."), section_block("Extract the LIVER section.")]
    if pinned_exemplars:
        stable.append(exemplar_block("<exemplars>", pinned=True))
    return PromptBundle(stable=stable, volatile=[VolatileBlock(text="<transcript with char offsets>", label="transcript")])


def test_stable_blocks_are_ordered_most_stable_first() -> None:
    """Declaration order must not matter; stability must."""
    blocks = _bundle().to_content_blocks()
    texts = [b["text"] for b in blocks]
    assert texts[0].startswith("You are a radiology")
    assert texts[1] == "{json schema}"
    assert texts[2].startswith("Extract the LIVER")
    assert texts[3] == "<exemplars>"


def test_transcript_is_always_last() -> None:
    """Volatile content after the breakpoint, always."""
    blocks = _bundle().to_content_blocks()
    assert blocks[-1]["text"].startswith("<transcript")
    assert "cache_control" not in blocks[-1]


def test_exactly_one_cache_breakpoint_at_the_end_of_the_prefix() -> None:
    """The API caches everything up to and including the marked block, so one breakpoint suffices and extra ones mostly waste write cost."""
    blocks = _bundle().to_content_blocks()
    marked = [i for i, b in enumerate(blocks) if "cache_control" in b]
    assert marked == [3], "expected a single breakpoint after the last stable block"


def test_unpinned_exemplars_are_rejected_from_the_stable_region() -> None:
    """Plan Tier 2, made mechanical."""
    with pytest.raises(ValueError, match="volatile"):
        exemplar_block("<retrieved per report>", pinned=False)


def test_cache_key_ignores_volatile_content() -> None:
    """Two reports on the same template share a cacheable prefix."""
    a = _bundle()
    b = _bundle()
    b.volatile = [VolatileBlock(text="<a completely different transcript>")]
    assert a.cache_key() == b.cache_key()


def test_cache_key_changes_when_the_prefix_changes() -> None:
    a = _bundle()
    b = PromptBundle(stable=[system_block("A different system prompt.")], volatile=[VolatileBlock(text="<transcript>")])
    assert a.cache_key() != b.cache_key()


def test_caching_can_be_disabled_without_reordering() -> None:
    bundle = _bundle()
    bundle.cache = False
    assert all("cache_control" not in b for b in bundle.to_content_blocks())
    # Order is unchanged — disabling the cache must not change what the model sees.
    assert [b["text"] for b in bundle.to_content_blocks()] == [b["text"] for b in _bundle().to_content_blocks()]


def test_stability_ordering_is_total() -> None:
    values = [s.value for s in Stability]
    assert values == sorted(values) and len(set(values)) == len(values)


def test_empty_bundle_is_rejected() -> None:
    with pytest.raises(ValueError):
        PromptBundle()


def test_cacheable_prefix_is_the_bulk_of_the_prompt() -> None:
    """Sanity check on the premise that input is ~67% of the LLM bill and the prefix is most of it."""
    bundle = PromptBundle(stable=[system_block("s" * 4000), schema_block("c" * 6000)], volatile=[VolatileBlock(text="v" * 1500)])
    total = bundle.estimated_cacheable_chars() + len(bundle.volatile_text())
    assert bundle.estimated_cacheable_chars() / total > 0.8
