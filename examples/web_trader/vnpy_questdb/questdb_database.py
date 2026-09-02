from collections.abc import Iterator
from datetime import datetime, timezone
from time import monotonic, sleep
from typing import Any, TypeAlias

import psycopg
from psycopg.rows import DictRow, dict_row
from questdb.ingress import Sender

from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.database import (
    BaseDatabase,
    BarOverview,
    DB_TZ,
    TickOverview,
    convert_tz,
)
from vnpy.trader.object import BarData, TickData
from vnpy.trader.setting import SETTINGS


BAR_TABLE: str = "dbbardata"
TICK_TABLE: str = "dbtickdata"
FETCH_SIZE: int = 10_000
WAL_APPLY_TIMEOUT: float = 30

SqlValue: TypeAlias = str | int | float | bool | datetime | None
SqlParams: TypeAlias = tuple[SqlValue, ...]
IlpColumns: TypeAlias = dict[str, SqlValue]
RowTuple: TypeAlias = tuple[Any, ...]

CREATE_BAR_TABLE_SQL: str = f"""
CREATE TABLE IF NOT EXISTS {BAR_TABLE} (
    symbol SYMBOL CAPACITY 256 CACHE,
    exchange SYMBOL CAPACITY 32 CACHE,
    interval SYMBOL CAPACITY 16 CACHE,
    datetime TIMESTAMP,
    volume DOUBLE,
    turnover DOUBLE,
    open_interest DOUBLE,
    open_price DOUBLE,
    high_price DOUBLE,
    low_price DOUBLE,
    close_price DOUBLE,
    deleted BOOLEAN
) TIMESTAMP(datetime)
PARTITION BY MONTH
WAL
DEDUP UPSERT KEYS(datetime, symbol, exchange, interval);
"""

CREATE_TICK_TABLE_SQL: str = f"""
CREATE TABLE IF NOT EXISTS {TICK_TABLE} (
    symbol SYMBOL CAPACITY 256 CACHE,
    exchange SYMBOL CAPACITY 32 CACHE,
    datetime TIMESTAMP,
    name STRING,
    volume DOUBLE,
    turnover DOUBLE,
    open_interest DOUBLE,
    last_price DOUBLE,
    last_volume DOUBLE,
    limit_up DOUBLE,
    limit_down DOUBLE,
    open_price DOUBLE,
    high_price DOUBLE,
    low_price DOUBLE,
    pre_close DOUBLE,
    bid_price_1 DOUBLE,
    bid_price_2 DOUBLE,
    bid_price_3 DOUBLE,
    bid_price_4 DOUBLE,
    bid_price_5 DOUBLE,
    ask_price_1 DOUBLE,
    ask_price_2 DOUBLE,
    ask_price_3 DOUBLE,
    ask_price_4 DOUBLE,
    ask_price_5 DOUBLE,
    bid_volume_1 DOUBLE,
    bid_volume_2 DOUBLE,
    bid_volume_3 DOUBLE,
    bid_volume_4 DOUBLE,
    bid_volume_5 DOUBLE,
    ask_volume_1 DOUBLE,
    ask_volume_2 DOUBLE,
    ask_volume_3 DOUBLE,
    ask_volume_4 DOUBLE,
    ask_volume_5 DOUBLE,
    localtime TIMESTAMP,
    deleted BOOLEAN
) TIMESTAMP(datetime)
PARTITION BY DAY
WAL
DEDUP UPSERT KEYS(datetime, symbol, exchange);
"""

LOAD_BAR_DATA_SQL: str = f"""
    SELECT
        datetime,
        volume,
        turnover,
        open_interest,
        open_price,
        high_price,
        low_price,
        close_price
    FROM {BAR_TABLE}
    WHERE symbol = %s
        AND exchange = %s
        AND interval = %s
        AND datetime >= %s
        AND datetime <= %s
        AND deleted = false
    ORDER BY datetime;
"""

LOAD_TICK_DATA_SQL: str = f"""
    SELECT
        datetime,
        name,
        volume,
        turnover,
        open_interest,
        last_price,
        last_volume,
        limit_up,
        limit_down,
        open_price,
        high_price,
        low_price,
        pre_close,
        bid_price_1,
        bid_price_2,
        bid_price_3,
        bid_price_4,
        bid_price_5,
        ask_price_1,
        ask_price_2,
        ask_price_3,
        ask_price_4,
        ask_price_5,
        bid_volume_1,
        bid_volume_2,
        bid_volume_3,
        bid_volume_4,
        bid_volume_5,
        ask_volume_1,
        ask_volume_2,
        ask_volume_3,
        ask_volume_4,
        ask_volume_5,
        localtime
    FROM {TICK_TABLE}
    WHERE symbol = %s
        AND exchange = %s
        AND datetime >= %s
        AND datetime <= %s
        AND deleted = false
    ORDER BY datetime;
"""

