"""后台下载同花顺概念成分股（绕过HTTP超时）"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging

from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker

from config import DATABASE_URL
from logic.akshare_download import AkShareDownloader
from models.stock import Concept, StockConcept

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

engine = create_engine(DATABASE_URL, echo=False)
Session = sessionmaker(bind=engine)

if __name__ == "__main__":
    with Session() as sess:
        # 检查哪些概念还没有成分股数据
        all_codes = [r[0] for r in sess.query(Concept.code).all()]
        has_data = set(r[0] for r in sess.query(StockConcept.concept_code)
                       .filter(StockConcept.concept_code.in_(all_codes))
                       .distinct().all())

        need = [c for c in all_codes if c not in has_data]
        if not need:
            logger.info("All %d concepts have data, nothing to download", len(all_codes))
        else:
            logger.info("%d/%d concepts need data, downloading...", len(need), len(all_codes))
            dl = AkShareDownloader(sess)
            c, r = dl.download_concepts(codes_need=need)
            logger.info("DONE: %d new concepts, %d stock-concept relations", c, r)
