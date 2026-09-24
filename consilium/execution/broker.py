"""Execution — delta orders and a simulated broker with realistic costs.

Sizing floors toward zero (never overshoot); sells go first so they fund the
buys; dust orders below a minimum notional are suppressed. The SimBroker fills every order but charges for it: half-spread +
slippage move the price against you, commission is deducted from cash. Cash
may go negative on a levered mandate and stays visible — nothing pretends to
enforce margin.
"""

from __future__ import annotations

from typing import Protocol

from consilium.core.models import Fill, Order, Position
from consilium.core.spec import CostModel


class Broker(Protocol):
    def positions(self) -> dict[str, Position]: ...
    def cash(self) -> float: ...
    def place_order(self, order: Order) -> Fill: ...


def build_orders(target_weights: dict[str, float], positions: dict[str, Position],
                 marks: dict[str, float], equity: float, min_trade_pct: float = 0.002) -> list[Order]:
    """Diff the target book against the current one.

    Orders whose notional is below `min_trade_pct` of equity are not emitted
    unless they close a position: share-rounding drift as NAV moves is not a
    reason to pay the spread. Exits always execute in full.
    """
    sells, buys = [], []
    for t in sorted(set(target_weights) | set(positions)):
        mark = marks[t]
        target_w = target_weights.get(t, 0.0)
        target = int(target_w * equity / mark)
        current = positions[t].shares if t in positions else 0
        delta = target - current
        if delta == 0:
            continue
        if target != 0 and abs(delta) * mark < min_trade_pct * equity:
            continue
        o = Order(ticker=t, side="buy" if delta > 0 else "sell", quantity=abs(delta), reference_price=mark)
        (buys if delta > 0 else sells).append(o)
    return sells + buys


class SimBroker:
    def __init__(self, cash: float, costs: CostModel | None = None, positions: dict[str, Position] | None = None) -> None:
        self._cash = cash
        self._costs = costs or CostModel()
        self._pos: dict[str, Position] = dict(positions or {})

    def positions(self) -> dict[str, Position]:
        return {t: p for t, p in self._pos.items() if p.shares != 0}

    def cash(self) -> float:
        return self._cash

    def place_order(self, order: Order) -> Fill:
        if order.reference_price <= 0:
            raise ValueError(f"cannot fill {order.ticker} at {order.reference_price}")
        bps = (self._costs.slippage_bps + self._costs.half_spread_bps) / 10_000
        px = order.reference_price * (1 + bps if order.side == "buy" else 1 - bps)
        notional = px * order.quantity
        commission = notional * self._costs.commission_bps / 10_000
        slip = abs(px - order.reference_price) * order.quantity
        signed = order.quantity if order.side == "buy" else -order.quantity
        cur = self._pos.get(order.ticker, Position(ticker=order.ticker, shares=0, avg_cost=0.0))
        new_shares = cur.shares + signed
        # average cost tracks the open side; a flip resets it
        if cur.shares == 0 or (cur.shares > 0) != (new_shares > 0):
            avg = px
        elif abs(new_shares) > abs(cur.shares):
            avg = (cur.avg_cost * abs(cur.shares) + px * order.quantity) / abs(new_shares)
        else:
            avg = cur.avg_cost
        self._pos[order.ticker] = Position(ticker=order.ticker, shares=new_shares, avg_cost=avg)
        if new_shares == 0:
            del self._pos[order.ticker]
        self._cash += -notional - commission if order.side == "buy" else notional - commission
        return Fill(ticker=order.ticker, side=order.side, quantity=order.quantity, price=px,
                    commission=commission, slippage_cost=slip)
