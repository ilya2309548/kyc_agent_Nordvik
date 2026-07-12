"""Application settings.

All runtime configuration comes from environment variables (or `.env`).
Model identifiers use the LangChain ``init_chat_model`` format
``provider:model_name``; the special provider ``fake`` selects the
deterministic offline implementation (see SPEC.md section 9).
"""

from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- LLM right-sizing per pipeline step (SPEC 4.9) ---
    # Offline-first defaults: the stack must boot and run without API keys.
    # Production values (Anthropic models) are documented in .env.example.
    router_model: str = Field(
        "fake:router", validation_alias=AliasChoices("MODEL_ROUTER", "ROUTER_MODEL")
    )
    extractor_model: str = Field(
        "fake:extractor", validation_alias=AliasChoices("MODEL_EXTRACTOR", "EXTRACTOR_MODEL")
    )
    validator_model: str = Field(
        "fake:validator", validation_alias=AliasChoices("MODEL_VALIDATOR", "VALIDATOR_MODEL")
    )
    risk_model: str = Field(
        "fake:risk", validation_alias=AliasChoices("MODEL_RISK", "RISK_MODEL")
    )
    fallback_model: str = Field(
        "fake:fallback", validation_alias=AliasChoices("MODEL_FALLBACK", "FALLBACK_MODEL")
    )

    # --- Bounded execution (SPEC 4.7) ---
    max_step_retries: int = 2
    graph_recursion_limit: int = 25

    # --- Business thresholds (SPEC 7) ---
    confidence_threshold: float = 0.75
    high_volume_threshold_individual_eur: Decimal = Decimal(10_000)
    high_volume_threshold_business_eur: Decimal = Decimal(50_000)
    name_fuzzy_critical: float = 0.85
    name_fuzzy_warning: float = 0.95
    address_fuzzy_threshold: float = 0.70

    # --- Persistence (SPEC 4.6, 6.5) ---
    persistence_backend: Literal["memory", "postgres"] = "postgres"
    database_url: str = "postgresql://kyc:kyc@localhost:5432/kyc"

    # --- Mock registries (SPEC assumptions 12.2): test-only failure injection ---
    registry_failure_rate: float = 0.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
