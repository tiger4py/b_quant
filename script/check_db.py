import sys
sys.path.insert(0, 'd:/my_import/sync_content/code/b_quant')
from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker
from config import DATABASE_URL
from models.stock import Concept, StockConcept, ConceptDaily, StockDaily, StockInfo

engine = create_engine(DATABASE_URL, echo=False)
s = sessionmaker(bind=engine)()

cc = s.query(func.count(Concept.code)).scalar()
rc = s.query(func.count(StockConcept.id)).scalar()
dc = s.query(func.count(ConceptDaily.id)).scalar()
cd_latest = s.query(func.max(ConceptDaily.trade_date)).scalar()
sd_latest = s.query(func.max(StockDaily.trade_date)).scalar()
si_count = s.query(func.count(StockInfo.code)).filter(StockInfo.type=='1', StockInfo.status==1).scalar()
sd_count = s.query(func.count(StockDaily.id)).filter(StockDaily.trade_date==sd_latest).scalar()

print(f'StockInfo(活跃): {si_count}')
print(f'StockDaily 最新日期: {sd_latest}, 当天: {sd_count}')
print(f'Concept: {cc}')
print(f'StockConcept: {rc}')
print(f'ConceptDaily: {dc}, 最新: {cd_latest}')
s.close()
