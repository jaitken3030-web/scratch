"""Configuration: config.yaml for behavior, .env for credentials."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import yaml
from dotenv import load_dotenv


@dataclass(frozen=True)
class FeeConfig:
    # Kalshi general fee: ceil_to_cent(rate * contracts * P * (1-P)).
    kalshi_rate: Decimal = Decimal("0.07")
    # Polymarket: rate * min(p, 1-p) * shares. 0 on most markets today.
    polymarket_rate: Decimal = Decimal("0")


@dataclass(frozen=True)
class SizingConfig:
    max_usd_per_arb: Decimal = Decimal("100")
    max_total_exposure_usd: Decimal = Decimal("500")
    max_book_depth_pct: Decimal = Decimal("0.25")  # take at most 25% of visible depth


@dataclass(frozen=True)
class SafetyConfig:
    kill_switch_file: str = "KILL"
    daily_loss_limit_usd: Decimal = Decimal("50")
    per_market_exposure_usd: Decimal = Decimal("200")
    webhook_url: str = ""  # optional alert webhook (Slack/Discord-style JSON POST)


@dataclass(frozen=True)
class ExecutionConfig:
    dry_run: bool = True
    second_leg_timeout_s: float = 10.0
    poll_fill_interval_s: float = 0.5


@dataclass(frozen=True)
class Credentials:
    kalshi_api_key_id: str = ""
    kalshi_private_key_path: str = ""
    polymarket_api_key: str = ""
    polymarket_api_secret: str = ""
    polymarket_api_passphrase: str = ""
    polymarket_private_key: str = ""  # wallet key, only needed for live orders


@dataclass(frozen=True)
class Config:
    min_edge_bps: Decimal = Decimal("150")
    poll_interval_s: float = 2.0
    pairs_file: str = "pairs.yaml"
    db_path: str = "arbot.db"
    audit_log_path: str = "audit.log"
    kalshi_base_url: str = "https://api.elections.kalshi.com/trade-api/v2"
    polymarket_clob_url: str = "https://clob.polymarket.com"
    polymarket_gamma_url: str = "https://gamma-api.polymarket.com"
    fees: FeeConfig = field(default_factory=FeeConfig)
    sizing: SizingConfig = field(default_factory=SizingConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    credentials: Credentials = field(default_factory=Credentials)


def _dec(v, default: Decimal) -> Decimal:
    return Decimal(str(v)) if v is not None else default


def load_config(path: str | Path = "config.yaml", env_file: str | Path | None = None) -> Config:
    """Load config.yaml (all keys optional) and credentials from .env."""
    load_dotenv(env_file)  # no-op if the file doesn't exist

    raw: dict = {}
    p = Path(path)
    if p.exists():
        raw = yaml.safe_load(p.read_text()) or {}

    fees_raw = raw.get("fees", {})
    sizing_raw = raw.get("sizing", {})
    safety_raw = raw.get("safety", {})
    exec_raw = raw.get("execution", {})

    creds = Credentials(
        kalshi_api_key_id=os.getenv("KALSHI_API_KEY_ID", ""),
        kalshi_private_key_path=os.getenv("KALSHI_PRIVATE_KEY_PATH", ""),
        polymarket_api_key=os.getenv("POLYMARKET_API_KEY", ""),
        polymarket_api_secret=os.getenv("POLYMARKET_API_SECRET", ""),
        polymarket_api_passphrase=os.getenv("POLYMARKET_API_PASSPHRASE", ""),
        polymarket_private_key=os.getenv("POLYMARKET_PRIVATE_KEY", ""),
    )

    defaults = Config()
    return Config(
        min_edge_bps=_dec(raw.get("min_edge_bps"), defaults.min_edge_bps),
        poll_interval_s=float(raw.get("poll_interval_s", defaults.poll_interval_s)),
        pairs_file=raw.get("pairs_file", defaults.pairs_file),
        db_path=raw.get("db_path", defaults.db_path),
        audit_log_path=raw.get("audit_log_path", defaults.audit_log_path),
        kalshi_base_url=raw.get("kalshi_base_url", defaults.kalshi_base_url),
        polymarket_clob_url=raw.get("polymarket_clob_url", defaults.polymarket_clob_url),
        polymarket_gamma_url=raw.get("polymarket_gamma_url", defaults.polymarket_gamma_url),
        fees=FeeConfig(
            kalshi_rate=_dec(fees_raw.get("kalshi_rate"), defaults.fees.kalshi_rate),
            polymarket_rate=_dec(fees_raw.get("polymarket_rate"), defaults.fees.polymarket_rate),
        ),
        sizing=SizingConfig(
            max_usd_per_arb=_dec(sizing_raw.get("max_usd_per_arb"), defaults.sizing.max_usd_per_arb),
            max_total_exposure_usd=_dec(
                sizing_raw.get("max_total_exposure_usd"), defaults.sizing.max_total_exposure_usd
            ),
            max_book_depth_pct=_dec(
                sizing_raw.get("max_book_depth_pct"), defaults.sizing.max_book_depth_pct
            ),
        ),
        safety=SafetyConfig(
            kill_switch_file=safety_raw.get("kill_switch_file", defaults.safety.kill_switch_file),
            daily_loss_limit_usd=_dec(
                safety_raw.get("daily_loss_limit_usd"), defaults.safety.daily_loss_limit_usd
            ),
            per_market_exposure_usd=_dec(
                safety_raw.get("per_market_exposure_usd"), defaults.safety.per_market_exposure_usd
            ),
            webhook_url=safety_raw.get("webhook_url", os.getenv("ALERT_WEBHOOK_URL", "")),
        ),
        execution=ExecutionConfig(
            dry_run=bool(exec_raw.get("dry_run", defaults.execution.dry_run)),
            second_leg_timeout_s=float(
                exec_raw.get("second_leg_timeout_s", defaults.execution.second_leg_timeout_s)
            ),
            poll_fill_interval_s=float(
                exec_raw.get("poll_fill_interval_s", defaults.execution.poll_fill_interval_s)
            ),
        ),
        credentials=creds,
    )
