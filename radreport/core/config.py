"""All runtime settings, read from the environment, including the pinned model identifiers.

Defines: the settings groups (StorageSettings, AudioGateSettings, LLMSettings, ASRSettings,
ObservabilitySettings, DemoAccount) gathered into one Settings object, reached through get_settings.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class StorageSettings(BaseModel):
    """S3-compatible object store for audio, kept as lossless FLAC or WAV indefinitely."""

    bucket: str = "radreport-audio"
    endpoint_url: str | None = None
    access_key_id: str | None = None
    secret_access_key: str | None = None
    region: str = "us-east-1"
    sse_kms_key_id: str | None = None
    """None => SSE-term mining. Production must set a KMS key."""


class AudioGateSettings(BaseModel):
    """Ingest quality gates. Warn-level bounds are separate from rejects."""

    allowed_formats: tuple[str, ...] = ("flac", "wav")
    """Lossy compression is rejected at ingest, irreversibly destructive."""

    min_sample_rate_hz: int = 16_000
    min_duration_seconds: float = 5.0
    max_duration_seconds: float = 3_600.0
    min_snr_db: float = 10.0
    max_silence_ratio: float = 0.85
    max_upload_bytes: int = 512 * 1024 * 1024


class LLMSettings(BaseModel):
    """Adapter behaviour. See `adapters/llm` for the caching discipline."""

    default_max_tokens: int = 4_096
    max_concurrency: int = 8
    """Backpressure against the vendor ( gap)."""

    max_attempts: int = 4
    circuit_breaker_failure_threshold: int = 5
    circuit_breaker_reset_seconds: float = 30.0

    enable_prompt_caching: bool = True
    """Plan Tier 1. Structurally guaranteed by the k=3 sample repeats."""

    pin_exemplars: bool = False
    """Plan Tier 2. Off until the week-6 pinned-vs-retrieved gate run."""

    per_run_budget_usd: float = 0.50
    """Hard abort on `pipeline_run.total_cost_usd`."""


class ASRSettings(BaseModel):
    engine: str = "whisper_local"
    engine_version: str = "v0-stub"
    max_concurrency: int = 4


class ObservabilitySettings(BaseModel):
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_host: str = "https://cloud.langfuse.com"
    log_level: str = "INFO"
    json_logs: bool = True


class DemoAccount(BaseModel):
    """A sign-in listed on the login page's test-credentials tab, for public demo deployments only."""

    label: str
    email: str
    password: str
    role: str = "support"
    """`support` is read-only; a public `product_admin` login lets any visitor lock the others out."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RADREPORT_", env_nested_delimiter="__", env_file=".env", extra="ignore")

    environment: str = "local"
    database_url: str = "postgresql+psycopg://radreport:radreport@localhost:5433/radreport"
    test_database_url: str | None = None

    storage: StorageSettings = Field(default_factory=StorageSettings)
    audio: AudioGateSettings = Field(default_factory=AudioGateSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    asr: ASRSettings = Field(default_factory=ASRSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)

    # Provider API keys are deliberately **not** settings.

    allow_phi_on_this_machine: bool = False
    """Developer machines get synthetic data only. See devtools/."""

    demo_accounts: list[DemoAccount] = Field(default_factory=list)
    """Empty => the login page has no credentials tab. Set as JSON in RADREPORT_DEMO_ACCOUNTS."""

    @field_validator("database_url")
    @classmethod
    def _require_psycopg(cls, v: str) -> str:
        if not v.startswith("postgresql"):
            raise ValueError("only PostgreSQL is supported (pgvector, RLS)")
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
