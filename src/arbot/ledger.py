"""SQLite ledger: opportunities, orders, fills, P&L, and an append-only
audit log (enforced with triggers; also mirrored to a JSON-lines file).

Money is stored as TEXT and parsed back to Decimal to avoid float drift.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import time
from decimal import Decimal
from pathlib import Path

from .models import Leg, Opportunity

SCHEMA = """
CREATE TABLE IF NOT EXISTS opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    pair_id TEXT NOT NULL,
    direction TEXT NOT NULL,
    edge_bps TEXT,
    size TEXT,
    est_profit TEXT,
    taken INTEGER NOT NULL DEFAULT 0,
    skip_reason TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    opportunity_id INTEGER REFERENCES opportunities(id),
    platform TEXT NOT NULL,
    market_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    action TEXT NOT NULL,            -- buy / sell
    order_type TEXT NOT NULL,        -- limit / market
    price TEXT,
    size TEXT NOT NULL,
    status TEXT NOT NULL,
    external_id TEXT,
    dry_run INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    ts REAL NOT NULL,
    price TEXT NOT NULL,
    size TEXT NOT NULL,
    fee TEXT NOT NULL DEFAULT '0'
);
CREATE TABLE IF NOT EXISTS pnl (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    pair_id TEXT,
    amount TEXT NOT NULL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    event TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS audit_no_update
    BEFORE UPDATE ON audit_log
    BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete
    BEFORE DELETE ON audit_log
    BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
"""


def _day_bounds(day: dt.date | None = None) -> tuple[float, float]:
    day = day or dt.date.today()
    start = dt.datetime.combine(day, dt.time.min).timestamp()
    return start, start + 86400


class Ledger:
    def __init__(self, db_path: str | Path, audit_log_path: str | Path | None = None):
        self.db = sqlite3.connect(str(db_path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()
        self.audit_log_path = Path(audit_log_path) if audit_log_path else None

    def close(self) -> None:
        self.db.close()

    # ---- audit ----

    def audit(self, event: str, payload: dict) -> None:
        ts = time.time()
        line = json.dumps({"ts": ts, "event": event, **payload}, default=str)
        self.db.execute(
            "INSERT INTO audit_log (ts, event, payload) VALUES (?, ?, ?)", (ts, event, line)
        )
        self.db.commit()
        if self.audit_log_path:
            with open(self.audit_log_path, "a") as f:
                f.write(line + "\n")

    # ---- opportunities ----

    def record_opportunity(
        self, opp_or_quote, taken: bool = False, skip_reason: str | None = None
    ) -> int:
        """Accepts an Opportunity, or a DirectionQuote for skipped ones."""
        if isinstance(opp_or_quote, Opportunity):
            opp = opp_or_quote
            row = (
                opp.ts, opp.pair_id, opp.direction, str(opp.edge_bps),
                str(opp.size), str(opp.est_profit), int(taken), skip_reason,
            )
        elif opp_or_quote.opportunity is not None:
            # A viable quote skipped for an external reason (halt, exposure).
            q, opp = opp_or_quote, opp_or_quote.opportunity
            row = (
                opp.ts, opp.pair_id, opp.direction, str(opp.edge_bps),
                str(opp.size), str(opp.est_profit), 0, skip_reason or q.skip_reason,
            )
        else:
            q = opp_or_quote
            edge = str(q.top_edge_bps) if q.top_edge_bps is not None else None
            row = (
                time.time(), q.pair_id, q.direction, edge, None, None, 0,
                skip_reason or q.skip_reason,
            )
        cur = self.db.execute(
            "INSERT INTO opportunities (ts, pair_id, direction, edge_bps, size, est_profit,"
            " taken, skip_reason) VALUES (?,?,?,?,?,?,?,?)",
            row,
        )
        self.db.commit()
        return cur.lastrowid

    # ---- orders / fills / pnl ----

    def record_order(
        self,
        *,
        opportunity_id: int | None,
        leg: Leg,
        action: str,
        order_type: str,
        status: str,
        external_id: str = "",
        dry_run: bool = False,
    ) -> int:
        cur = self.db.execute(
            "INSERT INTO orders (ts, opportunity_id, platform, market_id, outcome, action,"
            " order_type, price, size, status, external_id, dry_run)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                time.time(), opportunity_id, leg.platform.value, leg.market_id,
                leg.outcome.value, action, order_type, str(leg.price), str(leg.size),
                status, external_id, int(dry_run),
            ),
        )
        self.db.commit()
        return cur.lastrowid

    def update_order(self, order_id: int, status: str, external_id: str | None = None) -> None:
        if external_id is not None:
            self.db.execute(
                "UPDATE orders SET status = ?, external_id = ? WHERE id = ?",
                (status, external_id, order_id),
            )
        else:
            self.db.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))
        self.db.commit()

    def record_fill(self, order_id: int, price: Decimal, size: Decimal, fee: Decimal) -> int:
        cur = self.db.execute(
            "INSERT INTO fills (order_id, ts, price, size, fee) VALUES (?,?,?,?,?)",
            (order_id, time.time(), str(price), str(size), str(fee)),
        )
        self.db.commit()
        return cur.lastrowid

    def record_pnl(self, amount: Decimal, pair_id: str | None = None, note: str = "") -> None:
        self.db.execute(
            "INSERT INTO pnl (ts, pair_id, amount, note) VALUES (?,?,?,?)",
            (time.time(), pair_id, str(amount), note),
        )
        self.db.commit()

    # ---- queries ----

    def realized_pnl_today(self) -> Decimal:
        lo, hi = _day_bounds()
        rows = self.db.execute(
            "SELECT amount FROM pnl WHERE ts >= ? AND ts < ?", (lo, hi)
        ).fetchall()
        return sum((Decimal(r["amount"]) for r in rows), Decimal(0))

    def open_positions(self, include_dry_run: bool = False) -> list[dict]:
        """Net position per (platform, market, outcome) from real fills."""
        dry_clause = "" if include_dry_run else "AND o.dry_run = 0"
        rows = self.db.execute(
            f"""
            SELECT o.platform, o.market_id, o.outcome,
                   SUM(CASE WHEN o.action = 'buy' THEN CAST(f.size AS REAL)
                            ELSE -CAST(f.size AS REAL) END) AS net_size,
                   SUM(CASE WHEN o.action = 'buy'
                            THEN CAST(f.size AS REAL) * CAST(f.price AS REAL) + CAST(f.fee AS REAL)
                            ELSE -(CAST(f.size AS REAL) * CAST(f.price AS REAL) - CAST(f.fee AS REAL))
                       END) AS net_cost
            FROM fills f JOIN orders o ON o.id = f.order_id
            WHERE 1=1 {dry_clause}
            GROUP BY o.platform, o.market_id, o.outcome
            HAVING ABS(net_size) > 1e-9
            """
        ).fetchall()
        return [dict(r) for r in rows]

    def exposure_usd(self, market_id: str | None = None) -> Decimal:
        """Capital currently locked in open positions (cost basis)."""
        positions = self.open_positions()
        total = Decimal(0)
        for p in positions:
            if market_id is not None and p["market_id"] != market_id:
                continue
            total += Decimal(str(p["net_cost"]))
        return total

    def edges_today(self) -> dict:
        lo, hi = _day_bounds()
        row = self.db.execute(
            """
            SELECT COUNT(*) AS seen,
                   SUM(taken) AS taken,
                   SUM(CASE WHEN taken = 1 THEN CAST(est_profit AS REAL) ELSE 0 END) AS est_profit_taken
            FROM opportunities WHERE ts >= ? AND ts < ?
            """,
            (lo, hi),
        ).fetchone()
        return {
            "seen": row["seen"] or 0,
            "taken": row["taken"] or 0,
            "est_profit_taken": row["est_profit_taken"] or 0.0,
        }

    def skip_reasons_today(self) -> list[tuple[str, int]]:
        lo, hi = _day_bounds()
        rows = self.db.execute(
            """
            SELECT skip_reason, COUNT(*) AS n FROM opportunities
            WHERE ts >= ? AND ts < ? AND skip_reason IS NOT NULL
            GROUP BY skip_reason ORDER BY n DESC
            """,
            (lo, hi),
        ).fetchall()
        return [(r["skip_reason"], r["n"]) for r in rows]

    def best_edges_today(self, limit: int = 10) -> list[dict]:
        lo, hi = _day_bounds()
        rows = self.db.execute(
            """
            SELECT ts, pair_id, direction, edge_bps, size, est_profit, taken, skip_reason
            FROM opportunities
            WHERE ts >= ? AND ts < ? AND edge_bps IS NOT NULL
            ORDER BY CAST(edge_bps AS REAL) DESC LIMIT ?
            """,
            (lo, hi, limit),
        ).fetchall()
        return [dict(r) for r in rows]
