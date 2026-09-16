"""Valuation: positions × prices → values in base currency, with stale prices flagged."""

from .money import dec, q


def positions_value(engine, prices, day, *, base, fx, asset_classes, currencies=None):
    """Value every open lot. Missing prices are reported in `stale`, never zeroed."""
    currencies = currencies or {}
    by_security, stale = {}, []
    total_value = dec(0)
    total_cost = dec(0)
    for (account, security), _lots in engine._lots.items():
        for lot in _lots:
            if lot.qty <= 0:
                continue
            bucket = by_security.setdefault(
                security,
                {
                    "qty": dec(0),
                    "cost": dec(0),
                    "market_value": dec(0),
                    "unrealized_pnl": dec(0),
                    "asset_class": asset_classes.get(security, "Other"),
                },
            )
            bucket["qty"] += lot.qty
            bucket["cost"] += lot.cost
            price = prices.get((security, day))
            if price is None:
                bucket["market_value"] = None
                if security not in stale:
                    stale.append(security)
                continue
            if bucket["market_value"] is None:
                continue
            ccy = currencies.get(security, lot.currency)
            mv_native = lot.qty * dec(price)
            mv_base = fx.convert(mv_native, ccy, base, day)
            bucket["market_value"] += q(mv_base, 2)
    for security, bucket in by_security.items():
        bucket["cost"] = q(bucket["cost"], 2)
        if bucket["market_value"] is None:
            continue
        bucket["market_value"] = q(bucket["market_value"], 2)
        bucket["unrealized_pnl"] = bucket["market_value"] - bucket["cost"]
        total_value += bucket["market_value"]
        total_cost += bucket["cost"]
    return {
        "day": day,
        "base": base,
        "by_security": by_security,
        "stale": sorted(stale),
        "total_value": total_value,
        "total_cost": total_cost,
        "unrealized_pnl": total_value - total_cost,
    }


def allocation(by_security, key):
    """Percentage allocation by asset_class / currency-like buckets."""
    groups = {}
    for security, bucket in by_security.items():
        if bucket["market_value"] is None:
            continue
        label = bucket.get(key, "Other")
        groups[label] = groups.get(label, dec(0)) + bucket["market_value"]
    total = sum(groups.values(), dec(0))
    if total == 0:
        return {}
    return {name: q(value / total * 100, 2) for name, value in groups.items()}
