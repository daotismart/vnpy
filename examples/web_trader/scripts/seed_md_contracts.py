"""One-shot: seed Redis contract cache from web MainEngine for md_receiver.

Usage (ScriptTrader): upload + start. Safe to run while live strategy is paused.
After seeding, restart md_receiver so it rewarms and subscribes.
"""

from __future__ import annotations

import json
import traceback

from vnpy_scripttrader import ScriptEngine


def run(engine: ScriptEngine) -> None:
    try:
        from md_bus import store_contracts_to_redis
    except Exception:
        engine.write_log("md_bus.store_contracts_to_redis unavailable — fallback redis hset")
        store_contracts_to_redis = None  # type: ignore

    contracts = list(engine.main_engine.get_all_contracts() or [])
    prefs = ("IF", "IO")
    filtered = [
        c
        for c in contracts
        if str(getattr(c, "symbol", "") or "").upper().startswith(prefs)
    ]
    engine.write_log(f"[SEED] contracts total={len(contracts)} filtered={len(filtered)}")
    if store_contracts_to_redis is not None:
        result = store_contracts_to_redis(filtered or contracts)
        engine.write_log(f"[SEED] redis result={result}")
    else:
        try:
            import redis
            from md_bus import contract_to_dict, contracts_key, redis_url

            client = redis.Redis.from_url(redis_url(), decode_responses=True)
            n = 0
            for contract in filtered or contracts:
                client.hset(
                    contracts_key(),
                    contract.vt_symbol,
                    json.dumps(contract_to_dict(contract), ensure_ascii=False),
                )
                n += 1
            engine.write_log(f"[SEED] stored={n} total_hash={client.hlen(contracts_key())}")
        except Exception:
            engine.write_log("[SEED] failed\n" + traceback.format_exc())
            return
    engine.write_log("[SEED] done — please restart md_receiver container")
