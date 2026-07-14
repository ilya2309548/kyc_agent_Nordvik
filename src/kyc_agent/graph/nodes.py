"""Graph nodes (SPEC 8.1).

Concurrency note: extraction workers run in the same superstep (Send
fan-out), so they may only write channels with additive reducers
(``extractions``, ``errors``, ``retry_counts``, ``degraded_reasons``).
The case counts as degraded whenever ``degraded_reasons`` is non-empty.
"""

from functools import partial
from typing import Any, Literal

import structlog
from langgraph.runtime import Runtime
from langgraph.types import Command, Send, interrupt
from pydantic import ValidationError

from kyc_agent.audit.sink import emit
from kyc_agent.graph.context import PipelineContext
from kyc_agent.graph.state import ExtractTask, KYCState
from kyc_agent.llm.base import PipelineStep
from kyc_agent.llm.resilience import StepExhaustedError, run_resilient
from kyc_agent.rules.ids import RuleId
from kyc_agent.rules.risk import RiskThresholds, decide_gate, evaluate_risk_rules, risk_level
from kyc_agent.rules.validation import (
    MatchThresholds,
    check_package_completeness,
    run_validation_rules,
)
from kyc_agent.schemas.case import CaseStatus, CustomerType
from kyc_agent.schemas.decisions import (
    DecidedBy,
    Decision,
    DecisionOutcome,
    ExtractionResult,
    HumanDecision,
    RegistryHit,
    RiskAssessment,
    RuleFlag,
    Severity,
    ValidationReport,
)
from kyc_agent.schemas.documents import DocumentType, InputDocument
from kyc_agent.tools.registries import MockRegistryClient

logger = structlog.get_logger(__name__)

_PERSON_DOC_TYPES = (DocumentType.ID_DOCUMENT, DocumentType.PROOF_OF_ADDRESS)


def _is_degraded(state: KYCState) -> bool:
    return bool(state.get("degraded_reasons"))


async def intake(state: KYCState, runtime: Runtime[PipelineContext]) -> dict[str, Any]:
    ctx = runtime.context
    package = state["package"]
    await emit(
        ctx.audit,
        state["case_id"],
        "intake",
        "node_completed",
        customer_type=package.customer_type,
        applicant=package.applicant.full_name,
        documents=[d.document_id for d in package.documents],
    )
    logger.info("case_received", case_id=state["case_id"], customer_type=package.customer_type)
    return {"status": CaseStatus.PROCESSING}


async def router(
    state: KYCState, runtime: Runtime[PipelineContext]
) -> Command[Literal["orchestrator", "handle_error"]]:
    ctx = runtime.context
    package = state["package"]

    try:
        classified, errors = await run_resilient(
            PipelineStep.ROUTER,
            "router",
            lambda: ctx.services.router.classify(package.documents, package.customer_type),
            lambda: ctx.fallback.router.classify(package.documents, package.customer_type),
            ctx.settings.max_step_retries,
        )
    except StepExhaustedError as exc:
        await emit(
            ctx.audit,
            state["case_id"],
            "router",
            "error",
            errors=[e.model_dump(mode="json") for e in exc.errors],
        )
        return Command(
            goto="handle_error",
            update={
                "errors": exc.errors,
                "retry_counts": {"router": len(exc.errors)},
                "degraded_reasons": ["router failed after retries and fallback"],
            },
        )

    await emit(
        ctx.audit,
        state["case_id"],
        "router",
        "node_completed",
        classifications=[c.model_dump(mode="json") for c in classified],
    )
    return Command(
        goto="orchestrator",
        update={
            "classified_documents": classified,
            "errors": errors,
            "retry_counts": {"router": len(errors) + 1},
        },
    )


