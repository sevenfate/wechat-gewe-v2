from __future__ import annotations

import logging
import sys
from typing import Any, cast

import structlog


def configure_logging(level: str) -> None:
    logging.basicConfig(
        format="%(message)s",
        level=getattr(logging, level, logging.INFO),
        stream=sys.stdout,
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level, logging.INFO)),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(**context: Any) -> structlog.stdlib.BoundLogger:
    logger = structlog.get_logger().bind(**context)
    return cast(structlog.stdlib.BoundLogger, logger)
