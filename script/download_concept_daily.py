"""后台下载概念指数日K线"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging
from datetime import datetime

from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker

from config import DATABASE_URL, DOWNLOAD_DAYS
from logic.akshare_download import AkShareDownloader
from models.stock import Concept, ConceptDaily

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

engine = create_engine(DATABASE_URL, echo=False)
Session = sessionmaker(bind=engine)

if __name__ == "__main__":
    today = datetime.now().strftime("%Y-%m-%d")

    with Session() as sess:
        all_concepts = sess.query(Concept).all()

        # 检查哪些概念已有最近日线
        all_codes = [c.code for c in all_concepts]
        recent = dict(sess.query(
            ConceptDaily.concept_code, func.max(ConceptDaily.trade_date)
        ).filter(
            ConceptDaily.concept_code.in_(all_codes)
        ).group_by(ConceptDaily.concept_code).all())

        need = [c for c in all_concepts if c.code not in recent or recent[c.code] < today]
        skipped = len(all_concepts) - len(need)
        if skipped:
            logger.info("Skipping %d concepts with up-to-date data, %d remaining", skipped, len(need))

        if not need:
            logger.info("All concepts up to date, nothing to download")
        else:
            # Temporarily replace all concepts with only the needed ones
            dl = AkShareDownloader(sess)
            n = dl.download_concept_daily(days=DOWNLOAD_DAYS, concepts=need)
            logger.info("DONE: %d concept daily records", n)