async def orchestrator(
    state: KYCState, runtime: Runtime[PipelineContext]
) -> Command[Literal["extract_document", "decision_gate"]]:
    ctx = runtime.context
    package = state["package"]
    classified = state["classified_documents"]
    docs_by_id = {d.document_id: d for d in package.documents}

    known = [c for c in classified if c.doc_type is not DocumentType.UNKNOWN]
    unknown = [c for c in classified if c.doc_type is DocumentType.UNKNOWN]

    update: dict[str, Any] = {}
    if unknown:
        reasons = [f"unreadable or unclassifiable document: {c.document_id}" for c in unknown]
        update["degraded_reasons"] = reasons
        for reason in reasons:
            await emit(
                ctx.audit,
                state["case_id"],
                "orchestrator",
                "rule_triggered",
                rule_id=RuleId.UNREADABLE_DOCUMENT,
                details=reason,
            )

    completeness_flag = check_package_completeness(
        package.customer_type, {c.doc_type for c in known}
    )
    update["completeness_flag"] = completeness_flag

    if completeness_flag is not None:
        # With an unreadable document in the package the missing type may be
        # exactly that document, so the case degrades to a human instead of
        # being auto-rejected as incomplete (SPEC 7.4 + degraded path).
        await emit(
            ctx.audit,
            state["case_id"],
            "orchestrator",
            "rule_triggered",
            rule_id=completeness_flag.rule_id,
            details=completeness_flag.details,
            degraded=bool(unknown),
        )
        return Command(goto="decision_gate", update=update)

    await emit(
        ctx.audit,
        state["case_id"],
        "orchestrator",
        "node_completed",
        dispatched=[c.document_id for c in known],
    )
    sends = [
        Send(
            "extract_document",
            ExtractTask(
                case_id=state["case_id"],
                document=docs_by_id[c.document_id],
                doc_type=c.doc_type,
            ),
        )
        for c in known
    ]
    return Command(goto=sends, update=update)


async def extract_document(task: ExtractTask, runtime: Runtime[PipelineContext]) -> dict[str, Any]:
    ctx = runtime.context
    document: InputDocument = task["document"]
    doc_type: DocumentType = task["doc_type"]
    node = f"extract_document:{document.document_id}"

    try:
        result, errors = await run_resilient(
            PipelineStep.EXTRACTOR,
            node,
            lambda: ctx.services.extractor.extract(document, doc_type),
            lambda: ctx.fallback.extractor.extract(document, doc_type),
            ctx.settings.max_step_retries,
        )
    except StepExhaustedError as exc:
        result = ExtractionResult(
            document_id=document.document_id,
            doc_type=doc_type,
            extraction_error="extraction failed after retries and fallback",
        )
        await emit(
            ctx.audit,
            task["case_id"],
            node,
            "error",
            errors=[e.model_dump(mode="json") for e in exc.errors],
        )
        return {
            "extractions": [result],
            "errors": exc.errors,
            "retry_counts": {node: len(exc.errors)},
            "degraded_reasons": [f"extraction failed for document {document.document_id}"],
        }

    update: dict[str, Any] = {
        "extractions": [result],
        "errors": errors,
        "retry_counts": {node: len(errors) + 1},
    }
    if result.extraction_error is not None:
        update["degraded_reasons"] = [
            f"document {document.document_id} unreadable: {result.extraction_error}"
        ]
    await emit(
        ctx.audit,
        task["case_id"],
        node,
        "node_completed",
        doc_type=doc_type,
        fields=result.fields,
        field_confidence=result.field_confidence,
        extraction_error=result.extraction_error,
    )
    return update


def _overall_confidence(
    extractions: list[ExtractionResult],
    grounding_flags: list[RuleFlag],
    all_flags: list[RuleFlag],
) -> float:
    """Deterministic confidence formula (documented in SPEC 4.4).

    Base = worst per-field extractor confidence; grounding discrepancies
    cap it at 0.4, an unreadable document caps it at 0.5, and up to three
    warning flags shave 0.05 each.
    """
    values = [
        c for e in extractions if e.extraction_error is None for c in e.field_confidence.values()
    ]
    confidence = min(values, default=0.3)
    if grounding_flags:
        confidence = min(confidence, 0.4)
    if any(e.extraction_error is not None for e in extractions):
        confidence = min(confidence, 0.5)
    warnings = sum(1 for f in all_flags if f.severity is Severity.WARNING)
    confidence -= 0.05 * min(warnings, 3)
    return max(0.0, min(1.0, round(confidence, 4)))


