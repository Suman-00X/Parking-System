"""A lab with Hindi on finds its English terms from Devanagari and Hinglish; a lab without it does not."""

from __future__ import annotations

import uuid

import pytest

from radreport.core import system_config
from radreport.db.models.knowledge import LexiconTerm
from radreport.db.session import tenant_session
from radreport.knowledge.term_lookup import find_term, is_unknown
from radreport.onboarding.lexicon import get_or_create_tenant_lexicon
from radreport.pipeline.stages.providers import load_tenant_knowledge

pytestmark = pytest.mark.db


def test_lab_languages_reach_lookup_and_the_pipeline(migrated_db: str, two_tenants) -> None:
    lab, other = two_tenants
    for tenant in (lab, other):
        with tenant_session(tenant, url=migrated_db) as session:
            lexicon = get_or_create_tenant_lexicon(session, tenant)
            session.add_all([LexiconTerm(tenant_id=tenant, lexicon_set_id=lexicon.id, canonical_form=form, term_type="pathology", phonetic_key_primary="X") for form in ("pneumonia", "calculus")])
    with tenant_session(lab, url=migrated_db) as session:
        actor = uuid.uuid4()
        system_config.set_value(session, "languages.hi", 1, actor_id=actor, tenant_id=lab)
        system_config.set_value(session, "languages.hi_latin", 1, actor_id=actor, tenant_id=lab)
    with tenant_session(lab, url=migrated_db) as session:
        match = find_term(session, lab, "निमोनिया")
        assert match is not None and match.canonical_form == "pneumonia" and match.how == "translated_hi" and match.confidence == 0.9
        assert find_term(session, lab, "Pathri").canonical_form == "calculus"  # type: ignore[union-attr]
        assert not is_unknown(session, lab, "pathri") and is_unknown(session, lab, "पित्ताशय"), "a term the lexicon lacks is still new, in any language"
        knowledge = load_tenant_knowledge(session, lab)
        assert knowledge.languages.languages == ("hi",) and knowledge.languages.latin_hindi and not knowledge.languages.online
    with tenant_session(other, url=migrated_db) as session:
        assert find_term(session, other, "निमोनिया") is None and not load_tenant_knowledge(session, other).languages.any
