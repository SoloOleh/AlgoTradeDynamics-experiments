"""Adapter exposing the DCA runtime as a strategy."""
from __future__ import annotations

from app.bot_engine.dca_runtime import (
    get_dca_risk_summary,
    get_effective_dca_settings,
    tick_dca_bot,
    validate_dca_configuration,
)
from app.bot_engine.grid_runtime import (
    cancel_all_bot_orders,
    close_bot_position,
    ensure_live_trading_allowed,
    get_position_snapshot,
    get_runtime_state,
    sync_bot_orders,
)
from app.bot_engine.exchange.capabilities import Capability
from app.bot_engine.strategies.base import COMMON_CAPABILITIES
from app.models.trading_bot import TradingBot


class DcaStrategy:
    strategy_type = "dca"

    def get_effective_settings(self, bot: TradingBot) -> dict:
        return get_effective_dca_settings(bot)

    def get_runtime_state(self, db, bot: TradingBot) -> tuple[str, str | None]:
        return get_runtime_state(db, bot)

    def required_capabilities(self, bot: TradingBot) -> frozenset[Capability]:
        return COMMON_CAPABILITIES | {Capability.MARKET_ORDERS}

    def validate_start(self, db, bot: TradingBot) -> None:
        message = ensure_live_trading_allowed(db, bot)
        if message:
            raise ValueError(message)
        validate_dca_configuration(bot)

    def tick(self, db, bot: TradingBot, *, force: bool = False, adapter=None) -> dict:
        return tick_dca_bot(db, bot, force=force, adapter=adapter)

    def sync_orders(self, db, bot: TradingBot, *, adapter=None) -> list:
        return [change["order"] for change in sync_bot_orders(db, bot, adapter=adapter)]

    def cancel_orders(self, db, bot: TradingBot, *, adapter=None) -> list:
        return cancel_all_bot_orders(db, bot, adapter=adapter)

    def get_position(self, db, bot: TradingBot, *, adapter=None) -> dict:
        return get_position_snapshot(db, bot, adapter=adapter)

    def get_risk(self, db, bot: TradingBot, *, adapter=None) -> dict:
        return get_dca_risk_summary(db, bot, adapter=adapter)

    def close_position(self, db, bot: TradingBot, *, adapter=None) -> dict:
        return close_bot_position(db, bot, adapter=adapter)
