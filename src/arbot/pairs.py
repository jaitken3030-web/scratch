"""Manual pair whitelist (pairs.yaml) and the candidate-pair suggester.

Markets are NEVER auto-matched. The bot only trades pairs listed in
pairs.yaml with enabled: true. The suggester exists purely to produce
candidates for a human to review — its output is a YAML snippet you
paste into pairs.yaml after checking resolution criteria yourself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

import yaml

from .models import Pair


def load_pairs(path: str | Path) -> list[Pair]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"pairs file not found: {p} (see pairs.example.yaml)")
    raw = yaml.safe_load(p.read_text()) or {}
    pairs = []
    for item in raw.get("pairs", []):
        pairs.append(
            Pair(
                pair_id=str(item["id"]),
                kalshi_ticker=str(item["kalshi_ticker"]),
                polymarket_condition_id=str(item["polymarket_condition_id"]),
                polymarket_yes_token_id=str(item["polymarket_yes_token_id"]),
                polymarket_no_token_id=str(item["polymarket_no_token_id"]),
                notes=str(item.get("notes", "")),
                enabled=bool(item.get("enabled", True)),
            )
        )
    ids = [p_.pair_id for p_ in pairs]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"duplicate pair ids in pairs.yaml: {sorted(dupes)}")
    return pairs


def enabled_pairs(path: str | Path) -> list[Pair]:
    return [p for p in load_pairs(path) if p.enabled]


# ----- candidate suggestion (human-review helper, never auto-traded) -----

_STOPWORDS = {"will", "the", "a", "an", "of", "in", "on", "by", "be", "to", "at", "for"}


def _normalize(title: str) -> str:
    words = re.sub(r"[^a-z0-9 ]", " ", title.lower()).split()
    return " ".join(w for w in words if w not in _STOPWORDS)


@dataclass(frozen=True)
class Candidate:
    score: float
    kalshi_ticker: str
    kalshi_title: str
    polymarket_condition_id: str
    polymarket_yes_token_id: str
    polymarket_no_token_id: str
    polymarket_question: str


def suggest_pairs(
    kalshi_markets: list[dict],
    polymarket_markets: list[dict],
    min_score: float = 0.6,
    limit: int = 50,
) -> list[Candidate]:
    """Fuzzy-match market titles across platforms.

    kalshi_markets: dicts with at least `ticker` and `title`.
    polymarket_markets: dicts with `condition_id`, `question`, and
    `tokens` (list of {token_id, outcome}).
    """
    candidates: list[Candidate] = []
    poly_prepped = []
    for pm in polymarket_markets:
        tokens = {t.get("outcome", "").lower(): t.get("token_id", "") for t in pm.get("tokens", [])}
        poly_prepped.append((pm, _normalize(pm.get("question", "")), tokens))

    for km in kalshi_markets:
        k_norm = _normalize(km.get("title", ""))
        if not k_norm:
            continue
        for pm, p_norm, tokens in poly_prepped:
            if not p_norm:
                continue
            score = SequenceMatcher(None, k_norm, p_norm).ratio()
            if score >= min_score:
                candidates.append(
                    Candidate(
                        score=round(score, 3),
                        kalshi_ticker=km.get("ticker", ""),
                        kalshi_title=km.get("title", ""),
                        polymarket_condition_id=pm.get("condition_id", ""),
                        polymarket_yes_token_id=tokens.get("yes", ""),
                        polymarket_no_token_id=tokens.get("no", ""),
                        polymarket_question=pm.get("question", ""),
                    )
                )
    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates[:limit]


def candidate_yaml(c: Candidate) -> str:
    """YAML snippet for pairs.yaml — review before enabling."""
    pair_id = re.sub(r"[^a-z0-9-]", "-", c.kalshi_ticker.lower())
    return (
        f"  - id: {pair_id}\n"
        f"    kalshi_ticker: {c.kalshi_ticker}\n"
        f"    polymarket_condition_id: \"{c.polymarket_condition_id}\"\n"
        f"    polymarket_yes_token_id: \"{c.polymarket_yes_token_id}\"\n"
        f"    polymarket_no_token_id: \"{c.polymarket_no_token_id}\"\n"
        f"    notes: \"TODO: verify resolution criteria match. "
        f"K: {c.kalshi_title} | P: {c.polymarket_question} (score {c.score})\"\n"
        f"    enabled: false  # flip to true only after manual review\n"
    )
