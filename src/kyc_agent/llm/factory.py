"""Per-step service factory (SPEC 4.9, 9).

Model identifiers are ``provider:model`` strings resolved through
LangChain ``init_chat_model`` — no vendor is hardcoded. The special
provider ``fake`` yields the deterministic offline implementation.
"""

from functools import lru_cache

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
def _chat_model(model_spec: str, temperature: float) -> BaseChatModel:
    provider, _, model_name = model_spec.partition(":")
    if not model_name:
        raise ValueError(f"invalid model spec {model_spec!r}: expected 'provider:model_name'")
    return init_chat_model(model_name, model_provider=provider, temperature=temperature)


def _router(model_spec: str) -> RouterService:
    if _is_fake(model_spec):
        return FakeRouter()
    return LiveRouter(_chat_model(model_spec, TEMPERATURE_BY_STEP[PipelineStep.ROUTER]))


def _extractor(model_spec: str) -> ExtractorService:
    if _is_fake(model_spec):
        return FakeExtractor()
    return LiveExtractor(_chat_model(model_spec, TEMPERATURE_BY_STEP[PipelineStep.EXTRACTOR]))


def _evaluator(model_spec: str) -> EvaluatorService:
    if _is_fake(model_spec):
        return FakeEvaluator()
    return LiveEvaluator(_chat_model(model_spec, TEMPERATURE_BY_STEP[PipelineStep.VALIDATOR]))


def _narrator(model_spec: str) -> RiskNarratorService:
    if _is_fake(model_spec):
        return FakeRiskNarrator()
    return LiveRiskNarrator(_chat_model(model_spec, TEMPERATURE_BY_STEP[PipelineStep.RISK]))


def build_services(settings: Settings) -> PipelineServices:
    """Primary right-sized service per step (SPEC 4.9)."""
    return PipelineServices(
        router=_router(settings.router_model),
        extractor=_extractor(settings.extractor_model),
        evaluator=_evaluator(settings.validator_model),
        narrator=_narrator(settings.risk_model),
    )


def build_fallback_services(settings: Settings) -> PipelineServices:
    """Fallback tier: every step backed by MODEL_FALLBACK (SPEC 4.7)."""
    spec = settings.fallback_model
    return PipelineServices(
        router=_router(spec),
        extractor=_extractor(spec),
        evaluator=_evaluator(spec),
        narrator=_narrator(spec),
    )
