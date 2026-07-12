"""Engineering observability: structured trajectory logs + LangSmith.

LangSmith tracing needs no code: LangGraph picks it up from the standard
environment variables (LANGSMITH_TRACING, LANGSMITH_API_KEY,
LANGSMITH_PROJECT). This module only configures structlog.
"""

from kyc_agent.observability.logging import configure_logging

__all__ = ["configure_logging"]