async def validator(
    state: KYCState, runtime: Runtime[PipelineContext]
) -> Command[Literal["risk_scorer", "handle_error"]]:
    ctx = runtime.context
    package = state["package"]
    extractions = state.get("extractions", [])
    docs_by_id = {d.document_id: d for d in package.documents}

    thresholds = MatchThresholds(
        name_critical=ctx.settings.name_fuzzy_critical,
        name_warning=ctx.settings.name_fuzzy_warning,
        address=ctx.settings.address_fuzzy_threshold,
    )
    checks, rule_flags = run_validation_rules(package.applicant, extractions, ctx.today, thresholds)

    grounding_flags: list[RuleFlag] = []
    if ctx.evaluator_enabled:
        for extraction in extractions:
            if extraction.extraction_error is not None:
                continue
            document = docs_by_id[extraction.document_id]
            try:
                flags, errors = await run_resilient(
                    PipelineStep.VALIDATOR,
                    "validator",
                    partial(ctx.services.evaluator.verify_grounding, document, extraction),
                    partial(ctx.fallback.evaluator.verify_grounding, document, extraction),
                    ctx.settings.max_step_retries,
                )
            except StepExhaustedError as exc:
                await emit(
                    ctx.audit,
                    state["case_id"],
                    "validator",
                    "error",
                    errors=[e.model_dump(mode="json") for e in exc.errors],
                )
                return Command(
                    goto="handle_error",
                    update={
                        "errors": exc.errors,
                        "retry_counts": {"validator": len(exc.errors)},
                        "degraded_reasons": ["validator failed after retries and fallback"],
                    },
                )
            grounding_flags.extend(flags)
            if errors:
                # Transient failures that recovered: keep them for the audit.
                await emit(
                    ctx.audit,
                    state["case_id"],
                    "validator",
                    "error",
                    recovered=True,
                    errors=[e.model_dump(mode="json") for e in errors],
                )

    all_flags = rule_flags + grounding_flags
    confidence = _overall_confidence(extractions, grounding_flags, all_flags)
    report = ValidationReport(
        field_checks=checks,
        rule_flags=all_flags,
        overall_confidence=confidence,
        evaluator_notes=(
            f"deterministic flags: {len(rule_flags)}; grounding discrepancies: "
            f"{len(grounding_flags)}; evaluator_enabled={ctx.evaluator_enabled}"
        ),
    )

    for flag in all_flags:
        await emit(
            ctx.audit,
            state["case_id"],
            "validator",
            "rule_triggered",
            rule_id=flag.rule_id,
            severity=flag.severity,
            details=flag.details,
        )
    await emit(
        ctx.audit,
        state["case_id"],
        "validator",
        "node_completed",
        overall_confidence=confidence,
        field_checks=[c.model_dump(mode="json") for c in checks],
        flags=[f.model_dump(mode="json") for f in all_flags],
    )
    return Command(goto="risk_scorer", update={"validation": report})


async def _screen(
    ctx: PipelineContext,
    client: MockRegistryClient,
    names: list[str],
    case_id: str,
) -> tuple[list[RegistryHit], bool]:
    """Screen names against one registry; returns (hits, registry_available)."""
    hits: list[RegistryHit] = []
    for name in names:
        try:
            found, _ = await run_resilient(
                PipelineStep.RISK,
                f"risk:{client.registry}",
                partial(client.search, name),
                None,
                ctx.settings.max_step_retries,
            )
        except StepExhaustedError:
            await emit(
                ctx.audit,
                case_id,
                "risk_scorer",
                "error",
                registry=client.registry,
                details="registry unavailable after retries",
            )
            return hits, False
        await emit(
            ctx.audit,
            case_id,
            "risk_scorer",
            "registry_checked",
            registry=client.registry,
            query=name,
            hits=[h.model_dump(mode="json") for h in found],
        )
        hits.extend(found)
    return hits, True


def _screening_names(state: KYCState) -> tuple[list[str], list[str], list[str]]:
    """(person_names, company_names, ubo_names) to screen, deduplicated."""
    package = state["package"]
    persons: dict[str, None] = {}
    companies: dict[str, None] = {}
    owners: dict[str, None] = {}

    persons[package.applicant.full_name] = None
    if package.customer_type is CustomerType.BUSINESS and package.applicant.company_name:
        companies[package.applicant.company_name] = None

    for extraction in state.get("extractions", []):
        if extraction.extraction_error is not None:
            continue
        name = extraction.fields.get("full_name")
        if extraction.doc_type in _PERSON_DOC_TYPES and isinstance(name, str):
            persons[name] = None
        company = extraction.fields.get("company_name")
        if isinstance(company, str):
            companies[company] = None
        for owner in extraction.fields.get("beneficial_owners") or []:
            owner_name = owner.get("full_name") if isinstance(owner, dict) else None
            if owner_name:
                owners[owner_name] = None

    return list(persons), list(companies), list(owners)


