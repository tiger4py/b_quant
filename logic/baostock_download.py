import logging
from datetime import datetime, timedelta

import baostock as bs
from sqlalchemy.orm import Session

from config import K_FIELDS, K_FREQUENCY
from models.stock import StockInfo, StockDaily

logger = logging.getLogger(__name__)


class BaoStockDownloader:

    def __init__(self, session: Session, days: int):
        self.session = session
        self.days = days

    @staticmethod
    def _login():
        lg = bs.login()
        if lg.error_code != "0":
            raise RuntimeError(f"baostock login failed: {lg.error_msg}")

    @staticmethod
    def _logout():
        bs.logout()

    def download_stock_basic(self) -> int:
        """下载股票基本信息，返回新增数量"""
        self._login()
        try:
            rs = bs.query_stock_basic()
            count = 0
            while rs.next():
                row = rs.get_row_data()
                code = row[0]
                if self.session.get(StockInfo, code):
                    continue
                # baostock query_stock_basic returns: code, name, ipoDate, outDate, type, status
                self.session.add(StockInfo(
                    code=code,
                    name=row[1],
                    market="sh" if code.startswith("sh") else "sz",
                    ipo_date=row[2] if row[2] else None,
                    type=row[4] if row[4] else None,
                    status=1 if row[5] == "1" else 0,
                ))
                count += 1
            self.session.commit()
            return count
        finally:
            self._logout()

    def download_daily_k(self, code: str = None) -> int:
        """下载日K线数据，返回插入/更新数量"""
        end_date = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=self.days * 2)).strftime("%Y-%m-%d")

        codes = [code] if code else [
            r[0] for r in self.session.query(StockInfo.code)
            .filter(StockInfo.status == 1).all()
        ]

        self._login()
        count = 0
        try:
            for code in codes:
                rs = bs.query_history_k_data_plus(
                    code, K_FIELDS,
                    start_date=start_date, end_date=end_date,
                    frequency=K_FREQUENCY, adjustflag="3"
                )
                if rs.error_code != "0":
                    logger.warning("query %s error: %s", code, rs.error_msg)
                    continue

                rows = []
                while rs.next():
                    rows.append(rs.get_row_data())

                if not rows:
                    continue

                # Drop the oldest tradedays so we keep exactly self.days trading days
                rows = rows[-self.days:] if len(rows) > self.days else rows

                for row in rows:
                    trade_date = row[0]
                    existing = self.session.query(StockDaily).filter_by(
                        code=code, trade_date=trade_date,
                    ).first()
                    if existing:
                        existing.open = float(row[1]) if row[1] else None
                        existing.high = float(row[2]) if row[2] else None
                        existing.low = float(row[3]) if row[3] else None
                        existing.close = float(row[4]) if row[4] else None
                        existing.volume = int(row[5]) if row[5] else None
                        existing.amount = float(row[6]) if row[6] else None
                        existing.turn = float(row[7]) if row[7] else None
                        existing.pe_ttm = float(row[8]) if row[8] else None
                    else:
                        self.session.add(StockDaily(
                            code=code,
                            trade_date=trade_date,
                            open=float(row[1]) if row[1] else None,
                            high=float(row[2]) if row[2] else None,
                            low=float(row[3]) if row[3] else None,
                            close=float(row[4]) if row[4] else None,
                            volume=int(row[5]) if row[5] else None,
                            amount=float(row[6]) if row[6] else None,
                            turn=float(row[7]) if row[7] else None,
                            pe_ttm=float(row[8]) if row[8] else None,
                        ))
                    count += 1

                self.session.commit()
                logger.info("downloaded %s: %d rows", code, len(rows))
        finally:
            self._logout()

        return count
