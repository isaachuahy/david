import os
from typing import Mapping

import sentry_sdk
from loguru import logger


def init_sentry() -> bool:
    """
    Initializes Sentry from environment variables when a DSN is configured.

    This gives the rest of the application one stable bootstrap entrypoint
    instead of repeating SDK setup details in multiple runtime modules.
    """
    sentry_dsn = os.getenv("SENTRY_DSN", "").strip()
    if not sentry_dsn:
        logger.info("Sentry is disabled because SENTRY_DSN is not configured.")
        return False

    sentry_sdk.init(
        dsn=sentry_dsn,
        environment=os.getenv("DAVID_ENVIRONMENT", "production"),
        release=os.getenv("DAVID_RELEASE"),
        send_default_pii=False,
        enable_tracing=False,
    )
    sentry_sdk.set_tag("service", "david")
    logger.info("Sentry is enabled for runtime error reporting.")
    return True


def capture_exception(
    error: Exception,
    *,
    component: str,
    operation: str,
    message: str | None = None,
    tags: Mapping[str, object] | None = None,
) -> None:
    """
    Logs and reports an exception with stable application-level metadata.

    `component` and `operation` are the primary classification keys, while
    `tags` carries any additional workflow-specific context such as session or
    trigger identifiers.
    """
    if message:
        logger.opt(exception=error).error(message)

    with sentry_sdk.new_scope() as scope:
        scope.set_tag("component", component)
        scope.set_tag("operation", operation)

        if tags:
            for key, value in tags.items():
                scope.set_tag(key, str(value))

        sentry_sdk.capture_exception(error)