async def risk_scorer(state: KYCState, runtime: Runtime[PipelineContext]) -> dict[str, Any]:
    ctx = runtime.context
    package = state["package"]
    case_id = state["case_id"]
    validation = state.get("validation")
    validation_flags = validation.rule_flags if validation else []
    confidence = validation.overall_confidence if validation else 0.0

    persons, companies, owners = _screening_names(state)

    registry_unavailable: list[str] = []
    sanctions_hits, sanctions_ok = await _screen(ctx, ctx.sanctions, persons + companies, case_id)
    pep_hits, pep_ok = await _screen(ctx, ctx.pep, persons, case_id)

    ubo_hits: list[RegistryHit] = []
    if owners:
        ubo_sanctions, ubo_sanctions_ok = await _screen(ctx, ctx.sanctions, owners, case_id)
        ubo_pep, ubo_pep_ok = await _screen(ctx, ctx.pep, owners, case_id)
        ubo_hits = ubo_sanctions + ubo_pep
        sanctions_ok = sanctions_ok and ubo_sanctions_ok
        pep_ok = pep_ok and ubo_pep_ok

    if not sanctions_ok:
        registry_unavailable.append("sanctions")
    if not pep_ok:
        registry_unavailable.append("pep")

    triggered = evaluate_risk_rules(
        customer_type=package.customer_type,
        expected_monthly_volume_eur=package.applicant.expected_monthly_volume_eur,
        overall_confidence=confidence,
        validation_flags=validation_flags,
        sanctions_hits=sanctions_hits,
        pep_hits=pep_hits,
        ubo_hits=ubo_hits,
        registry_unavailable=registry_unavailable,
        thresholds=RiskThresholds(
            high_volume_individual_eur=ctx.settings.high_volume_threshold_individual_eur,
            high_volume_business_eur=ctx.settings.high_volume_threshold_business_eur,
            confidence=ctx.settings.confidence_threshold,
        ),
    )
    level = risk_level(triggered)

    try:
        rationale, _ = await run_resilient(
            PipelineStep.RISK,
            "risk_scorer",
            lambda: ctx.services.narrator.narrate(package.applicant, triggered, validation_flags),
            lambda: ctx.fallback.narrator.narrate(package.applicant, triggered, validation_flags),
            ctx.settings.max_step_retries,
        )
    except StepExhaustedError:
        # Narration is presentation, not judgement: fall back to a plain
        # deterministic summary instead of degrading the whole case.
        rationale = "; ".join(f"{f.rule_id}: {f.details}" for f in triggered) or (
            "no risk triggers fired"
        )

    for flag in triggered:
        await emit(
            ctx.audit,
            case_id,
            "risk_scorer",
            "rule_triggered",
            rule_id=flag.rule_id,
            severity=flag.severity,
            details=flag.details,
        )
    await emit(
        ctx.audit,
        case_id,
        "risk_scorer",
        "node_completed",
        level=level,
        triggered=[f.rule_id for f in triggered],
        sanctions_hits=len(sanctions_hits),
        pep_hits=len(pep_hits),
        ubo_hits=len(ubo_hits),
    )
    return {
        "risk": RiskAssessment(
            level=level,
            triggered_rules=triggered,
            sanctions_hits=sanctions_hits,
            pep_hits=pep_hits + ubo_hits,
            rationale=rationale,
        )
    }


async def decision_gate(
    state: KYCState, runtime: Runtime[PipelineContext]
) -> Command[Literal["auto_decision", "human_review"]]:
    ctx = runtime.context
    validation = state.get("validation")
    risk = state.get("risk")
    decision = decide_gate(
        completeness_flag=state.get("completeness_flag"),
        validation_flags=validation.rule_flags if validation else [],
        escalation_triggers=risk.triggered_rules if risk else [],
        degraded=_is_degraded(state),
    )
    await emit(
        ctx.audit,
        state["case_id"],
        "decision_gate",
        "node_completed",
        outcome=decision.outcome,
        reason_codes=decision.reason_codes,
    )
    if decision.outcome is DecisionOutcome.ESCALATE:
        return Command(
            goto="human_review",
            update={"decision": decision, "status": CaseStatus.AWAITING_HUMAN_REVIEW},
        )
    return Command(goto="auto_decision", update={"decision": decision})


