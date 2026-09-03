"""Unit tests for Redis MD bus tick/contract serialization."""

from __future__ import annotations

from datetime import datetime, timezone

from vnpy.trader.constant import Exchange, OptionType, Product
from vnpy.trader.object import ContractData, TickData

from md_bus import contract_from_dict, contract_to_dict, tick_from_dict, tick_to_dict


def test_tick_roundtrip() -> None:
    tick = TickData(
        gateway_name="CTP",
        symbol="IO2609-C-4600",
        exchange=Exchange.CFFEX,
        datetime=datetime(2026, 9, 3, 14, 30, 0, tzinfo=timezone.utc),
        last_price=12.4,
        bid_price_1=12.2,
        ask_price_1=12.6,
        volume=100,
    )
    restored = tick_from_dict(tick_to_dict(tick))
    assert restored.vt_symbol == "IO2609-C-4600.CFFEX"
    assert restored.last_price == 12.4
    assert restored.bid_price_1 == 12.2
    assert restored.ask_price_1 == 12.6
    assert restored.volume == 100


def test_contract_roundtrip() -> None:
    contract = ContractData(
        gateway_name="CTP",
        symbol="IO2609-C-4600",
        exchange=Exchange.CFFEX,
        name="IO2609-C-4600",
        product=Product.OPTION,
        size=100,
        pricetick=0.2,
    )
    contract.option_type = OptionType.CALL
    contract.option_strike = 4600
    contract.option_underlying = "IO2609.CFFEX"
    restored = contract_from_dict(contract_to_dict(contract))
    assert restored.vt_symbol == "IO2609-C-4600.CFFEX"
    assert restored.product == Product.OPTION
    assert restored.option_type == OptionType.CALL
    assert restored.option_strike == 4600


if __name__ == "__main__":
    test_tick_roundtrip()
    test_contract_roundtrip()
    print("ok: md_bus serialize")
