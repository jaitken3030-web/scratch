"""CLI: arbot run | suggest-pairs | report."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from .config import load_config
from .ledger import Ledger
from .pairs import candidate_yaml, enabled_pairs, suggest_pairs
from .report import build_report


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


async def _cmd_run(args) -> int:
    from .clients import KalshiClient, PolymarketClient
    from .executor import ArbExecutor
    from .safety import SafetyManager
    from .scanner import BookStore, Scanner

    cfg = load_config(args.config)
    pairs = enabled_pairs(cfg.pairs_file)
    if not pairs:
        print("No enabled pairs in pairs.yaml — nothing to scan.", file=sys.stderr)
        return 1

    ledger = Ledger(cfg.db_path, cfg.audit_log_path)
    kalshi = KalshiClient(
        cfg.kalshi_base_url,
        api_key_id=cfg.credentials.kalshi_api_key_id,
        private_key_path=cfg.credentials.kalshi_private_key_path,
    )
    poly = PolymarketClient(
        cfg.polymarket_clob_url,
        gamma_url=cfg.polymarket_gamma_url,
        api_key=cfg.credentials.polymarket_api_key,
        api_secret=cfg.credentials.polymarket_api_secret,
        api_passphrase=cfg.credentials.polymarket_api_passphrase,
        private_key=cfg.credentials.polymarket_private_key,
    )
    safety = SafetyManager(cfg.safety, cfg.sizing, ledger)
    executor = ArbExecutor(
        {"kalshi": kalshi, "polymarket": poly}, ledger, safety, cfg.execution, cfg.fees
    )
    store = BookStore(kalshi, poly)
    scanner = Scanner(cfg, pairs, store, executor, ledger, safety)

    if not cfg.execution.dry_run:
        print("*** LIVE MODE — real orders will be placed ***", file=sys.stderr)

    ledger.audit("startup", {"pairs": [p.pair_id for p in pairs], "dry_run": cfg.execution.dry_run})
    try:
        await scanner.run_forever()
    finally:
        await kalshi.close()
        await poly.close()
        await safety.close()
        ledger.close()
    return 0


async def _cmd_suggest_pairs(args) -> int:
    cfg = load_config(args.config)

    if args.kalshi_json:
        kalshi_markets = json.loads(Path(args.kalshi_json).read_text())
    else:
        from .clients import KalshiClient

        client = KalshiClient(cfg.kalshi_base_url)
        try:
            kalshi_markets = await client.list_markets(limit=args.limit)
        finally:
            await client.close()

    if args.poly_json:
        poly_markets = json.loads(Path(args.poly_json).read_text())
    else:
        from .clients import PolymarketClient

        client = PolymarketClient(cfg.polymarket_clob_url, gamma_url=cfg.polymarket_gamma_url)
        try:
            poly_markets = await client.list_markets(limit=args.limit)
        finally:
            await client.close()

    candidates = suggest_pairs(kalshi_markets, poly_markets, min_score=args.min_score)
    if not candidates:
        print("No candidates above score threshold.")
        return 0

    print(f"# {len(candidates)} candidate pairs — REVIEW RESOLUTION CRITERIA BEFORE ENABLING.")
    print("# Paste approved entries into pairs.yaml under `pairs:` and set enabled: true.\n")
    print("pairs:")
    for c in candidates:
        print(f"# score={c.score}  K: {c.kalshi_title}")
        print(f"#              P: {c.polymarket_question}")
        print(candidate_yaml(c))
    return 0


def _cmd_report(args) -> int:
    cfg = load_config(args.config)
    ledger = Ledger(cfg.db_path, cfg.audit_log_path)
    try:
        print(build_report(ledger, include_dry_run=args.include_dry_run))
    finally:
        ledger.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="arbot", description="Kalshi/Polymarket arbitrage bot")
    parser.add_argument("-c", "--config", default="config.yaml", help="path to config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="poll books, scan for arbs, execute (dry-run by default)")

    sp = sub.add_parser("suggest-pairs", help="fuzzy-match market titles; prints YAML candidates")
    sp.add_argument("--min-score", type=float, default=0.6)
    sp.add_argument("--limit", type=int, default=200, help="markets fetched per platform")
    sp.add_argument("--kalshi-json", help="offline: read Kalshi markets from a JSON file")
    sp.add_argument("--poly-json", help="offline: read Polymarket markets from a JSON file")

    rp = sub.add_parser("report", help="positions, locked capital, edges, skip reasons")
    rp.add_argument("--include-dry-run", action="store_true", help="count dry-run orders as positions")

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    if args.command == "run":
        return asyncio.run(_cmd_run(args))
    if args.command == "suggest-pairs":
        return asyncio.run(_cmd_suggest_pairs(args))
    if args.command == "report":
        return _cmd_report(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
