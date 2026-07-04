import pytest

from arbot.pairs import candidate_yaml, enabled_pairs, load_pairs, suggest_pairs

from .conftest import load_fixture

PAIRS_YAML = """
pairs:
  - id: fed-sept
    kalshi_ticker: KXFED-26SEP
    polymarket_condition_id: "0xfed"
    polymarket_yes_token_id: "901"
    polymarket_no_token_id: "902"
    notes: "resolution sources differ: Fed statement vs UMA oracle"
    enabled: true
  - id: nba-bos
    kalshi_ticker: KXNBA-26-BOS
    polymarket_condition_id: "0xnba"
    polymarket_yes_token_id: "903"
    polymarket_no_token_id: "904"
    enabled: false
"""


class TestLoadPairs:
    def test_load_and_filter_enabled(self, tmp_path):
        f = tmp_path / "pairs.yaml"
        f.write_text(PAIRS_YAML)
        all_pairs = load_pairs(f)
        assert len(all_pairs) == 2
        assert all_pairs[0].notes.startswith("resolution sources differ")
        active = enabled_pairs(f)
        assert [p.pair_id for p in active] == ["fed-sept"]

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_pairs(tmp_path / "nope.yaml")

    def test_duplicate_ids_rejected(self, tmp_path):
        f = tmp_path / "pairs.yaml"
        f.write_text(PAIRS_YAML.replace("nba-bos", "fed-sept"))
        with pytest.raises(ValueError, match="duplicate"):
            load_pairs(f)


class TestSuggestPairs:
    def test_fuzzy_match_finds_true_pairs_only(self):
        kalshi = load_fixture("kalshi_markets.json")
        poly = load_fixture("polymarket_markets.json")
        candidates = suggest_pairs(kalshi, poly, min_score=0.6)
        matched = {(c.kalshi_ticker, c.polymarket_condition_id) for c in candidates}
        assert ("KXFED-26SEP-CUT", "0xfed") in matched
        assert ("KXNBA-26-BOS", "0xnba") in matched
        # weather/btc shouldn't cross-match anything
        assert all(c.polymarket_condition_id != "0xbtc" for c in candidates)
        assert all(c.kalshi_ticker != "KXWEATHER-NYC" for c in candidates)

    def test_candidates_sorted_by_score(self):
        kalshi = load_fixture("kalshi_markets.json")
        poly = load_fixture("polymarket_markets.json")
        candidates = suggest_pairs(kalshi, poly, min_score=0.3)
        scores = [c.score for c in candidates]
        assert scores == sorted(scores, reverse=True)

    def test_yaml_snippet_defaults_to_disabled(self):
        kalshi = load_fixture("kalshi_markets.json")
        poly = load_fixture("polymarket_markets.json")
        c = suggest_pairs(kalshi, poly, min_score=0.6)[0]
        snippet = candidate_yaml(c)
        assert "enabled: false" in snippet
        assert "TODO: verify resolution criteria" in snippet
        assert c.polymarket_yes_token_id in snippet
        assert c.polymarket_no_token_id in snippet