COUNT_BAR_DATA_SQL: str = f"""
    SELECT count() AS count
    FROM {BAR_TABLE}
    WHERE symbol = %s
        AND exchange = %s
        AND interval = %s
        AND deleted = false;
"""

SOFT_DELETE_BAR_DATA_SQL: str = f"""
    UPDATE {BAR_TABLE}
    SET deleted = true
    WHERE symbol = %s
        AND exchange = %s
        AND interval = %s
        AND deleted = false;
"""

COUNT_TICK_DATA_SQL: str = f"""
    SELECT count() AS count
    FROM {TICK_TABLE}
    WHERE symbol = %s
        AND exchange = %s
        AND deleted = false;
"""

SOFT_DELETE_TICK_DATA_SQL: str = f"""
    UPDATE {TICK_TABLE}
    SET deleted = true
    WHERE symbol = %s
        AND exchange = %s
        AND deleted = false;
"""

GET_BAR_OVERVIEW_SQL: str = f"""
    SELECT
        symbol,
        exchange,
        interval,
        count() AS count,
        min(datetime) AS start_datetime,
        max(datetime) AS end_datetime
    FROM {BAR_TABLE}
    WHERE deleted = false
    GROUP BY symbol, exchange, interval
    ORDER BY symbol, exchange, interval;
"""

GET_TICK_OVERVIEW_SQL: str = f"""
    SELECT
        symbol,
        exchange,
        count() AS count,
        min(datetime) AS start_datetime,
        max(datetime) AS end_datetime
    FROM {TICK_TABLE}
    WHERE deleted = false
    GROUP BY symbol, exchange
    ORDER BY symbol, exchange;
"""

WAL_TABLE_STATUS_SQL: str = """
    SELECT
        suspended,
        writerTxn,
        sequencerTxn,
        errorMessage
    FROM wal_tables()
    WHERE name = %s;
"""


