"""Graph assembly (SPEC 8.2).

Topology::

    START -> intake -> router -> orchestrator ==Send×N==> extract_document
    orchestrator --(incomplete package)--> decision_gate
    extract_document -> validator -> risk_scorer -> decision_gate
    decision_gate -> auto_decision | human_review (interrupt) -> finalize -> END
    router/validator --(exhausted)--> handle_error -> decision_gate

Command-returning nodes (router, orchestrator, validator, decision_gate)
declare their targets in type annotations; the remaining edges are static.
"""

from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from kyc_agent.graph import nodes
from kyc_agent.graph.context import PipelineContext
from kyc_agent.graph.state import KYCState


def build_graph(
    checkpointer: BaseCheckpointSaver[Any] | None = None,
) -> CompiledStateGraph[KYCState, PipelineContext]:
    graph: StateGraph[KYCState, PipelineContext] = StateGraph(
        KYCState, context_schema=PipelineContext
    )

    graph.add_node("intake", nodes.intake)
    graph.add_node("router", nodes.router)
    graph.add_node("orchestrator", nodes.orchestrator)
    graph.add_node("extract_document", nodes.extract_document)
    graph.add_node("validator", nodes.validator)
    graph.add_node("risk_scorer", nodes.risk_scorer)
    graph.add_node("decision_gate", nodes.decision_gate)
    graph.add_node("auto_decision", nodes.auto_decision)
    graph.add_node("human_review", nodes.human_review)
    graph.add_node("finalize", nodes.finalize)
    graph.add_node("handle_error", nodes.handle_error)

    graph.add_edge(START, "intake")
    graph.add_edge("intake", "router")
    graph.add_edge("extract_document", "validator")
    graph.add_edge("risk_scorer", "decision_gate")
    graph.add_edge("handle_error", "decision_gate")
    graph.add_edge("auto_decision", "finalize")
    graph.add_edge("human_review", "finalize")
    graph.add_edge("finalize", END)

    return graph.compile(checkpointer=checkpointer)
