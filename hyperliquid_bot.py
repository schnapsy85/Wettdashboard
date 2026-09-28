#!/usr/bin/env python3
"""Hyperliquid testnet bot scaffold: read-only market data plus guarded execution.
Execution stays disabled until TESTNET_TRADING_ENABLED=true and credentials exist.
"""
import json
import os
import time
import urllib.request
from dataclasses import dataclass

TESTNET = "https://api.hyperliquid-testnet.xyz"
UNIVERSE = ("BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "AVAX", "LINK", "SUI")

@dataclass(frozen=True)
class Risk:
    max_leverage: float = 2.0
    risk_per_trade: float = 0.0035
    max_open_risk: float = 0.007
    daily_loss: float = 0.01
    weekly_loss: float = 0.03
    max_drawdown: float = 0.05
    max_positions: int = 3

RISK = Risk()


def config():
    return {
        "execution": os.getenv("TESTNET_TRADING_ENABLED", "false").lower() == "true",
        "credentials": bool(os.getenv("HYPERLIQUID_TESTNET_PRIVATE_KEY")),
        "order_adapter": False,
        "environment": "testnet",
        "universe": list(UNIVERSE),
        "risk": RISK.__dict__,
    }


def info(payload):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(TESTNET + "/info", data=body,
                                 headers={"Content-Type": "application/json", "User-Agent": "HermesTestnetBot/1.0"}, method="POST")
    with urllib.request.urlopen(req, timeout=8) as response:
        return json.loads(response.read())


def snapshot():
    try:
        meta = info({"type": "metaAndAssetCtxs"})
        return {"status": "available", "source": TESTNET, "retrieved_at": time.time(), "markets": len(meta[0].get("universe", [])) if meta else 0}
    except Exception as exc:
        return {"status": "unavailable", "source": TESTNET, "reason": type(exc).__name__}


def strategy_contract():
    return {
        "name": "HL-Liquid-Conservative-Intraday-v1",
        "timeframes": {"regime": "1h", "signal": "15m", "execution": "1m"},
        "signals": {"trend": "EMA20/EMA50/EMA200", "momentum": "RSI14", "volatility": "ATR14", "breakout": "completed 20-candle high/low with volume >= 1.2x average"},
        "exits": {"stop": "1.5x ATR or swing, max 2%", "take_profit": "50% at 2R; trail remainder", "max_hold": "8h"},
        "controls": ["isolated margin", "no averaging", "no martingale", "reduce-only stop", "reconcile before every order", "kill-switch on stale data, missing stop, drawdown or API failure"],
    }


def validate_risk():
    values = RISK.__dict__
    assert values["max_drawdown"] <= 0.05
    assert values["risk_per_trade"] <= values["max_open_risk"]
    assert values["daily_loss"] < values["weekly_loss"] < values["max_drawdown"]
    return True

validate_risk()


def guarded_order(*_args, **_kwargs):
    """Fail closed: order signing/execution is impossible without explicit testnet setup."""
    c = config()
    if not c["execution"]:
        raise RuntimeError("TESTNET_TRADING_ENABLED=false")
    if not c["credentials"]:
        raise RuntimeError("HYPERLIQUID_TESTNET_PRIVATE_KEY fehlt")
    raise RuntimeError("Orderadapter noch nicht freigegeben; Review erforderlich")


if __name__ == "__main__":
    print(json.dumps({"config": config(), "snapshot": snapshot()}, ensure_ascii=False))
