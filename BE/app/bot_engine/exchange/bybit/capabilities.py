"""What the Bybit adapter declares per market type (sad.md §5, ADR-0006).

Mirrors what bots do on Bybit today, including semantically loose cases (position reads on spot).
"""
from app.bot_engine.exchange.capabilities import Capability, CapabilitySet, MarketType

_EVERY_MARKET = frozenset(Capability) - {Capability.SHORT_SELLING}

BYBIT_CAPABILITIES = CapabilitySet(
    {
        MarketType.SPOT: _EVERY_MARKET,
        MarketType.LINEAR: _EVERY_MARKET | {Capability.SHORT_SELLING},
        MarketType.INVERSE: _EVERY_MARKET | {Capability.SHORT_SELLING},
    },
    client_tag_max_length=36,
)