class QuestdbDatabase(BaseDatabase):
    """QuestDB database adapter for VeighNa."""

    def __init__(self) -> None:
        self.host: str = str(SETTINGS.get("database.host") or "localhost")
        self.port: int = int(SETTINGS.get("database.port") or 8812)
        self.user: str = str(SETTINGS.get("database.user") or "admin")
        self.password: str = str(SETTINGS.get("database.password") or "quest")
        self.database: str = str(SETTINGS.get("database.database") or "qdb")
        self.http_port: int = int(SETTINGS.get("database.http_port") or 9000)

        self.conninfo: str = (
            f"host={self.host} "
            f"port={self.port} "
            f"user={self.user} "
            f"password={self.password} "
            f"dbname={self.database}"
        )
        self.ilp_conf: str = f"http::addr={self.host}:{self.http_port};"

        self.init_tables()

    def init_tables(self) -> None:
        with psycopg.connect(self.conninfo, autocommit=True) as conn:
            with conn.cursor() as cursor:
                cursor.execute(CREATE_BAR_TABLE_SQL)
                cursor.execute(CREATE_TICK_TABLE_SQL)

    def save_bar_data(self, bars: list[BarData], stream: bool = False) -> bool:
        if not bars:
            return True

        with Sender.from_conf(self.ilp_conf) as sender:
            for bar in bars:
                interval: Interval | None = bar.interval
                if interval is None:
                    raise ValueError("BarData.interval不能为空")

                sender.row(
                    BAR_TABLE,
                    symbols={
                        "symbol": bar.symbol,
                        "exchange": bar.exchange.value,
                        "interval": interval.value,
                    },
                    columns={
                        "volume": bar.volume,
                        "turnover": bar.turnover,
                        "open_interest": bar.open_interest,
                        "open_price": bar.open_price,
                        "high_price": bar.high_price,
                        "low_price": bar.low_price,
                        "close_price": bar.close_price,
                        "deleted": False,
                    },
                    at=self._to_questdb_datetime(bar.datetime),
                )
            sender.flush()

        self._wait_wal_apply(BAR_TABLE)
        return True

    def save_tick_data(self, ticks: list[TickData], stream: bool = False) -> bool:
        if not ticks:
            return True

        with Sender.from_conf(self.ilp_conf) as sender:
            for tick in ticks:
                columns: IlpColumns = {
                    "name": tick.name,
                    "volume": tick.volume,
                    "turnover": tick.turnover,
                    "open_interest": tick.open_interest,
                    "last_price": tick.last_price,
                    "last_volume": tick.last_volume,
                    "limit_up": tick.limit_up,
                    "limit_down": tick.limit_down,
                    "open_price": tick.open_price,
                    "high_price": tick.high_price,
                    "low_price": tick.low_price,
                    "pre_close": tick.pre_close,
                    "bid_price_1": tick.bid_price_1,
                    "bid_price_2": tick.bid_price_2,
                    "bid_price_3": tick.bid_price_3,
                    "bid_price_4": tick.bid_price_4,
                    "bid_price_5": tick.bid_price_5,
                    "ask_price_1": tick.ask_price_1,
                    "ask_price_2": tick.ask_price_2,
                    "ask_price_3": tick.ask_price_3,
                    "ask_price_4": tick.ask_price_4,
                    "ask_price_5": tick.ask_price_5,
                    "bid_volume_1": tick.bid_volume_1,
                    "bid_volume_2": tick.bid_volume_2,
                    "bid_volume_3": tick.bid_volume_3,
                    "bid_volume_4": tick.bid_volume_4,
                    "bid_volume_5": tick.bid_volume_5,
                    "ask_volume_1": tick.ask_volume_1,
                    "ask_volume_2": tick.ask_volume_2,
                    "ask_volume_3": tick.ask_volume_3,
                    "ask_volume_4": tick.ask_volume_4,
                    "ask_volume_5": tick.ask_volume_5,
                    "deleted": False,
                }
                if tick.localtime:
                    columns["localtime"] = self._to_questdb_datetime(tick.localtime)

                sender.row(
                    TICK_TABLE,
                    symbols={
                        "symbol": tick.symbol,
                        "exchange": tick.exchange.value,
                    },
                    columns=columns,
                    at=self._to_questdb_datetime(tick.datetime),
                )
            sender.flush()

        self._wait_wal_apply(TICK_TABLE)
        return True

    def load_bar_data(
        self,
        symbol: str,
        exchange: Exchange,
        interval: Interval,
        start: datetime,
        end: datetime,
    ) -> list[BarData]:
        params: SqlParams = (
            symbol,
            exchange.value,
            interval.value,
            self._to_pg_datetime(start),
            self._to_pg_datetime(end),
        )

        bars: list[BarData] = []
        append = bars.append
        from_datetime = self._from_questdb_datetime
        for row in self._iter_tuples(LOAD_BAR_DATA_SQL, params):
            append(
                BarData(
                    symbol=symbol,
                    exchange=exchange,
                    datetime=from_datetime(row[0]),
                    interval=interval,
                    volume=row[1],
                    turnover=row[2],
                    open_interest=row[3],
                    open_price=row[4],
                    high_price=row[5],
                    low_price=row[6],
                    close_price=row[7],
                    gateway_name="DB",
                )
            )
        return bars

    def load_tick_data(
        self,
        symbol: str,
        exchange: Exchange,
        start: datetime,
        end: datetime,
    ) -> list[TickData]:
        params: SqlParams = (
            symbol,
            exchange.value,
            self._to_pg_datetime(start),
            self._to_pg_datetime(end),
        )

        ticks: list[TickData] = []
        append = ticks.append
        from_datetime = self._from_questdb_datetime
        for row in self._iter_tuples(LOAD_TICK_DATA_SQL, params):
            localtime: datetime | None = None
            if row[33]:
                localtime = from_datetime(row[33])
            append(
                TickData(
                    symbol=symbol,
                    exchange=exchange,
                    datetime=from_datetime(row[0]),
                    name=row[1],
                    volume=row[2],
                    turnover=row[3],
                    open_interest=row[4],
                    last_price=row[5],
                    last_volume=row[6],
                    limit_up=row[7],
                    limit_down=row[8],
                    open_price=row[9],
                    high_price=row[10],
                    low_price=row[11],
                    pre_close=row[12],
                    bid_price_1=row[13],
                    bid_price_2=row[14],
                    bid_price_3=row[15],
                    bid_price_4=row[16],
                    bid_price_5=row[17],
                    ask_price_1=row[18],
                    ask_price_2=row[19],
                    ask_price_3=row[20],
                    ask_price_4=row[21],
                    ask_price_5=row[22],
                    bid_volume_1=row[23],
                    bid_volume_2=row[24],
                    bid_volume_3=row[25],
                    bid_volume_4=row[26],
                    bid_volume_5=row[27],
                    ask_volume_1=row[28],
                    ask_volume_2=row[29],
                    ask_volume_3=row[30],
                    ask_volume_4=row[31],
                    ask_volume_5=row[32],
                    localtime=localtime,
                    gateway_name="DB",
                )
            )
        return ticks

    def delete_bar_data(self, symbol: str, exchange: Exchange, interval: Interval) -> int:
        params: SqlParams = (symbol, exchange.value, interval.value)
        count: int = self._query_count(COUNT_BAR_DATA_SQL, params)
        self._execute(SOFT_DELETE_BAR_DATA_SQL, params)
        self._wait_wal_apply(BAR_TABLE)
        return count

    def delete_tick_data(self, symbol: str, exchange: Exchange) -> int:
        params: SqlParams = (symbol, exchange.value)
        count: int = self._query_count(COUNT_TICK_DATA_SQL, params)
        self._execute(SOFT_DELETE_TICK_DATA_SQL, params)
        self._wait_wal_apply(TICK_TABLE)
        return count

    def get_bar_overview(self) -> list[BarOverview]:
        overviews: list[BarOverview] = []
        for row in self._iter_rows(GET_BAR_OVERVIEW_SQL):
            overviews.append(
                BarOverview(
                    symbol=row["symbol"],
                    exchange=Exchange(row["exchange"]),
                    interval=Interval(row["interval"]),
                    count=int(row["count"]),
                    start=self._from_questdb_datetime(row["start_datetime"]),
                    end=self._from_questdb_datetime(row["end_datetime"]),
                )
            )
        return overviews

    def get_tick_overview(self) -> list[TickOverview]:
        overviews: list[TickOverview] = []
        for row in self._iter_rows(GET_TICK_OVERVIEW_SQL):
            overviews.append(
                TickOverview(
                    symbol=row["symbol"],
                    exchange=Exchange(row["exchange"]),
                    count=int(row["count"]),
                    start=self._from_questdb_datetime(row["start_datetime"]),
                    end=self._from_questdb_datetime(row["end_datetime"]),
                )
            )
        return overviews

    def _iter_rows(self, sql: str, params: SqlParams | None = None) -> Iterator[DictRow]:
        with psycopg.connect(self.conninfo, row_factory=dict_row) as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql, params)
                while batch := cursor.fetchmany(FETCH_SIZE):
                    yield from batch

    def _iter_tuples(self, sql: str, params: SqlParams | None = None) -> Iterator[RowTuple]:
        with psycopg.connect(self.conninfo) as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql, params)
                while batch := cursor.fetchmany(FETCH_SIZE):
                    yield from batch

    def _query_count(self, sql: str, params: SqlParams) -> int:
        with psycopg.connect(self.conninfo, row_factory=dict_row) as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql, params)
                row: DictRow | None = cursor.fetchone()
                if not row:
                    return 0
                return int(row["count"])

    def _execute(self, sql: str, params: SqlParams) -> None:
        with psycopg.connect(self.conninfo, autocommit=True) as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql, params)

    def _wait_wal_apply(self, table_name: str) -> None:
        if WAL_APPLY_TIMEOUT <= 0:
            return

        deadline: float = monotonic() + WAL_APPLY_TIMEOUT
        while True:
            with psycopg.connect(self.conninfo, row_factory=dict_row) as conn:
                with conn.cursor() as cursor:
                    cursor.execute(WAL_TABLE_STATUS_SQL, (table_name,))
                    row: DictRow | None = cursor.fetchone()

            if not row:
                return
            if row["suspended"]:
                raise RuntimeError(f"QuestDB WAL表{table_name}已暂停: {row['errorMessage']}")
            if row["writerTxn"] == row["sequencerTxn"]:
                return
            if monotonic() >= deadline:
                raise TimeoutError(f"等待QuestDB WAL表{table_name}应用超时")
            sleep(0.05)

    @staticmethod
    def _to_questdb_datetime(dt: datetime) -> datetime:
        db_dt: datetime = convert_tz(dt).replace(tzinfo=DB_TZ)
        return db_dt.astimezone(timezone.utc)

    @classmethod
    def _to_pg_datetime(cls, dt: datetime) -> datetime:
        return cls._to_questdb_datetime(dt).replace(tzinfo=None)

    @staticmethod
    def _from_questdb_datetime(dt: datetime) -> datetime:
        if dt.tzinfo:
            return dt.astimezone(DB_TZ)
        return dt.replace(tzinfo=timezone.utc).astimezone(DB_TZ)
