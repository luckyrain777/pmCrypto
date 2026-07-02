"""结算判定测试：用 list_markets(condition_ids=) 查市场，价格收敛即定胜负
（不强依赖不可靠的 state.closed 标志）。"""
from data.resolutions import resolve_from_client


class _Side:
    def __init__(self, token_id, price):
        self.token_id = token_id
        self.price = price


class _Outcomes:
    def __init__(self, yes, no):
        self.yes = yes
        self.no = no


class _Market:
    def __init__(self, yes_price, no_price):
        self.outcomes = _Outcomes(_Side("tokYES", yes_price), _Side("tokNO", no_price))


class _Pager:
    def __init__(self, items): self._items = items
    def iter_items(self): return iter(self._items)


class _Client:
    """假 PublicClient：list_markets(condition_ids=) 返回预置市场。"""
    def __init__(self, market): self._m = market
    def list_markets(self, **kw):
        return _Pager([self._m] if self._m is not None else [])


def test_converged_yes_wins():
    # 价格收敛到 Yes≈1 → 判 Yes 获胜（即使 closed 标志缺失/False）
    c = _Client(_Market(yes_price=0.999, no_price=0.001))
    assert resolve_from_client(c, "m") == "tokYES"


def test_converged_no_wins():
    c = _Client(_Market(yes_price=0.002, no_price=0.998))
    assert resolve_from_client(c, "m") == "tokNO"


def test_not_converged_returns_none():
    # 价格还在中间（未定胜负）→ 不判
    c = _Client(_Market(yes_price=0.55, no_price=0.45))
    assert resolve_from_client(c, "m") is None


def test_market_not_found_returns_none():
    c = _Client(None)
    assert resolve_from_client(c, "m") is None


class _ClosedOnlyClient:
    """模拟短周期加密市场：默认(活跃)查询返回空，只有 closed=True 才查到。"""
    def __init__(self, market): self._m = market
    def list_markets(self, **kw):
        if kw.get("closed") is True:
            return _Pager([self._m])
        return _Pager([])   # 活跃列表里已消失


def test_closed_market_resolved_via_closed_flag():
    # 结算后从活跃列表消失、价格收敛的市场，必须靠 closed=True 查回并判定
    c = _ClosedOnlyClient(_Market(yes_price=0.0, no_price=1.0))
    assert resolve_from_client(c, "m") == "tokNO"
