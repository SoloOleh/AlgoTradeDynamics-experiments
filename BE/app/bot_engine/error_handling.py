"""Classify bot runtime failures and apply safe recovery policies.

The strategy layer raises the original exception. This module translates it into
an operational error type and decides whether a live/demo bot should retry,
resynchronise, pause for user action, or stop.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta
from enum import Enum
from typing import Any

try:
    from enum import StrEnum
except ImportError:  # pragma: no cover - Python < 3.11 fallback
    class StrEnum(str, Enum):
        pass

from app.bot_engine.events import log_bot_event
from app.bot_engine.exchange.bybit.errors import classify_bybit_failure, extract_code
from app.bot_engine.exchange.errors import CapabilityMissing, ErrorClass, GuardRefusal
from app.bot_engine.exchange.redaction import scrub
from app.core.clock import utcnow
from app.models.trading_bot import TradingBot


class BotErrorType(StrEnum):
    NETWORK = "network"
    RATE_LIMIT = "rate_limit"
    EXCHANGE_UNAVAILABLE = "exchange_unavailable"
    TIME_SYNC = "time_sync"
    AUTHENTICATION = "authentication"
    PERMISSION = "permission"
    INSUFFICIENT_FUNDS = "insufficient_funds"
    INVALID_ORDER = "invalid_order"
    INVALID_SYMBOL = "invalid_symbol"
    ORDER_STATE = "order_state"
    CONFIGURATION = "configuration"
    RISK = "risk"
    INTERNAL = "internal"


class BotErrorSeverity(StrEnum):
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class BotErrorAction(StrEnum):
    RETRY_BACKOFF = "retry_backoff"
    RESYNC_AND_RETRY = "resync_and_retry"
    PAUSE = "pause"
    STOP = "stop"


@dataclass(slots=True, frozen=True)
class BotErrorDecision:
    error_type: BotErrorType
    severity: BotErrorSeverity
    action: BotErrorAction
    retryable: bool
    message: str
    code: str | None = None
    retry_after_seconds: int | None = None


class ExchangeOperationError(RuntimeError):
    """Structured non-zero exchange response for deterministic classification."""

    def __init__(self, message: str, *, code: str | int | None = None, operation: str | None = None):
        self.code = None if code is None else str(code)
        self.operation = operation
        prefix = f"{operation}: " if operation else ""
        suffix = f" (code {self.code})" if self.code is not None else ""
        super().__init__(f"{prefix}{message}{suffix}")


@dataclass(slots=True, frozen=True)
class ClassReaction:
    error_type: BotErrorType
    severity: BotErrorSeverity
    action: BotErrorAction
    retry_after_seconds: int | None = None


# Error class → reaction (ADR-0005). The values are the ones the bot took before the exchange
# adapter existed; money/config/auth problems never retry forever.
REACTION_TABLE: dict[ErrorClass, ClassReaction] = {
    ErrorClass.RATE_LIMITED: ClassReaction(BotErrorType.RATE_LIMIT, BotErrorSeverity.WARNING, BotErrorAction.RETRY_BACKOFF, 15),
    ErrorClass.EXCHANGE_UNAVAILABLE: ClassReaction(BotErrorType.EXCHANGE_UNAVAILABLE, BotErrorSeverity.ERROR, BotErrorAction.RETRY_BACKOFF, 15),
    ErrorClass.TIME_SYNC: ClassReaction(BotErrorType.TIME_SYNC, BotErrorSeverity.WARNING, BotErrorAction.RETRY_BACKOFF, 10),
    ErrorClass.REJECTED_CREDENTIALS: ClassReaction(BotErrorType.AUTHENTICATION, BotErrorSeverity.CRITICAL, BotErrorAction.STOP),
    ErrorClass.PERMISSION_DENIED: ClassReaction(BotErrorType.PERMISSION, BotErrorSeverity.CRITICAL, BotErrorAction.STOP),
    ErrorClass.INSUFFICIENT_BALANCE: ClassReaction(BotErrorType.INSUFFICIENT_FUNDS, BotErrorSeverity.ERROR, BotErrorAction.PAUSE),
    ErrorClass.INVALID_SYMBOL: ClassReaction(BotErrorType.INVALID_SYMBOL, BotErrorSeverity.ERROR, BotErrorAction.PAUSE),
    ErrorClass.DUPLICATE_CLIENT_TAG: ClassReaction(BotErrorType.ORDER_STATE, BotErrorSeverity.WARNING, BotErrorAction.RESYNC_AND_RETRY, 5),
    ErrorClass.UNKNOWN_ORDER: ClassReaction(BotErrorType.ORDER_STATE, BotErrorSeverity.WARNING, BotErrorAction.RESYNC_AND_RETRY, 5),
    ErrorClass.ORDER_STATE_CONFLICT: ClassReaction(BotErrorType.ORDER_STATE, BotErrorSeverity.WARNING, BotErrorAction.RESYNC_AND_RETRY, 5),
    ErrorClass.RISK_LIMIT: ClassReaction(BotErrorType.RISK, BotErrorSeverity.CRITICAL, BotErrorAction.PAUSE),
    ErrorClass.INVALID_ORDER: ClassReaction(BotErrorType.INVALID_ORDER, BotErrorSeverity.ERROR, BotErrorAction.PAUSE),
    ErrorClass.NETWORK: ClassReaction(BotErrorType.NETWORK, BotErrorSeverity.WARNING, BotErrorAction.RETRY_BACKOFF, 5),
}


def _decision(
    error_type: BotErrorType,
    severity: BotErrorSeverity,
    action: BotErrorAction,
    message: str,
    *,
    code: str | None,
    retry_after_seconds: int | None = None,
) -> BotErrorDecision:
    return BotErrorDecision(
        error_type=error_type,
        severity=severity,
        action=action,
        retryable=action in {BotErrorAction.RETRY_BACKOFF, BotErrorAction.RESYNC_AND_RETRY},
        message=message,
        code=code,
        retry_after_seconds=retry_after_seconds,
    )


def classify_bot_error(exc: BaseException) -> BotErrorDecision:
    """Map an arbitrary runtime exception to a safe operational policy.

    Exchange failures arrive as `ExchangeError` (or, from call sites not yet on the adapter, as a
    raw Bybit/emulator exception the Bybit adapter classifies); their error class picks the
    reaction from `REACTION_TABLE`. Other failures keep the engine's own classification.
    """
    exchange_error = classify_bybit_failure(exc)
    if exchange_error is not None:
        reaction = REACTION_TABLE[exchange_error.error_class]
        message = (exchange_error.message or "").strip() or type(exc).__name__
        return _decision(
            reaction.error_type, reaction.severity, reaction.action, message,
            code=exchange_error.raw_code, retry_after_seconds=reaction.retry_after_seconds,
        )

    message = str(exc).strip() or type(exc).__name__
    lowered = message.lower()
    code = extract_code(exc)
    if isinstance(exc, ValueError) and any(token in lowered for token in ("configuration", "supports linear", "must be", "cannot be", "inactive")):
        return _decision(BotErrorType.CONFIGURATION, BotErrorSeverity.ERROR, BotErrorAction.PAUSE, message, code=code)
    return _decision(BotErrorType.INTERNAL, BotErrorSeverity.CRITICAL, BotErrorAction.STOP, message, code=code)


def _error_setting(bot: TradingBot, key: str, default: Any) -> Any:
    settings = bot.settings or {}
    return settings.get(key, default)


def _retry_delay(bot: TradingBot, decision: BotErrorDecision, retry_count: int) -> int:
    base = int(decision.retry_after_seconds or _error_setting(bot, "error_retry_base_seconds", 5))
    maximum = int(_error_setting(bot, "error_retry_max_seconds", 300))
    # retry_count is 1-based here; first retry uses the base delay.
    return max(1, min(maximum, base * (2 ** max(0, retry_count - 1))))


def handle_bot_runtime_error(db, bot: TradingBot, exc: BaseException, *, source: str = "worker") -> BotErrorDecision:
    """Persist the error and apply its recovery action to a live/demo bot."""
    decision = classify_bot_error(exc)
    decision = replace(decision, message=scrub(decision.message))
    now = utcnow()
    retry_count = int(bot.error_retry_count or 0) + 1 if decision.retryable else 0
    max_retries = max(0, int(_error_setting(bot, "error_max_retries", 5)))

    action = decision.action
    severity = decision.severity
    next_retry_at = None
    if decision.retryable:
        if retry_count > max_retries:
            action = BotErrorAction.PAUSE
            severity = BotErrorSeverity.ERROR
        else:
            next_retry_at = now + timedelta(seconds=_retry_delay(bot, decision, retry_count))

    bot.last_run_at = now
    bot.last_error = decision.message[:500]
    bot.last_error_type = decision.error_type.value
    bot.last_error_severity = severity.value
    bot.last_error_action = action.value
    bot.last_error_code = decision.code
    bot.last_error_at = now
    bot.error_retry_count = retry_count
    bot.next_retry_at = next_retry_at

    if action in {BotErrorAction.RETRY_BACKOFF, BotErrorAction.RESYNC_AND_RETRY}:
        bot.runtime_status = "retrying"
    elif action == BotErrorAction.PAUSE:
        bot.runtime_status = "paused"
    else:
        bot.runtime_status = "error"
        bot.stopped_at = now

    payload = {
        "error_type": decision.error_type.value,
        "severity": severity.value,
        "action": action.value,
        "retryable": action in {BotErrorAction.RETRY_BACKOFF, BotErrorAction.RESYNC_AND_RETRY},
        "error_code": decision.code,
        "retry_count": retry_count,
        "max_retries": max_retries,
        "next_retry_at": next_retry_at.isoformat() if next_retry_at else None,
        "source": source,
        "exception_class": type(exc).__name__,
    }
    event = log_bot_event(db, bot, "bot_error", decision.message[:500], payload)
    from app.services.operations_service import create_bot_incident, record_operation_log
    record_operation_log(
        db,
        level="ERROR" if severity.value in {"error", "critical"} else "WARN",
        service="BOT_WORKER",
        message=decision.message,
        correlation_id=f"bot:{bot.id}",
        user_id=bot.user_id,
        bot_id=bot.id,
        exchange=bot.exchange,
        error_type=decision.error_type.value,
    )
    create_bot_incident(
        db,
        bot=bot,
        severity=severity.value,
        incident_type=decision.error_type.value,
        description=decision.message,
        action_taken=action.value,
        source_event_id=event.id,
    )
    db.add(bot)
    db.commit()
    return BotErrorDecision(
        error_type=decision.error_type,
        severity=severity,
        action=action,
        retryable=payload["retryable"],
        message=decision.message,
        code=decision.code,
        retry_after_seconds=(int((next_retry_at - now).total_seconds()) if next_retry_at else None),
    )


def _guard_reason(refusal: GuardRefusal) -> str:
    return "capability_missing" if isinstance(refusal, CapabilityMissing) else "venue_unsupported"


def pause_bot_for_guard(db, bot: TradingBot, refusal: GuardRefusal, *, source: str = "worker") -> None:
    """Pause a bot the venue guard refused (ADR-0004). Never retried, never classified.

    No request reaches any exchange and the bot's orders are left as they are: the Operator
    decides what to do with them (AC-10, AC-15b). Event shape: `bot.guard_paused.v1`.
    """
    reason = _guard_reason(refusal)
    text = str(refusal)
    message = scrub(f"Bot paused: {text[:1].lower()}{text[1:]}")[:500]
    now = utcnow()

    bot.runtime_status = "paused"
    bot.last_run_at = now
    bot.last_error = message
    bot.last_error_type = BotErrorType.CONFIGURATION.value
    bot.last_error_severity = BotErrorSeverity.ERROR.value
    bot.last_error_action = BotErrorAction.PAUSE.value
    bot.last_error_code = None
    bot.last_error_at = now
    bot.error_retry_count = 0
    bot.next_retry_at = None

    event = log_bot_event(db, bot, reason, message, {"reason": reason, **refusal.details})
    from app.services.operations_service import create_bot_incident, record_operation_log
    record_operation_log(
        db,
        level="ERROR",
        service="BOT_WORKER",
        message=message,
        correlation_id=f"bot:{bot.id}",
        user_id=bot.user_id,
        bot_id=bot.id,
        exchange=bot.exchange,
        error_type=BotErrorType.CONFIGURATION.value,
    )
    create_bot_incident(
        db,
        bot=bot,
        severity=BotErrorSeverity.ERROR.value,
        incident_type=reason,
        description=message,
        action_taken=BotErrorAction.PAUSE.value,
        source_event_id=event.id,
    )
    db.add(bot)
    db.commit()


def clear_bot_runtime_error(bot: TradingBot) -> None:
    """Clear current recovery state after a successful worker tick."""
    bot.last_error = None
    bot.last_error_type = None
    bot.last_error_severity = None
    bot.last_error_action = None
    bot.last_error_code = None
    bot.last_error_at = None
    bot.error_retry_count = 0
    bot.next_retry_at = None
