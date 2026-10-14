"""Operational logging and incident lifecycle helpers."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from app.bot_engine.exchange.redaction import scrub
from app.core.clock import utcnow
from app.core.config import get_settings
from app.models.operations import Incident, OperationLog
from app.models.trading_bot import TradingBot


def record_operation_log(
    db: Session,
    *,
    level: str,
    service: str,
    message: str,
    request_id: str | None = None,
    correlation_id: str | None = None,
    user_id: int | None = None,
    bot_id: int | None = None,
    method: str | None = None,
    path: str | None = None,
    status_code: int | None = None,
    duration_ms: float | None = None,
    exchange: str | None = None,
    error_type: str | None = None,
) -> OperationLog:
    settings = get_settings()
    now = utcnow()
    item = OperationLog(
        occurred_at=now,
        level=level.upper()[:20],
        service=service.upper()[:40],
        request_id=request_id,
        correlation_id=correlation_id,
        user_id=user_id,
        bot_id=bot_id,
        method=method,
        path=path,
        status_code=status_code,
        duration_ms=duration_ms,
        exchange=exchange,
        error_type=error_type,
        message=scrub(message)[:1000],
        retention_until=now + timedelta(days=max(settings.operations_log_retention_days, 1)),
    )
    db.add(item)
    db.flush()
    return item


def create_bot_incident(
    db: Session,
    *,
    bot: TradingBot,
    severity: str,
    incident_type: str,
    description: str,
    action_taken: str,
    source_event_id: int | None = None,
) -> Incident | None:
    """Create one open incident for an ERROR/CRITICAL bot failure.

    Repeated retries of the same type update the current incident instead of
    flooding the register with duplicates.
    """
    severity = severity.upper()
    if severity not in {"ERROR", "CRITICAL"}:
        return None
    now = utcnow()
    existing = (
        db.query(Incident)
        .filter(
            Incident.user_id == bot.user_id,
            Incident.bot_id == bot.id,
            Incident.incident_type == incident_type.upper(),
            Incident.status == "OPEN",
        )
        .order_by(Incident.id.desc())
        .first()
    )
    if existing is not None:
        existing.updated_at = now
        existing.severity = severity
        existing.description = scrub(description)[:5000]
        existing.action_taken = action_taken[:255]
        existing.source_event_id = source_event_id or existing.source_event_id
        db.add(existing)
        return existing

    settings = get_settings()
    item = Incident(
        opened_at=now,
        updated_at=now,
        retention_until=now + timedelta(days=max(settings.incident_retention_days, 1)),
        user_id=bot.user_id,
        bot_id=bot.id,
        source_event_id=source_event_id,
        correlation_id=f"bot:{bot.id}",
        severity=severity,
        incident_type=incident_type.upper()[:80],
        status="OPEN",
        title=f"{bot.name}: {incident_type.replace('_', ' ').title()}"[:255],
        description=scrub(description)[:5000],
        action_taken=action_taken[:255],
    )
    db.add(item)
    db.flush()
    return item


def resolve_bot_incidents(db: Session, bot: TradingBot, *, resolution: str) -> int:
    now = utcnow()
    items = db.query(Incident).filter(
        Incident.user_id == bot.user_id,
        Incident.bot_id == bot.id,
        Incident.status == "OPEN",
    ).all()
    for item in items:
        item.status = "RESOLVED"
        item.updated_at = now
        item.resolved_at = now
        item.resolution = resolution[:5000]
        db.add(item)
    return len(items)
