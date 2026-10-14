"""Structured logging configuration."""
import logging
import sys

from app.core.config import get_settings


class ScrubbingFilter(logging.Filter):
    """Pass every log record through the shared scrubber (ADR-0008): keys `****abcd`, secrets never."""

    def filter(self, record: logging.LogRecord) -> bool:
        from app.bot_engine.exchange.redaction import scrub

        record.msg = scrub(record.getMessage())
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = scrub(record.exc_text)
        if record.stack_info:
            record.stack_info = scrub(record.stack_info)
        return True


def configure_logging() -> None:
    """Initialize root logger with a consistent formatter."""
    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    handler.addFilter(ScrubbingFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # Tame noisy libs.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    # Telegram Bot API credentials live in the request URL; do not let HTTP client
    # INFO logs print outgoing URLs containing the secret bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
