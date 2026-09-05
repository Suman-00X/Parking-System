"""Who may do what on the review screen, and how each kind of edit is classified."""

from __future__ import annotations

import uuid

import pytest

from radreport.core.types import ErrorCategory, UserRole
from radreport.review.rbac import Permission, PermissionDenied, Reviewer, require
from radreport.review.session import categorise_edit


def _reviewer(*roles: str) -> Reviewer:
    return Reviewer(user_id=uuid.uuid4(), roles=roles)


def test_an_assistant_may_revise_but_may_not_sign() -> None:
    """Two-layer supervision is the entire safety model of the assistant path: the assistant corrects, the radiologist takes responsibility."""
    assistant = _reviewer(UserRole.TRANSCRIPTIONIST)

    assert assistant.can(Permission.REVISE_DRAFT)
    assert not assistant.can(Permission.SIGN_REPORT)
    with pytest.raises(PermissionDenied):
        require(assistant, Permission.SIGN_REPORT)


def test_an_assistant_may_not_grade() -> None:
    """Deciding whether an error was clinically significant is the same judgement as deciding whether the report was right."""
    assert not _reviewer(UserRole.TRANSCRIPTIONIST).can(Permission.GRADE_REPORT)
    assert _reviewer(UserRole.RADIOLOGIST).can(Permission.GRADE_REPORT)


def test_a_lab_admin_has_no_clinical_access() -> None:
    """They are frequently the person with the most system access and the least clinical standing."""
    admin = _reviewer(UserRole.LAB_ADMIN)

    assert admin.can(Permission.VIEW_QUEUE)
    assert not admin.can(Permission.VIEW_DRAFT)
    assert not admin.can(Permission.REVISE_DRAFT)
    assert not admin.can(Permission.SIGN_REPORT)


def test_an_auditor_is_read_only() -> None:
    """An auditor who can alter what they are auditing is not an auditor."""
    auditor = _reviewer(UserRole.AUDITOR)

    assert auditor.can(Permission.VIEW_DRAFT)
    assert auditor.can(Permission.VIEW_AUDIT_LOG)
    assert not auditor.can(Permission.REVISE_DRAFT)
    assert not auditor.can(Permission.SIGN_REPORT)


def test_roles_are_additive() -> None:
    """A user holding two roles gets the union."""
    both = _reviewer(UserRole.TRANSCRIPTIONIST, UserRole.RADIOLOGIST)
    assert both.can(Permission.SIGN_REPORT)
    assert both.can(Permission.REVISE_DRAFT)


def test_the_assistant_role_is_relabelled_not_renamed() -> None:
    """Plan: the schema keeps `transcriptionist`; the screen says "Radiologist assistant". Relabel, do not diverge from the source."""
    assert _reviewer(UserRole.TRANSCRIPTIONIST).display_role == "Radiologist assistant"
    assert _reviewer(UserRole.RADIOLOGIST).display_role == "Radiologist"


# ============================================== edit categorisation ===
@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        # A laterality flip is a G4 class and must never be filed as style,
        # however many other words also changed.
        ("left kidney", "right kidney", ErrorCategory.LATERALITY),
        ("the left renal cyst", "the right renal lesion", ErrorCategory.LATERALITY),
        ("no effusion", "effusion present", ErrorCategory.NEGATION),
        ("3.2 cm", "2.3 cm", ErrorCategory.ASR_NUMBER),
        # The system asserted something the reviewer removed.
        ("simple cyst", "", ErrorCategory.HALLUCINATION),
        ("", "simple cyst", ErrorCategory.EXTRACTION_MISS),
        # A near-homophone is a mishearing, which is what makes it an ASR
        # training example rather than a rewording.
        ("hepatic", "hepatik", ErrorCategory.ASR_TERM),
        ("the liver is enlarged", "hepatomegaly is present", ErrorCategory.STYLE),
    ],
)
def test_edit_categorisation_follows_clinical_consequence(before, after, expected) -> None:
    assert categorise_edit(before, after) == expected


def test_categorisation_is_deterministic() -> None:
    """The edit corpus is training data; a mislabelled one is worse than a small one."""
    assert categorise_edit("left", "right") == categorise_edit("left", "right")
