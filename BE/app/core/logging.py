"""Structured logging configuration."""
import logging
import sys

from app.core.config import get_settings

# uvicorn's error loggers carry exception tracebacks. Its access logger is left alone: its formatter
# unpacks `record.args` (client, method, path, version, status), which the filter would flatten.
UVICORN_LOGGERS = ("uvicorn", "uvicorn.error")


class ScrubbingFilter(logging.Filter):
    """Pass every log record through the shared scrubber (ADR-0008): keys `****abcd`, secrets never."""

    def filter(self, record: logging.LogRecord) -> bool:
        from app.bot_engine.exchange.redaction import scrub

        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a format/argument mismatch must not reach the handler raw
            # Left as is, Handler.handleError would print the raw msg and args to stderr.
            try:
                message = f"{record.msg} (unformattable log arguments: {record.args!r})"
            except Exception:  # noqa: BLE001 - msg or an argument cannot be turned into text at all
                # Handler.handleError does not cover filter(), so this text must not raise either.
                message = f"unformattable log record at {record.pathname}:{record.lineno}"
        record.msg = scrub(message)
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

    # uvicorn's loggers keep their own handlers and do not propagate to the root one.
    for name in UVICORN_LOGGERS:
        for uvicorn_handler in logging.getLogger(name).handlers:
            if not any(isinstance(item, ScrubbingFilter) for item in uvicorn_handler.filters):
                uvicorn_handler.addFilter(ScrubbingFilter())

    # Tame noisy libs.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    # Telegram Bot API credentials live in the request URL; do not let HTTP client
    # INFO logs print outgoing URLs containing the secret bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
