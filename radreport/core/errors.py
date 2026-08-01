"""Every error this system raises on purpose, kept apart from HTTP so the API layer maps them to status codes.

Defines: RadReportError and its subclasses, covering tenancy (TenancyError, NoTenantContext,
CrossTenantAccess), ingest (IngestRejected, DuplicateRecording), model choice
(ModelResolutionError, UngatedActivation), running a stage (StageFailed, BudgetExceeded),
providers (ProviderError, ProviderSaturated) and training data (EvalSetLeakage,
DeidentificationRequired).
"""

from __future__ import annotations


class RadReportError(Exception):
    """Base for everything this system raises deliberately."""


# ---------------------------------------------------------------- tenancy ---
class TenancyError(RadReportError):
    """Base for the isolation invariants."""


class NoTenantContext(TenancyError):
    """A tenant-scoped operation ran without `app.current_tenant_id` set."""


class CrossTenantAccess(TenancyError):
    """An operation tried to span two tenants in one unit of work."""


# ---------------------------------------------------------------- ingest ----
class IngestRejected(RadReportError):
    """Audio failed a quality gate or a format rule."""

    def __init__(self, reason: str, code: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code


class DuplicateRecording(RadReportError):
    """`recording.content_hash` already present — idempotent no-op."""

    def __init__(self, content_hash: str, recording_id: str) -> None:
        super().__init__(f"recording {recording_id} already holds hash {content_hash}")
        self.content_hash = content_hash
        self.recording_id = recording_id


# ---------------------------------------------------------- model config ----
class ModelResolutionError(RadReportError):
    """No active `task_model_assignment` for (tenant, task_key)."""


class UngatedActivation(RadReportError):
    """Activation attempted without a gold-set `eval_run`."""


# ------------------------------------------------------------- pipeline -----
class StageFailed(RadReportError):
    def __init__(self, stage_name: str, detail: str) -> None:
        super().__init__(f"stage {stage_name} failed: {detail}")
        self.stage_name = stage_name
        self.detail = detail


class BudgetExceeded(RadReportError):
    """`pipeline_run.total_cost_usd` passed its cap — hard abort."""

    def __init__(self, spent_usd: float, cap_usd: float) -> None:
        super().__init__(f"pipeline spend ${spent_usd:.4f} exceeded cap ${cap_usd:.4f}")
        self.spent_usd = spent_usd
        self.cap_usd = cap_usd


# ------------------------------------------------------------- provider -----
class ProviderError(RadReportError):
    """Upstream ASR/LLM failure, after the adapter's own retries."""


class ProviderSaturated(ProviderError):
    """Backpressure: the adapter's concurrency limiter refused the call."""


# ----------------------------------------------------------------- eval -----
class EvalSetLeakage(RadReportError):
    """A training snapshot intersected the eval set."""


class DeidentificationRequired(RadReportError):
    """`is_deidentified = false` on data bound for an external API."""


# ----------------------------------------------------------- onboarding -----
class OnboardingError(RadReportError):
    """Base for onboarding refusals."""


class BatchStateError(OnboardingError):
    """An import batch was moved through an illegal status transition."""

    def __init__(self, batch_id: str, current: str, target: str) -> None:
        super().__init__(f"import_batch {batch_id}: cannot move {current!r} -> {target!r}")
        self.batch_id = batch_id
        self.current = current
        self.target = target


class BatchBlocked(OnboardingError):
    """`blocking_issue_count > 0` — `applied` is refused."""

    def __init__(self, batch_id: str, blocking_issue_count: int) -> None:
        super().__init__(f"import_batch {batch_id} has {blocking_issue_count} unresolved blocking issue(s); resolve every block-severity finding before applying")
        self.batch_id = batch_id
        self.blocking_issue_count = blocking_issue_count


class ApprovalRequired(OnboardingError):
    """Nothing reaches `template_version` without radiologist approval."""


class ConsentRequired(OnboardingError):
    """A voiceprint was offered with no enrollment consent behind it."""