async def auto_decision(state: KYCState, runtime: Runtime[PipelineContext]) -> dict[str, Any]:
    ctx = runtime.context
    decision = state["decision"]
    assert decision is not None
    await emit(
        ctx.audit,
        state["case_id"],
        "auto_decision",
        "decision_made",
        outcome=decision.outcome,
        decided_by=decision.decided_by,
        reason_codes=decision.reason_codes,
        rationale=decision.rationale,
    )
    return {}


def _review_payload(state: KYCState, error: str | None = None) -> dict[str, Any]:
    decision = state.get("decision")
    validation = state.get("validation")
    risk = state.get("risk")
    reason_codes = decision.reason_codes if decision else []
    recommendation = (
        "reject"
        if {RuleId.SANCTIONS_HIT, RuleId.PEP_MATCH, RuleId.UBO_SANCTIONS_OR_PEP} & set(reason_codes)
        else "manual_verification"
    )
    payload: dict[str, Any] = {
        "case_id": state["case_id"],
        "reason_codes": reason_codes,
        "summary": {
            "extracted": {e.document_id: e.fields for e in state.get("extractions", [])},
            "flags": [
                f.model_dump(mode="json") for f in (validation.rule_flags if validation else [])
            ],
            "risk": risk.model_dump(mode="json") if risk else None,
        },
        "system_recommendation": recommendation,
        "degraded": _is_degraded(state),
        "degraded_reasons": state.get("degraded_reasons", []),
    }
    if error is not None:
        payload["resume_error"] = error
    return payload


async def human_review(state: KYCState, runtime: Runtime[PipelineContext]) -> dict[str, Any]:
    ctx = runtime.context
    error: str | None = None

    while True:
        resume_value = interrupt(_review_payload(state, error))
        try:
            human = HumanDecision.model_validate(resume_value)
            break
        except ValidationError as exc:
            # Invalid resume payload: re-interrupt, the case must not be
            # closed by a malformed review (SPEC 8.3).
            error = f"invalid review payload: {exc.error_count()} validation error(s)"

    system_decision = state.get("decision")
    decision = Decision(
        outcome=DecisionOutcome(human.outcome),
        decided_by=DecidedBy.HUMAN,
        reason_codes=system_decision.reason_codes if system_decision else [],
        rationale=human.comment or f"human review by {human.reviewer}",
        reviewer=human.reviewer,
    )
    await emit(
        ctx.audit,
        state["case_id"],
        "human_review",
        "human_decision",
        outcome=decision.outcome,
        reviewer=human.reviewer,
        comment=human.comment,
        reason_codes=decision.reason_codes,
    )
    return {"decision": decision}


async def finalize(state: KYCState, runtime: Runtime[PipelineContext]) -> dict[str, Any]:
    ctx = runtime.context
    decision = state["decision"]
    assert decision is not None
    await emit(
        ctx.audit,
        state["case_id"],
        "finalize",
        "case_completed",
        outcome=decision.outcome,
        decided_by=decision.decided_by,
        reason_codes=decision.reason_codes,
        reviewer=decision.reviewer,
        degraded=_is_degraded(state),
    )
    logger.info(
        "case_completed",
        case_id=state["case_id"],
        outcome=decision.outcome,
        decided_by=decision.decided_by,
    )
    return {"status": CaseStatus.COMPLETED}


async def handle_error(state: KYCState, runtime: Runtime[PipelineContext]) -> dict[str, Any]:
    """Fallback path (SPEC 4.7): a persistent step failure degrades the
    case to manual review through the decision gate — never a silent drop."""
    ctx = runtime.context
    await emit(
        ctx.audit,
        state["case_id"],
        "handle_error",
        "error",
        degraded_reasons=state.get("degraded_reasons", []),
        errors=[e.model_dump(mode="json") for e in state.get("errors", [])[-5:]],
    )
    return {"status": CaseStatus.PROCESSING}
