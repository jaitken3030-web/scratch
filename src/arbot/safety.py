"""Safety rails: kill switch, daily loss halt, exposure caps, alerts."""

from __future__ import annotations

import logging
from decimal import Decimal
from pathlib import Path

import httpx

from .config import SafetyConfig, SizingConfig
from .ledger import Ledger

log = logging.getLogger("arbot.safety")


class SafetyManager:
    def __init__(
        self,
        cfg: SafetyConfig,
        sizing: SizingConfig,
        ledger: Ledger,
        http: httpx.AsyncClient | None = None,
    ):
        self.cfg = cfg
        self.sizing = sizing
        self.ledger = ledger
        self.http = http or httpx.AsyncClient(timeout=5)
        self._halted_reason: str | None = None

    # ---- gates ----

    def kill_switch_active(self) -> bool:
        return Path(self.cfg.kill_switch_file).exists()

    def daily_loss_exceeded(self) -> bool:
        pnl = self.ledger.realized_pnl_today()
        return pnl <= -self.cfg.daily_loss_limit_usd

    async def trading_allowed(self) -> tuple[bool, str]:
        """Master gate, checked every cycle and before every order."""
        if self.kill_switch_active():
            return False, f"kill switch file present: {self.cfg.kill_switch_file}"
        if self.daily_loss_exceeded():
            if self._halted_reason != "daily_loss":
                self._halted_reason = "daily_loss"
                await self.alert(
                    "HALT: daily loss limit reached "
                    f"(pnl today {self.ledger.realized_pnl_today()}, "
                    f"limit -{self.cfg.daily_loss_limit_usd})"
                )
            return False, "daily loss limit reached"
        self._halted_reason = None
        return True, ""

    def pair_headroom_usd(self, kalshi_ticker: str, poly_token_ids: tuple[str, str]) -> Decimal:
        """Remaining budget for a pair given per-market and total caps."""
        total_exposure = self.ledger.exposure_usd()
        total_headroom = self.sizing.max_total_exposure_usd - total_exposure
        market_exposure = self.ledger.exposure_usd(kalshi_ticker) + sum(
            (self.ledger.exposure_usd(t) for t in poly_token_ids), Decimal(0)
        )
        market_headroom = self.cfg.per_market_exposure_usd - market_exposure
        return max(Decimal(0), min(total_headroom, market_headroom))

    # ---- alerting ----

    async def alert(self, message: str, **context) -> None:
        """Log + audit + optional webhook. Never raises."""
        log.error("ALERT: %s %s", message, context or "")
        try:
            self.ledger.audit("alert", {"message": message, **context})
        except Exception:
            log.exception("failed to write alert to audit log")
        if self.cfg.webhook_url:
            try:
                await self.http.post(self.cfg.webhook_url, json={"text": f"[arbot] {message}"})
            except Exception:
                log.exception("alert webhook delivery failed")

    async def alert_unhedged(self, pair_id: str, detail: dict) -> None:
        await self.alert(f"UNHEDGED POSITION on pair {pair_id}", **detail)

    async def close(self) -> None:
        await self.http.aclose()
