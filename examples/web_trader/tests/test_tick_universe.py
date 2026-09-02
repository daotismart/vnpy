"""Unit checks for tick-record universe helpers (no CTP required)."""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT.joinpath("server.py")


def _load_helpers():
    source = SERVER.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(SERVER))
    wanted = {"env_flag", "env_int", "should_auto_record_ticks", "chain_record_sort_key", "live_portfolios_from_env", "record_max_chains_from_env", "record_scope_label"}
    body = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
    module = ast.Module(body=body, type_ignores=[])
    code = compile(module, str(SERVER), "exec")
    ns: dict = {"os": __import__("os"), "re": __import__("re"), "Any": object}
    exec(code, ns)
    return ns


def main() -> int:
    helpers = _load_helpers()
    chain_record_sort_key = helpers["chain_record_sort_key"]
    should_auto_record_ticks = helpers["should_auto_record_ticks"]

    assert chain_record_sort_key("IO2603.CFFEX") < chain_record_sort_key("IO2609.CFFEX")
    near = SimpleNamespace(days_to_expiry=5)
    far = SimpleNamespace(days_to_expiry=40)
    assert chain_record_sort_key("IO2609.CFFEX", near) < chain_record_sort_key("IO2603.CFFEX", far)

    os.environ.pop("LIVE_RECORD_TICKS", None)
    os.environ["LIVE_IRON_CONDOR"] = "1"
    assert should_auto_record_ticks() is True
    os.environ["LIVE_RECORD_TICKS"] = "0"
    assert should_auto_record_ticks() is False
    os.environ["LIVE_RECORD_TICKS"] = "1"
    os.environ["LIVE_IRON_CONDOR"] = "0"
    assert should_auto_record_ticks() is True

    os.environ["LIVE_RECORD_MAX_CHAINS"] = "0"
    assert helpers["record_max_chains_from_env"]() == 0
    assert helpers["record_scope_label"](0) == "全部到期月"
    assert "近2" in helpers["record_scope_label"](2)

    # syntax check full server module without importing heavy deps
    ast.parse(SERVER.read_text(encoding="utf-8"))
    print("ok: tick universe helpers")
    return 0


if __name__ == "__main__":
    sys.exit(main())
