"""Status report: positions, locked capital, edges seen vs taken, skips."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from .ledger import Ledger


def build_report(ledger: Ledger, include_dry_run: bool = False) -> str:
    lines: list[str] = []
    today = dt.date.today().isoformat()
    lines.append(f"=== arbot report — {today} ===")

    positions = ledger.open_positions(include_dry_run=include_dry_run)
    lines.append("")
    lines.append(f"Open positions ({len(positions)}):")
    if not positions:
        lines.append("  (none)")
    locked = Decimal(0)
    for p in positions:
        cost = Decimal(str(p["net_cost"]))
        locked += cost
        lines.append(
            f"  {p['platform']:<11} {p['market_id']:<30} {p['outcome']:<4} "
            f"size={p['net_size']:>10.2f}  cost=${cost:.2f}"
        )
    lines.append(f"Locked capital: ${locked:.2f}")

    pnl = ledger.realized_pnl_today()
    lines.append(f"Realized P&L today: ${pnl:.2f}")

    edges = ledger.edges_today()
    lines.append("")
    lines.append(
        f"Edges today: {edges['seen']} seen, {edges['taken']} taken, "
        f"est. profit on taken ${edges['est_profit_taken']:.2f}"
    )

    best = ledger.best_edges_today()
    if best:
        lines.append("Top edges today:")
        for b in best:
            ts = dt.datetime.fromtimestamp(b["ts"]).strftime("%H:%M:%S")
            status = "TAKEN" if b["taken"] else (b["skip_reason"] or "skipped")
            edge = Decimal(b["edge_bps"]).quantize(Decimal("0.1"))
            size = b["size"] or "-"
            lines.append(
                f"  {ts}  {b['pair_id']:<24} {b['direction']:<22} "
                f"{edge:>8}bps size={size:<8} {status}"
            )

    skips = ledger.skip_reasons_today()
    lines.append("")
    lines.append("Skip reasons today:")
    if not skips:
        lines.append("  (none)")
    for reason, n in skips:
        lines.append(f"  {reason:<32} {n}")

    return "\n".join(lines)
