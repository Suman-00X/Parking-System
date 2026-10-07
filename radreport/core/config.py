"""All runtime settings, read from the environment, including the pinned model identifiers.

Defines: the settings groups (DatabaseSettings, EventSettings, StorageSettings, AudioGateSettings, LLMSettings,
ASRSettings, ObservabilitySettings, DemoAccount) gathered into one Settings object, reached through
get_settings.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseModel):
    """Connection pool and query-logging behaviour for every engine the app opens."""

    pool_size: int = Field(default=30, ge=1, le=500)
    """Connections each worker keeps open. Workers x (pool_size + max_overflow) must stay under Postgres max_connections, or behind PgBouncer."""

    max_overflow: int = Field(default=10, ge=0, le=500)
    """Extra connections a worker may open under a burst; closed again when returned."""

    async_pool_size: int = Field(default=5, ge=1, le=200)
    """The async engine's own pool, per worker. Small, because it is counted against max_connections alongside the sync pool."""

    async_max_overflow: int = Field(default=5, ge=0, le=200)
    side_pool_size: int = Field(default=4, ge=1, le=50)
    """The access middleware's own pool, per worker: rate-limit counts and admin sign-in checks."""

    threadpool_size: int = Field(default=100, ge=8, le=2000)
    """Worker threads for sync handlers, per process. Above the pool size, so work that needs no connection is never starved by work waiting for one."""

    workers_hint: int = Field(default=2, ge=1, le=256)
    """How many app processes share the database; only used to warn when their pools together could exceed max_connections."""

    pool_timeout_seconds: float = Field(default=10.0, gt=0)
    """How long a request waits for a free connection before failing, instead of queueing forever."""

    pool_recycle_seconds: int = 1800
    connect_timeout_seconds: int = Field(default=5, ge=1, le=120)
    """How long opening a connection may take, so a health check against an unreachable database answers instead of hanging."""
    echo: bool | None = None
    """Log every statement. Unset means on in the test environment only."""

    slow_query_ms: float = Field(default=100.0, ge=0)
    """A statement slower than this is logged as slow_query, with its timing and the route that ran it."""

    replica_url: str | None = None
    """A streaming read replica (RADREPORT_DB__REPLICA_URL). Dashboards, cost views, lists and exports read from it; unset, they read from the primary."""

    replica_max_lag_seconds: float = Field(default=10.0, gt=0)
    """Replica reads go back to the primary while the replica is further behind than this."""

    read_your_writes_seconds: int = Field(default=10, ge=1, le=600)
    """After a browser writes, its reads stay on the primary this long, so it sees its own change."""

    shards: dict[str, str] = Field(default_factory=dict)
    """Shard name -> database URL (RADREPORT_DB__SHARDS, JSON). Two or more turn sharding by lab on; see db/sharding.py."""

    pgbouncer: bool = False
    """Connect through PgBouncer in transaction mode: server-side prepared statements are turned off, since the next transaction may land on another server connection."""


class EventSettings(BaseModel):
    """Where the outbox relay sends domain events."""

    bus: str = Field(default="postgres", pattern="^(postgres|kafka)$")
    """`postgres` applies events to in-process consumers; `kafka` produces them to topics for consumers anywhere."""

    kafka_bootstrap: str = "localhost:9092"
    topic_prefix: str = "radreport."
    relay_batch_size: int = Field(default=100, ge=1, le=10_000)


class StorageSettings(BaseModel):
    """S3-compatible object store for audio, kept as lossless FLAC or WAV indefinitely."""

    backend: str = Field(default="s3", pattern="^(s3|local)$")
    """`local` keeps objects in a folder (local_path); refused outside local, test and development."""

    local_path: str = ".storage"
    bucket: str = "radreport-audio"
    endpoint_url: str | None = None
    access_key_id: str | None = None
    secret_access_key: str | None = None
    region: str = "us-east-1"
    sse_kms_key_id: str | None = None
    """None => SSE-S3 (AES256). Production must set a KMS key."""

    signed_url_seconds: int = 60
    """How long a signed audio link stays valid. Audio is never cached or served through a CDN; the browser fetches it from the bucket directly."""


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

    response_cache: str = Field(default="postgres", pattern="^(off|postgres|shared)$")
    """Answer a repeat of an identical request from a stored reply: `postgres` (per lab, under row-level security), `shared` (the Redis or memory cache), or `off`."""

    response_cache_ttl_hours: float = Field(default=168.0, gt=0)

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


class LabAuthSettings(BaseModel):
    token_secret: str = ""
    """Signs lab users' access tokens. Required outside local/test/development (RADREPORT_LAB_AUTH__TOKEN_SECRET)."""

    access_ttl_minutes: int = 15
    refresh_ttl_days: int = 14


class DemoAccount(BaseModel):
    """A sign-in listed on the login page's test-credentials tab, for public demo deployments only."""

    label: str
    email: str
    password: str
    role: str = "support"
    """`support` is read-only; a public `product_admin` login lets any visitor lock the others out. Only read-only roles (support, auditor) are ever shown."""

    lab: str | None = None
    """For a lab account (an `auditor`), the lab's slug; empty for an admin-panel account."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RADREPORT_", env_nested_delimiter="__", env_file=".env", extra="ignore")

    environment: str = "local"
    database_url: str = "postgresql+psycopg://radreport:radreport@localhost:5433/radreport"
    test_database_url: str | None = None
    redis_url: str | None = None
    """The shared cache (RADREPORT_REDIS_URL). Unset, each worker caches in its own memory with short TTLs."""

    db: DatabaseSettings = Field(default_factory=DatabaseSettings)
    events: EventSettings = Field(default_factory=EventSettings)

    storage: StorageSettings = Field(default_factory=StorageSettings)
    audio: AudioGateSettings = Field(default_factory=AudioGateSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    asr: ASRSettings = Field(default_factory=ASRSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    lab_auth: LabAuthSettings = Field(default_factory=LabAuthSettings)

    # Provider API keys are deliberately **not** settings.

    allow_phi_on_this_machine: bool = False
    """Developer machines get synthetic data only. See devtools/."""

    trusted_origins: list[str] = Field(default_factory=list)
    """Extra origins (scheme://host[:port]) allowed to send state-changing admin requests, e.g. a public hostname in front of a proxy. Set as JSON in RADREPORT_TRUSTED_ORIGINS."""

    seed_on_start: bool = True
    """Seed a database with no labs when the API starts (RADREPORT_SEED_ON_START); a database with any lab is never touched."""

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
