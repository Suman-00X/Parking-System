"""Imports every ORM model, which is what registers all the tables on the shared metadata."""

from radreport.db.base import Base
from radreport.db.models.adaptation import ModelAdaptationRun, TrainingCorpusSnapshot, VerbatimTranscript
from radreport.db.models.asr import AsrRun, AsrSegment, Transcript, TranscriptUtterance
from radreport.db.models.evaluation import EvalItem, EvalResult, EvalRun, EvalSet
from radreport.db.models.events import ConsumedEvent, OutboxEvent
from radreport.db.models.identity import AppUser, LabRefreshToken, Patient, RadiologistProfile, Study
from radreport.db.models.ingestion import Recording
from radreport.db.models.jobs import Job
from radreport.db.models.knowledge import AutonomyClass, LexiconSet, LexiconSurfaceVariant, LexiconTerm, SpeakerTermBias, Template, TemplateField, TemplateVersion
from radreport.db.models.llm_cache import LLMResponseCache
from radreport.db.models.modelconfig import ModelDefinition, ModelProvider, TaskModelAssignment, TaskModelAssignmentLog
from radreport.db.models.onboarding import BoilerplateCandidate, CollisionAuditFinding, CorpusReport, CorpusReportTemplateMap, ImportArtifact, ImportBatch, LexiconMiningRun, OnboardingReadinessCheck, TemplateImportCandidate, TemplateMergeProposal
from radreport.db.models.ops import LabShard, SystemConfig
from radreport.db.models.orchestration import AuditLog, PipelineRun, StageExecution
from radreport.db.models.reporting import AutonomyObservation, CriticalFindingAlert, CriticalFindingRule, ProvenanceSpan, ReportDraft, ReportFieldValue, RoutingDecision, VerificationFinding
from radreport.db.models.review import DraftUsefulnessReport, EditEvent, FinalReport, ReportRevision
from radreport.db.models.tenancy import PlatformUser, RateLimitCounter, Tenant, TenantBranding, TrainingConsentEventLog

__all__ = [
    "Base",
    # tenancy
    "Tenant",
    "PlatformUser",
    "RateLimitCounter",
    "TenantBranding",
    "TrainingConsentEventLog",
    # identity
    "AppUser",
    "LabRefreshToken",
    "RadiologistProfile",
    "Patient",
    "Study",
    # ingestion
    "Recording",
    # asr
    "AsrRun",
    "AsrSegment",
    "Transcript",
    "TranscriptUtterance",
    # knowledge
    "LexiconSet",
    "LexiconTerm",
    "LexiconSurfaceVariant",
    "AutonomyClass",
    "Template",
    "TemplateVersion",
    "TemplateField",
    "SpeakerTermBias",
    # reporting
    "RoutingDecision",
    "ReportDraft",
    "ReportFieldValue",
    "ProvenanceSpan",
    "VerificationFinding",
    "CriticalFindingRule",
    "CriticalFindingAlert",
    "AutonomyObservation",
    # review
    "ReportRevision",
    "EditEvent",
    "FinalReport",
    "DraftUsefulnessReport",
    # orchestration
    "PipelineRun",
    "StageExecution",
    "AuditLog",
    # evaluation
    "EvalSet",
    "EvalItem",
    "EvalRun",
    "EvalResult",
    # onboarding
    "ImportBatch",
    "ImportArtifact",
    "TemplateImportCandidate",
    "TemplateMergeProposal",
    "CorpusReport",
    "CorpusReportTemplateMap",
    "LexiconMiningRun",
    "CollisionAuditFinding",
    "BoilerplateCandidate",
    "OnboardingReadinessCheck",
    # adaptation
    "VerbatimTranscript",
    "TrainingCorpusSnapshot",
    "ModelAdaptationRun",
    # model config
    "ModelProvider",
    "ModelDefinition",
    "TaskModelAssignment",
    "TaskModelAssignmentLog",
    # operations
    "SystemConfig",
    "LabShard",
    "Job",
    "OutboxEvent",
    "LLMResponseCache",
    "ConsumedEvent",
]
