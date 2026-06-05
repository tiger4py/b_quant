from sqlalchemy import Column, String, Float, Integer, BigInteger, Date, Index, Text
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class StockInfo(Base):
    """A股股票基本信息"""
    __tablename__ = "stock_info"

    code = Column(String(16), primary_key=True, comment="股票代码 (sh.600000)")
    name = Column(String(32), nullable=False, comment="股票名称")
    market = Column(String(8), comment="市场 (sh/sz)")
    ipo_date = Column(String(10), comment="上市日期")
    type = Column(String(8), comment="证券类型: 1-股票, 2-ETF, etc.")
    out_shares = Column(Float, comment="总股本(万股)")
    circ_shares = Column(Float, comment="流通股本(万股)")
    status = Column(Integer, default=1, comment="状态: 1-正常, 0-退市")


class StockDaily(Base):
    """A股日K线数据"""
    __tablename__ = "stock_daily"
    __table_args__ = (
        Index("ix_stock_daily_code_date", "code", "trade_date", unique=True),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(16), nullable=False, comment="股票代码")
    trade_date = Column(String(10), nullable=False, comment="交易日期 YYYY-MM-DD")
    open = Column(Float, comment="开盘价")
    high = Column(Float, comment="最高价")
    low = Column(Float, comment="最低价")
    close = Column(Float, comment="收盘价")
    volume = Column(BigInteger, comment="成交量(股)")
    amount = Column(Float, comment="成交额(元)")
    turn = Column(Float, comment="换手率(%)")
    pe_ttm = Column(Float, comment="市盈率(TTM)")


class BacktestCache(Base):
    __tablename__ = "backtest_cache"

    cache_key = Column(String(64), primary_key=True)
    strategy_id = Column(String(64), nullable=False)
    name = Column(String(128), nullable=False)
    days = Column(Integer, nullable=False)
    stock_count = Column(Integer, nullable=False)
    latest_trade_date = Column(String(10))
    created_at = Column(String(19), nullable=False)
    result_json = Column(Text, nullable=False)
    turn = Column(Float, comment="换手率(%)")
    pe_ttm = Column(Float, comment="市盈率(TTM)")


class Concept(Base):
    """同花顺概念板块"""
    __tablename__ = "concept"

    code = Column(String(32), primary_key=True, comment="概念代码")
    name = Column(String(64), nullable=False, comment="概念名称")


class StockConcept(Base):
    """股票-概念关联"""
    __tablename__ = "stock_concept"
    __table_args__ = (
        Index("ix_stock_concept_code", "stock_code", "concept_code", unique=True),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    stock_code = Column(String(16), nullable=False, comment="股票代码")
    concept_code = Column(String(32), nullable=False, comment="概念代码")


class ConceptDaily(Base):
    """概念指数日K线"""
    __tablename__ = "concept_daily"
    __table_args__ = (
        Index("ix_concept_daily_code_date", "concept_code", "trade_date", unique=True),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    concept_code = Column(String(32), nullable=False, comment="概念代码")
    trade_date = Column(String(10), nullable=False, comment="交易日期")
    open = Column(Float, comment="开盘指数")
    high = Column(Float, comment="最高指数")
    low = Column(Float, comment="最低指数")
    close = Column(Float, comment="收盘指数")
    volume = Column(BigInteger, comment="成交量(手)")
    amount = Column(Float, comment="成交额(元)")
