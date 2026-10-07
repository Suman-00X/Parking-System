"""Term lookup against a lab's lexicon: exact wording, short forms, heard variants and synonyms; never another lab's."""

from __future__ import annotations

import pytest

from radreport.cache import filters
from radreport.db.models.knowledge import LexiconTerm
from radreport.db.session import tenant_session
from radreport.knowledge.term_lookup import find_term, is_unknown
from radreport.onboarding.lexicon import get_or_create_tenant_lexicon, record_surface_variants

pytestmark = pytest.mark.db


def test_a_term_is_found_by_wording_short_form_variant_or_synonym(migrated_db: str, two_tenants) -> None:
    lab, other = two_tenants
    with tenant_session(lab, url=migrated_db) as session:
        lexicon = get_or_create_tenant_lexicon(session, lab)
        term = LexiconTerm(tenant_id=lab, lexicon_set_id=lexicon.id, canonical_form="consolidation", short_form="CONS", term_type="pathology", phonetic_key_primary="KNSL")
        session.add(term)
        session.flush()
        record_surface_variants(session, tenant_id=lab, term_id=term.id, variants={"consolidations": 3})
        filters.forget_lexicon(lab)
        assert find_term(session, lab, "Consolidation").how == "exact"
        assert find_term(session, lab, "cons").how == "short_form"
        assert find_term(session, lab, "consolidations").how == "variant"
        synonym = find_term(session, lab, "infiltrate")
        assert synonym is not None and synonym.how == "synonym" and synonym.confidence == 0.95 and synonym.canonical_form == "consolidation"
        assert not is_unknown(session, lab, "airspace opacity")
        assert is_unknown(session, lab, "crazy paving pattern")
    with tenant_session(other, url=migrated_db) as session:
        assert find_term(session, other, "consolidation") is None
