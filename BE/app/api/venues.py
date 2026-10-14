"""Connection check per venue (sad.md §5, §6 Flow 8; ADR-0004, ADR-0008)."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, ConfigDict

from app.api.deps import get_current_user
from app.bot_engine.exchange import registry
from app.bot_engine.exchange.errors import VenueNotSupported
from app.models.user import User

router = APIRouter(prefix="/venues", tags=["Venues"])


class VenueKey(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exchange: str
    environment: str


class VenueConnection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    venue: VenueKey
    status: Literal["connected", "not_configured", "failing", "not_supported"]
    error_class: str | None = None
    message: str


def _message(name: str, status: str, error_class: str | None) -> str:
    if status == "connected":
        return f"Venue {name} is connected"
    if status == "not_configured":
        return f"Venue {name} has no exchange keys configured"
    if status == "failing":
        return f"Venue {name} is failing: {error_class}"
    return f"Venue {name} has no exchange adapter"


@router.get("/{exchange}/{environment}/connection", response_model=VenueConnection)
def get_venue_connection(
    exchange: str = Path(min_length=1, max_length=40),
    environment: str = Path(min_length=1, max_length=20),
    _: User = Depends(get_current_user),
) -> VenueConnection:
    """Check a venue with the keys configured on the server. Nothing is persisted.

    The answer names the venue and carries a fixed text per status: never the raw exchange
    message, a key or a secret. There is no default venue.
    """
    venue = VenueKey(exchange=exchange, environment=environment)
    name = f"{exchange}/{environment}"
    try:
        adapter = registry.adapter_for_venue(exchange, environment)
    except VenueNotSupported:
        return VenueConnection(venue=venue, status="not_supported", message=_message(name, "not_supported", None))
    result = adapter.check_connection()
    error_class = result.error_class if result.state == "failing" else None
    return VenueConnection(
        venue=venue, status=result.state, error_class=error_class, message=_message(name, result.state, error_class),
    )
