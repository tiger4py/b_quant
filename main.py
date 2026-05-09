import logging

from flask import Flask, jsonify, render_template, request
from sqlalchemy import create_engine, func
from sqlalchemy.orm import Session, sessionmaker

from config import DATABASE_URL, DOWNLOAD_DAYS
from models.stock import Base, StockInfo, StockDaily, Concept, StockConcept
from logic.baostock_download import BaoStockDownloader
from logic.akshare_download import AkShareDownloader

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

engine = create_engine(DATABASE_URL, echo=False)
SessionLocal = sessionmaker(bind=engine)


def get_session() -> Session:
    return SessionLocal()


Base.metadata.create_all(engine)


@app.route("/")
def index():
    return render_template("index.html")


# ── stock basic info ───────────────────────────────────────────
@app.route("/api/stocks")
def api_stocks():
    with get_session() as sess:
        stocks = sess.query(StockInfo).filter(StockInfo.status == 1).all()
        return jsonify([{
            "code": s.code, "name": s.name, "market": s.market,
            "ipo_date": s.ipo_date,
            "out_shares": s.out_shares, "circ_shares": s.circ_shares,
        } for s in stocks])


# ── daily K-line ───────────────────────────────────────────────
@app.route("/api/stocks/<code>/daily")
def api_stock_daily(code: str):
    with get_session() as sess:
        rows = (
            sess.query(StockDaily)
            .filter(StockDaily.code == code)
            .order_by(StockDaily.trade_date.desc())
            .limit(365)
            .all()
        )
        return jsonify([{
            "trade_date": r.trade_date,
            "open": r.open, "high": r.high, "low": r.low, "close": r.close,
            "volume": r.volume, "amount": r.amount,
            "turn": r.turn, "pe_ttm": r.pe_ttm,
        } for r in reversed(rows)])


# ── concepts ───────────────────────────────────────────────────
@app.route("/api/concepts")
def api_concepts():
    with get_session() as sess:
        concepts = sess.query(Concept).all()
        return jsonify([{"code": c.code, "name": c.name} for c in concepts])


@app.route("/api/concepts/<concept_code>/stocks")
def api_concept_stocks(concept_code: str):
    with get_session() as sess:
        concept = sess.get(Concept, concept_code)
        if not concept:
            return jsonify({"error": "concept not found"}), 404

        rows = (
            sess.query(StockConcept, StockInfo)
            .join(StockInfo, StockConcept.stock_code == StockInfo.code)
            .filter(StockConcept.concept_code == concept_code)
            .all()
        )
        return jsonify({
            "concept": {"code": concept.code, "name": concept.name},
            "stocks": [{
                "code": si.code, "name": si.name,
            } for _, si in rows],
        })


# ── stats ──────────────────────────────────────────────────────
@app.route("/api/stats")
def api_stats():
    with get_session() as sess:
        stock_count = sess.query(func.count(StockInfo.code)).scalar()
        daily_count = sess.query(func.count(StockDaily.id)).scalar()
        concept_count = sess.query(func.count(Concept.code)).scalar()
        rel_count = sess.query(func.count(StockConcept.id)).scalar()
        latest_date = sess.query(func.max(StockDaily.trade_date)).scalar()
        return jsonify({
            "stocks": stock_count,
            "daily_records": daily_count,
            "concepts": concept_count,
            "stock_concept_relations": rel_count,
            "latest_trade_date": latest_date,
        })


# ── download triggers ──────────────────────────────────────────
@app.route("/api/download/stocks", methods=["POST"])
def download_stocks():
    days = request.json.get("days", DOWNLOAD_DAYS) if request.is_json else DOWNLOAD_DAYS
    with get_session() as sess:
        dl = BaoStockDownloader(sess, days)
        n = dl.download_stock_basic()
        return jsonify({"status": "ok", "new_stocks": n})


@app.route("/api/download/daily", methods=["POST"])
def download_daily():
    days = request.json.get("days", DOWNLOAD_DAYS) if request.is_json else DOWNLOAD_DAYS
    code = request.json.get("code") if request.is_json else None
    with get_session() as sess:
        dl = BaoStockDownloader(sess, days)
        n = dl.download_daily_k(code=code)
        return jsonify({"status": "ok", "rows_upserted": n})


@app.route("/api/download/concepts", methods=["POST"])
def download_concepts():
    with get_session() as sess:
        dl = AkShareDownloader(sess)
        c, r = dl.download_concepts()
        return jsonify({"status": "ok", "new_concepts": c, "new_relations": r})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000,debug=True)