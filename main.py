import logging
import os
import threading
import time
from datetime import datetime

from flask import Flask, jsonify, render_template, request
from sqlalchemy import create_engine, func
from sqlalchemy.orm import Session, sessionmaker

from config import DATABASE_URL, DOWNLOAD_DAYS
from script.update_daily import update_concepts as scheduled_update_concepts
from script.update_daily import update_stocks as scheduled_update_stocks
from logic.progress import get as get_progress
from models.stock import Base, StockInfo, StockDaily, Concept, StockConcept, ConceptDaily
from logic.baostock_download import BaoStockDownloader
from logic.akshare_download import AkShareDownloader

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

engine = create_engine(DATABASE_URL, echo=False)
SessionLocal = sessionmaker(bind=engine)
_scheduler_thread = None
_scheduler_lock = threading.Lock()
_last_scheduler_run_date = None


def get_session() -> Session:
    return SessionLocal()


Base.metadata.create_all(engine)


def _run_daily_update_job():
    logger.info("scheduled daily update started")
    try:
        scheduled_update_stocks()
    except Exception:
        logger.exception("scheduled stock update failed")

    try:
        scheduled_update_concepts()
    except Exception:
        logger.exception("scheduled concept update failed")
    logger.info("scheduled daily update finished")


def _scheduler_loop():
    global _last_scheduler_run_date
    logger.info("flask scheduler started: workdays 18:00 update_daily")

    while True:
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        should_run = (
            now.weekday() < 5
            and now.hour == 18
            and now.minute == 0
            and _last_scheduler_run_date != today
        )

        if should_run:
            _last_scheduler_run_date = today
            _run_daily_update_job()

        time.sleep(30)


def start_scheduler():
    global _scheduler_thread
    with _scheduler_lock:
        if _scheduler_thread and _scheduler_thread.is_alive():
            return
        _scheduler_thread = threading.Thread(
            target=_scheduler_loop,
            name="daily-update-scheduler",
            daemon=True,
        )
        _scheduler_thread.start()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/stocks")
def page_stocks():
    return render_template("stocks.html")


@app.route("/concepts")
def page_concepts():
    return render_template("concepts.html")


# ── stock basic info ───────────────────────────────────────────
@app.route("/api/stocks")
def api_stocks():
    q = request.args.get("q", "")
    page = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 30))
    with get_session() as sess:
        base = sess.query(StockInfo).filter(StockInfo.type == "1", StockInfo.status == 1)
        if q:
            base = base.filter(
                StockInfo.code.contains(q) | StockInfo.name.contains(q)
            )
        total = base.count()
        rows = base.order_by(StockInfo.code).offset((page - 1) * per_page).limit(per_page).all()
        return jsonify({
            "total": total,
            "page": page,
            "per_page": per_page,
            "items": [{
                "code": s.code, "name": s.name, "market": s.market,
                "ipo_date": s.ipo_date,
            } for s in rows],
        })


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


@app.route("/api/concepts/<concept_code>/daily")
def api_concept_daily(concept_code: str):
    with get_session() as sess:
        rows = (
            sess.query(ConceptDaily)
            .filter(ConceptDaily.concept_code == concept_code)
            .order_by(ConceptDaily.trade_date.desc())
            .limit(1000)
            .all()
        )
        return jsonify([{
            "trade_date": r.trade_date,
            "open": r.open, "high": r.high, "low": r.low, "close": r.close,
            "volume": r.volume, "amount": r.amount,
        } for r in reversed(rows)])


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
        stock_count = sess.query(func.count(StockInfo.code)).filter(StockInfo.type == "1", StockInfo.status == 1).scalar()
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


# ── download progress ──────────────────────────────────────────
@app.route("/api/download/progress")
def api_progress():
    return jsonify(get_progress())


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


@app.route("/api/download/concept_daily", methods=["POST"])
def download_concept_daily():
    days = request.json.get("days", DOWNLOAD_DAYS) if request.is_json else DOWNLOAD_DAYS
    with get_session() as sess:
        dl = AkShareDownloader(sess)
        n = dl.download_concept_daily(days=days)
        return jsonify({"status": "ok", "rows": n})


if __name__ == "__main__":
    debug = True
    # Flask debug reload starts the app twice; only start the scheduler in the serving process.
    if not debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        start_scheduler()
    app.run(host="0.0.0.0", port=8000, debug=debug)
