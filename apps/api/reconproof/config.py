"""Runtime configuration.

Defaults are chosen so that ``git clone && make demo`` works with no external
services: SQLite on the local filesystem, no LLM, no network. Every heavier
dependency (PostgreSQL, Ollama, an Anthropic key, sentence-transformers) is
opt-in and degrades to a deterministic path when absent.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATA_DIR = REPO_ROOT / ".reconproof"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RECONPROOF_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: str = "local"
    debug: bool = False

    data_dir: Path = DEFAULT_DATA_DIR
    #: SQLAlchemy URL. Point this at PostgreSQL to switch engines; the schema
    #: and every query are engine-portable.
    database_url: str = ""
    sql_echo: bool = False

    api_host: str = "127.0.0.1"
    api_port: int = 8817
    cors_origins: list[str] = Field(default_factory=lambda: ["http://127.0.0.1:43917"])

    max_upload_bytes: int = 32 * 1024 * 1024
    allowed_upload_suffixes: tuple[str, ...] = (".csv", ".json", ".jsonl")

    # ---- matching ---------------------------------------------------------
    #: Candidate generation window. Settlements land T+2 to T+3 for most Indian
    #: merchants; the wider default absorbs weekends and bank holidays.
    candidate_day_window: int = 10
    candidate_amount_tolerance_bps: int = 500
    max_candidates_per_record: int = 25

    #: Precision the automatic-accept threshold is calibrated to hit on
    #: validation data. Recall is whatever remains: in reconciliation an
    #: unresolved row is cheap and a wrong auto-match is expensive.
    target_precision: float = 0.99
    #: Conformal risk budget. A candidate is only auto-accepted when its
    #: estimated error probability is at or below this.
    risk_budget: float = 0.01
    review_score_floor: float = 0.35

    # ---- semantic layer ---------------------------------------------------
    enable_semantic_matching: bool = False
    semantic_model_name: str = "BAAI/bge-small-en-v1.5"
    semantic_batch_size: int = 64

    # ---- agent ------------------------------------------------------------
    llm_provider: str = "auto"
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen3:4b"
    ollama_timeout_seconds: float = 90.0
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-sonnet-4-6"

    agent_max_iterations: int = 8
    agent_max_tool_calls: int = 24
    agent_max_output_retries: int = 1
    agent_search_row_cap: int = 50

    # ---- monitoring -------------------------------------------------------
    drift_psi_threshold: float = 0.2
    #: Multiplier applied to the risk budget when drift is detected. Below 1.0
    #: it tightens automation rather than loosening it.
    drift_risk_tightening: float = 0.5
    anomaly_contamination: float = 0.05

    # ---- razorpay integration -----------------------------------------------
    #: Test-mode (or live) key pair. Absent by default: the demo is fully
    #: functional on file uploads alone, so a missing key disables only the
    #: optional sync endpoint rather than anything the product depends on.
    razorpay_key_id: str | None = None
    razorpay_key_secret: str | None = None
    #: Required to accept webhook deliveries. Verification fails closed: no
    #: secret configured means every webhook is rejected, not accepted
    #: unverified, mirroring how the policy engine fails closed when its
    #: document is unavailable.
    razorpay_webhook_secret: str | None = None
    razorpay_api_base_url: str = "https://api.razorpay.com/v1"
    razorpay_sync_timeout_seconds: float = 30.0

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return f"sqlite+pysqlite:///{self.data_dir / 'reconproof.db'}"

    @property
    def upload_dir(self) -> Path:
        path = self.data_dir / "uploads"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def artifact_dir(self) -> Path:
        path = self.data_dir / "artifacts"
        path.mkdir(parents=True, exist_ok=True)
        return path


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
