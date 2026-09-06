"""Who may do what on the review screen; the four roles differ in real ways, not by degree.

Order: build the caller's Reviewer from their roles, then require checks one Permission and
raises PermissionDenied if it is missing.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from radreport.core.types import DISPLAY_LABELS, UserRole


class Permission(StrEnum):
    """What someone may do on the review surface."""

    VIEW_QUEUE = "view_queue"
    VIEW_DRAFT = "view_draft"
    REVISE_DRAFT = "revise_draft"
    SIGN_REPORT = "sign_report"
    GRADE_REPORT = "grade_report"
    """G0–G4 on the schedule. A clinical judgement about clinical significance, so it belongs to a radiologist."""

    REPORT_USELESS = "report_useless"
    VIEW_AUDIT_LOG = "view_audit_log"
    ACKNOWLEDGE_ALERT = "acknowledge_alert"


#: the role matrix. Additive: a user holding two roles gets the union.
ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    UserRole.TRANSCRIPTIONIST: frozenset(
        {
            Permission.VIEW_QUEUE,
            Permission.VIEW_DRAFT,
            Permission.REVISE_DRAFT,
            # Deliberately absent: SIGN_REPORT, GRADE_REPORT.
            Permission.REPORT_USELESS,
        }
    ),
    UserRole.RADIOLOGIST: frozenset({Permission.VIEW_QUEUE, Permission.VIEW_DRAFT, Permission.REVISE_DRAFT, Permission.SIGN_REPORT, Permission.GRADE_REPORT, Permission.REPORT_USELESS, Permission.ACKNOWLEDGE_ALERT}),
    UserRole.LAB_ADMIN: frozenset(
        {
            # Queue visibility for operational oversight — who is waiting, how
            # long — without any access to clinical content.
            Permission.VIEW_QUEUE
        }
    ),
    UserRole.AUDITOR: frozenset(
        {
            Permission.VIEW_QUEUE,
            Permission.VIEW_DRAFT,
            Permission.VIEW_AUDIT_LOG,
            # Read-only by construction: an auditor who can alter what they are
            # auditing is not an auditor.
        }
    ),
}


@dataclass(frozen=True, slots=True)
class Reviewer:
    """Who is acting, and what they may do."""

    user_id: object
    roles: tuple[str, ...]

    @property
    def permissions(self) -> frozenset[str]:
        granted: set[str] = set()
        for role in self.roles:
            granted |= ROLE_PERMISSIONS.get(role, frozenset())
        return frozenset(granted)

    def can(self, permission: str) -> bool:
        return permission in self.permissions

    @property
    def is_radiologist(self) -> bool:
        return UserRole.RADIOLOGIST in self.roles

    @property
    def display_role(self) -> str:
        """What the screen calls them."""
        for role in (UserRole.RADIOLOGIST, UserRole.TRANSCRIPTIONIST, UserRole.LAB_ADMIN):
            if role in self.roles:
                return DISPLAY_LABELS.get(role, role)
        return DISPLAY_LABELS.get(self.roles[0], self.roles[0]) if self.roles else "Unknown"


class PermissionDenied(Exception):
    """A reviewer attempted something their role does not permit."""

    def __init__(self, permission: str, roles: tuple[str, ...]) -> None:
        super().__init__(f"{permission!r} is not permitted for role(s) {', '.join(roles) or 'none'}")
        self.permission = permission
        self.roles = roles


def require(reviewer: Reviewer, permission: str) -> None:
    """Assert a permission, or raise."""
    if not reviewer.can(permission):
        raise PermissionDenied(permission, reviewer.roles)
