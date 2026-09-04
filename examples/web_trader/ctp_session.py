"""CTP connect helpers: allow MD-only / TD-only sessions for Redis MD bus split."""

from __future__ import annotations

import os
from typing import Any


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def patch_ctp_connect_modes() -> None:
    """Monkey-patch CtpGateway.connect for LIVE_CTP_SKIP_MD / LIVE_CTP_SKIP_TD."""
    from vnpy_ctp.gateway.ctp_gateway import CtpGateway

    if getattr(CtpGateway.connect, "_vnpy_md_bus_patched", False):
        return

    original = CtpGateway.connect

    def connect(self, setting: dict) -> None:
        userid: str = setting["用户名"]
        password: str = setting["密码"]
        brokerid: str = setting["经纪商代码"]
        td_address: str = setting["交易服务器"]
        md_address: str = setting["行情服务器"]
        appid: str = setting["产品名称"]
        auth_code: str = setting["授权编码"]
        production_mode: bool = setting.get("柜台环境", "实盘") == "实盘"

        if (
            (not td_address.startswith("tcp://"))
            and (not td_address.startswith("ssl://"))
            and (not td_address.startswith("socks"))
        ):
            td_address = "tcp://" + td_address
        if (
            (not md_address.startswith("tcp://"))
            and (not md_address.startswith("ssl://"))
            and (not md_address.startswith("socks"))
        ):
            md_address = "tcp://" + md_address

        skip_md = env_flag("LIVE_CTP_SKIP_MD")
        skip_td = env_flag("LIVE_CTP_SKIP_TD")

        if not skip_td:
            self.td_api.connect(td_address, userid, password, brokerid, auth_code, appid, production_mode)
        else:
            self.write_log("LIVE_CTP_SKIP_TD=1，跳过交易前置登录")

        if not skip_md:
            self.md_api.connect(md_address, userid, password, brokerid, production_mode)
        else:
            self.write_log("LIVE_CTP_SKIP_MD=1，跳过行情前置登录（行情来自 Redis MD bus）")

        if not skip_td:
            self.init_query()

    connect._vnpy_md_bus_patched = True  # type: ignore[attr-defined]
    CtpGateway.connect = connect  # type: ignore[method-assign]


def release_ctp_td(gateway: Any, hard: bool | None = None) -> bool:
    """Yield TD seat to the live process.

    Default is soft release (mark only): hard td_api.close() can segfault the
    native CTP API and restart the whole MD process. Set LIVE_MD_HARD_RELEASE_TD=1
    to force a hard close.
    """
    if hard is None:
        hard = env_flag("LIVE_MD_HARD_RELEASE_TD", False)
    if not hard:
        return True
    td_api = getattr(gateway, "td_api", None)
    if td_api is None:
        return False
    try:
        td_api.close()
        return True
    except Exception:
        try:
            td_api.exit()
            return True
        except Exception:
            return False
