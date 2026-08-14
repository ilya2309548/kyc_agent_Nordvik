"""Per-step service factory (SPEC 4.9, 9).

Model identifiers are ``provider:model`` strings resolved through
LangChain ``init_chat_model`` — no vendor is hardcoded. The special
provider ``fake`` yields the deterministic offline implementation.
"""

from functools import lru_cache
from typing import Any

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

from kyc_agent.config import Settings
from kyc_agent.llm.base import (
    TEMPERATURE_BY_STEP,
    EvaluatorService,
    ExtractorService,
    PipelineServices,
    PipelineStep,
    RiskNarratorService,
    RouterService,
)
from kyc_agent.llm.fake import FakeEvaluator, FakeExtractor, FakeRiskNarrator, FakeRouter
from kyc_agent.llm.live import LiveEvaluator, LiveExtractor, LiveRiskNarrator, LiveRouter


def _is_fake(model_spec: str) -> bool:
    return model_spec.partition(":")[0] == "fake"


@lru_cache(maxsize=16)
def _chat_model(
    model_spec: str,
    temperature: float,
    base_url: str | None = None,
    api_key: str | None = None,
) -> BaseChatModel:
    provider, _, model_name = model_spec.partition(":")
    if not model_name:
        raise ValueError(f"invalid model spec {model_spec!r}: expected 'provider:model_name'")
    kwargs: dict[str, Any] = {}
    if base_url:
        kwargs["base_url"] = base_url
    if api_key:
        kwargs["api_key"] = api_key
    # init_chat_model is typed as returning Any once **kwargs are passed.
    model: BaseChatModel = init_chat_model(
        model_name, model_provider=provider, temperature=temperature, **kwargs
    )
    return model


def _is_openai(model_spec: str) -> bool:
    return model_spec.partition(":")[0] == "openai"


def _creds(model_spec: str, settings: Settings) -> tuple[str | None, str | None]:
    """OpenAI-compatible gateway credentials, only for the openai provider."""
    if _is_openai(model_spec):
        return settings.openai_base_url, settings.openai_api_key
    return None, None


def _structured_method(model_spec: str) -> str | None:
    """Strict json_schema for openai-compatible gateways; default elsewhere."""
    return "json_schema" if _is_openai(model_spec) else None


def _model_for(model_spec: str, step: PipelineStep, settings: Settings) -> BaseChatModel:
    base_url, api_key = _creds(model_spec, settings)
    return _chat_model(model_spec, TEMPERATURE_BY_STEP[step], base_url, api_key)


def _router(model_spec: str, settings: Settings) -> RouterService:
    if _is_fake(model_spec):
        return FakeRouter()
    model = _model_for(model_spec, PipelineStep.ROUTER, settings)
    return LiveRouter(model, _structured_method(model_spec))


def _extractor(model_spec: str, settings: Settings) -> ExtractorService:
    if _is_fake(model_spec):
        return FakeExtractor()
    model = _model_for(model_spec, PipelineStep.EXTRACTOR, settings)
    return LiveExtractor(model, _structured_method(model_spec))


def _evaluator(model_spec: str, settings: Settings) -> EvaluatorService:
    if _is_fake(model_spec):
        return FakeEvaluator()
    model = _model_for(model_spec, PipelineStep.VALIDATOR, settings)
    return LiveEvaluator(model, _structured_method(model_spec))


def _narrator(model_spec: str, settings: Settings) -> RiskNarratorService:
    if _is_fake(model_spec):
        return FakeRiskNarrator()
    return LiveRiskNarrator(_model_for(model_spec, PipelineStep.RISK, settings))


def build_services(settings: Settings) -> PipelineServices:
    """Primary right-sized service per step (SPEC 4.9)."""
    return PipelineServices(
        router=_router(settings.router_model, settings),
        extractor=_extractor(settings.extractor_model, settings),
        evaluator=_evaluator(settings.validator_model, settings),
        narrator=_narrator(settings.risk_model, settings),
    )


def build_fallback_services(settings: Settings) -> PipelineServices:
    """Fallback tier: every step backed by MODEL_FALLBACK (SPEC 4.7)."""
    spec = settings.fallback_model
    return PipelineServices(
        router=_router(spec, settings),
        extractor=_extractor(spec, settings),
        evaluator=_evaluator(spec, settings),
        narrator=_narrator(spec, settings),
    )
