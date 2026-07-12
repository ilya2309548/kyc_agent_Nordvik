"""structlog configuration: JSON trajectory logs for every agent step."""

import logging
import sys

import structlog


def configure_logging(level: int = logging.INFO, json_output: bool = True) -> None:
    logging.basicConfig(stream=sys.stdout, level=level, format="%(message)s")
    renderer = (
        structlog.processors.JSONRenderer() if json_output else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )
