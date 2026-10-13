"""Static search for exchange access outside the exchange adapters (QG-3, ADR-0009).

Scope: the bot engine, every backtest service, the connection check and the UI live stream, with
no exemptions. Outside `BE/app/bot_engine/exchange/` it flags pybit / EmulatorHTTP imports, the
Bybit session factory, and `/v5/` exchange paths or exchange WS endpoints. In the bot engine, the
connection check and the UI stream it also flags Bybit field names; backtests that parse the
emulator admin plane are exempt from that rule only.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
ADAPTER_PACKAGE = "BE/app/bot_engine/exchange/"

SCOPE_GLOBS = (
    "BE/app/bot_engine/**/*.py",
    "BE/app/services/backtest_*.py",
    "BE/app/services/fast_*_backtest.py",
    "BE/app/api/bybit.py",
    "BE/app/api/venues.py",
    "BE/app/api/websockets.py",
)
FIELD_RULE_PREFIXES = (
    "BE/app/bot_engine/",
    "BE/app/api/bybit.py",
    "BE/app/api/venues.py",
    "BE/app/api/websockets.py",
)

RULES = (
    ("pybit-import", re.compile(r"^\s*(from\s+pybit\b|import\s+pybit\b)")),
    ("emulator-client", re.compile(r"\bEmulatorHTTP\b")),
    ("session-factory", re.compile(r"\bget_bybit_(session|credentials)\b")),
    ("v5-path", re.compile(r"""["'][^"']*/v5/[^"']*["']""")),
    ("exchange-ws", re.compile(r"stream(-testnet|-demo)?\.bybit\.com")),
)
FIELD_RULE = ("bybit-field", re.compile(r"\b(lotSizeFilter|priceFilter|retCode|closedPnl|tradeMode)\b"))


@dataclass(frozen=True)
class Hit:
    path: str
    line: int
    rule: str
    text: str


def _files(root: Path) -> list[Path]:
    found: set[Path] = set()
    for pattern in SCOPE_GLOBS:
        found.update(path for path in root.glob(pattern) if path.is_file())
    return sorted(found)


def scan(root: Path | None = None) -> list[Hit]:
    root = Path(root or REPO_ROOT)
    hits: list[Hit] = []
    for path in _files(root):
        relative = path.relative_to(root).as_posix()
        if relative.startswith(ADAPTER_PACKAGE):
            continue
        rules = list(RULES)
        if relative.startswith(FIELD_RULE_PREFIXES):
            rules.append(FIELD_RULE)
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            for rule, pattern in rules:
                if pattern.search(line):
                    hits.append(Hit(relative, number, rule, line.strip()))
    return hits


if __name__ == "__main__":
    results = scan()
    for item in results:
        print(f"{item.path}:{item.line}: [{item.rule}] {item.text}")
    print(f"{len(results)} hits")
