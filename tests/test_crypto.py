"""加密误定价信号测试：解析 + 对数正态定价 + edge 检测。"""
import math

import pytest

from config import Config
from strategy.signals import crypto_mispricing as cm
from strategy.models import Market, OutcomeBook, OpportunityKind
from strategy import edge_detector


# ── 解析 ──────────────────────────────────────────────────
def test_parse_above_with_comma():
    t = cm.parse_market("Will the price of Bitcoin be above $58,000 on June 30?")
    assert t.asset == "bitcoin"
    assert t.strike == 58000.0
    assert t.direction == "above"


def test_parse_reach_k_suffix():
    t = cm.parse_market("Will Bitcoin reach $90k in June?")
    assert t.strike == 90000.0
    assert t.direction == "above"


def test_parse_below():
    t = cm.parse_market("Will Ethereum dip to $2,000 this week?")
    assert t.asset == "ethereum"
    assert t.strike == 2000.0
    assert t.direction == "below"


def test_parse_non_crypto_returns_none():
    assert cm.parse_market("Will Trump win the election?") is None


def test_parse_missing_price_none():
    assert cm.parse_market("Will Bitcoin go up?") is None


# ── 对数正态定价 ──────────────────────────────────────────
def test_prob_above_deep_itm():
    # 现价远高于 strike，短期 → p≈1
    p = cm.prob_above(spot=100000, strike=50000, annual_vol=0.55,
                      years_to_expiry=0.01)
    assert p > 0.99


def test_prob_above_deep_otm():
    p = cm.prob_above(spot=50000, strike=100000, annual_vol=0.55,
                      years_to_expiry=0.01)
    assert p < 0.01


def test_prob_above_at_expiry_degenerate():
    # T→0：退化为现价是否过线
    assert cm.prob_above(60000, 58000, 0.55, 0.0) == 1.0
    assert cm.prob_above(56000, 58000, 0.55, 0.0) == 0.0


def test_prob_above_atm_near_half():
    # 现价≈strike，短期 → p 接近 0.5
    p = cm.prob_above(spot=58000, strike=58000, annual_vol=0.55,
                      years_to_expiry=0.02)
    assert 0.45 < p < 0.55


def test_estimate_true_prob_below_is_complement():
    terms_above = cm.CryptoTerms("bitcoin", 58000, "above")
    terms_below = cm.CryptoTerms("bitcoin", 58000, "below")
    pa = cm.estimate_true_prob(terms_above, 60000, 0.55, 0.02)
    pb = cm.estimate_true_prob(terms_below, 60000, 0.55, 0.02)
    assert abs((pa + pb) - 1.0) < 1e-9


def test_confidence_higher_near_expiry():
    near = cm.confidence_from_expiry(0.5 / 365)   # 半天
    far = cm.confidence_from_expiry(60 / 365)     # 60天
    assert near > far


# ── detect_crypto_edge（不联网，用假源）──────────────────
class FakeCrypto:
    def __init__(self, price): self.price = price
    def spot(self, asset): return self.price
    def annual_vol(self, asset): return 0.55


def _crypto_market(yes_ask, no_ask, end_ts, question):
    return Market(
        market_id="btc1", question=question,
        outcomes=(
            OutcomeBook("Yes", "tyes", best_ask=yes_ask, best_bid=yes_ask - 0.02,
                        ask_size=1000, bid_size=1000),
            OutcomeBook("No", "tno", best_ask=no_ask, best_bid=no_ask - 0.02,
                        ask_size=1000, bid_size=1000),
        ),
        snapshot_ts=1000.0, end_ts=end_ts,
    )


def test_detect_crypto_edge_finds_mispricing(monkeypatch):
    cfg = Config(edge_min_threshold=0.05)
    monkeypatch.setattr(edge_detector, "CONFIG", cfg)
    # 现价 70000 >> strike 58000，临近到期 → Yes 真实概率≈1
    # 但市场 Yes 报价只 0.60 → 买 Yes 有巨大 edge
    now = 1000.0
    end = now + 3600  # 1 小时后到期
    mkt = _crypto_market(
        yes_ask=0.60, no_ask=0.42, end_ts=end,
        question="Will the price of Bitcoin be above $58,000 on June 30?")
    opps = edge_detector.detect_crypto_edge(mkt, FakeCrypto(70000), now=now)
    assert opps
    # 应产出买 Yes（tyes）的机会，p_true 接近 1
    yes_opp = [o for o in opps if o.legs[0][0] == "tyes"]
    assert yes_opp
    assert yes_opp[0].estimated_p > 0.95
    assert yes_opp[0].raw_edge > 0.3


def test_detect_crypto_edge_no_mispricing_when_fairly_priced(monkeypatch):
    cfg = Config(edge_min_threshold=0.05)
    monkeypatch.setattr(edge_detector, "CONFIG", cfg)
    now = 1000.0
    end = now + 3600
    # 现价 70000 >> strike，Yes 真实≈1，市场 Yes 报价也 0.98 → 无 edge
    mkt = _crypto_market(
        yes_ask=0.98, no_ask=0.03, end_ts=end,
        question="Will the price of Bitcoin be above $58,000 on June 30?")
    opps = edge_detector.detect_crypto_edge(mkt, FakeCrypto(70000), now=now)
    # Yes 无 edge；No 真实≈0 报价 0.03 也无正 edge
    assert all(o.raw_edge >= cfg.edge_min_threshold for o in opps)
    assert not any(o.legs[0][0] == "tyes" for o in opps)


def test_detect_crypto_edge_non_crypto_empty(monkeypatch):
    cfg = Config(edge_min_threshold=0.05)
    monkeypatch.setattr(edge_detector, "CONFIG", cfg)
    mkt = _crypto_market(0.5, 0.5, 2000.0, "Will Trump win?")
    assert edge_detector.detect_crypto_edge(mkt, FakeCrypto(70000), now=1000.0) == []


def test_detect_crypto_edge_price_fetch_fail_empty(monkeypatch):
    cfg = Config(edge_min_threshold=0.05)
    monkeypatch.setattr(edge_detector, "CONFIG", cfg)

    class DeadSource:
        def spot(self, a): return None
        def annual_vol(self, a): return 0.55
    mkt = _crypto_market(
        0.6, 0.42, 4600.0,
        "Will the price of Bitcoin be above $58,000 on June 30?")
    assert edge_detector.detect_crypto_edge(mkt, DeadSource(), now=1000.0) == []
