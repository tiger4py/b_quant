#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build concept base JSON files under data/concept/base.

Outputs:
- data/concept/base/concepts.json
- data/concept/base/summary.json
- data/concept/base/by_concept/{concept_code}.json
"""

import argparse
import csv
import json
import re
import shutil
import sqlite3
import time
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config import THS_COOKIE

CONCEPT_DIR = ROOT_DIR / "data" / "concept"
BASE_DIR = CONCEPT_DIR / "base"
BY_CONCEPT_DIR = BASE_DIR / "by_concept"
DB_PATH = ROOT_DIR / "data" / "stock.db"
THS_URL = "http://q.10jqka.com.cn/gn/detail/order/asc/op/code/page/{page}/code/{code}/"


def _stock_code_with_prefix(code):
    if code.startswith("6"):
        return f"sh.{code}"
    if code.startswith(("0", "3")):
        return f"sz.{code}"
    if code.startswith(("4", "8", "9")):
        return f"bj.{code}"
    return code


def load_concepts_from_csv():
    concepts = {}
    for path in sorted(CONCEPT_DIR.glob("*/*.csv")):
        if not path.parent.name.isdigit():
            continue
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                code = (row.get("concept_code") or "").strip()
                name = (row.get("concept_name") or "").strip()
                trade_date = (row.get("trade_date") or "").strip()
                if not code or not name:
                    continue
                item = concepts.setdefault(
                    code,
                    {
                        "concept_code": code,
                        "concept_name": name,
                        "first_trade_date": trade_date,
                        "latest_trade_date": trade_date,
                    },
                )
                if trade_date:
                    item["first_trade_date"] = min(item["first_trade_date"], trade_date)
                    item["latest_trade_date"] = max(item["latest_trade_date"], trade_date)
    return [concepts[k] for k in sorted(concepts)]


def make_session():
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "http://q.10jqka.com.cn/",
        }
    )
    if THS_COOKIE:
        for part in THS_COOKIE.split(";"):
            part = part.strip()
            if "=" in part:
                key, value = part.split("=", 1)
                session.cookies.set(key.strip(), value.strip())
    return session


def parse_page(html):
    soup = BeautifulSoup(html, "lxml")
    stocks = []
    for tr in soup.select("table.m-table tr"):
        tds = tr.find_all("td")
        if len(tds) < 3:
            continue
        raw_code = tds[1].get_text(strip=True)
        name = tds[2].get_text(strip=True)
        if re.match(r"^\d{6}$", raw_code):
            stocks.append(
                {
                    "code": _stock_code_with_prefix(raw_code),
                    "raw_code": raw_code,
                    "name": name,
                }
            )

    total_pages = 1
    page_info = soup.find(class_="page_info")
    if page_info:
        match = re.search(r"\d+/(\d+)", page_info.get_text(strip=True))
        if match:
            total_pages = int(match.group(1))
    return stocks, total_pages


def fetch_page(session, concept_code, page, retries=2):
    url = THS_URL.format(page=page, code=concept_code)
    last_error = None
    for attempt in range(retries + 1):
        try:
            resp = session.get(url, timeout=15)
            resp.raise_for_status()
            resp.encoding = "gbk"
            return parse_page(resp.text)
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(str(last_error))


def fetch_constituents(concept, max_pages=None):
    session = make_session()
    code = concept["concept_code"]
    stocks, total_pages = fetch_page(session, code, 1)
    if max_pages:
        total_pages = min(total_pages, max_pages)
    for page in range(2, total_pages + 1):
        page_stocks, _ = fetch_page(session, code, page)
        stocks.extend(page_stocks)

    deduped = {}
    for stock in stocks:
        deduped[stock["code"]] = stock

    result = {
        **concept,
        "constituent_count": len(deduped),
        "constituents": sorted(deduped.values(), key=lambda x: x["code"]),
        "source": "ths_q_10jqka",
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "page_count": total_pages,
    }
    return result


def load_constituents_from_db(concept_code):
    if not DB_PATH.exists():
        return []

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        table_names = {
            row["name"]
            for row in conn.execute(
                "select name from sqlite_master where type='table'"
            ).fetchall()
        }
        if "stock_concept" not in table_names:
            return []

        has_stock_info = "stock_info" in table_names
        if has_stock_info:
            rows = conn.execute(
                """
                select sc.stock_code, si.name as stock_name
                from stock_concept sc
                left join stock_info si on si.code = sc.stock_code
                where sc.concept_code = ?
                order by sc.stock_code
                """,
                (concept_code,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                select stock_code, '' as stock_name
                from stock_concept
                where concept_code = ?
                order by stock_code
                """,
                (concept_code,),
            ).fetchall()

    stocks = []
    for row in rows:
        code = row["stock_code"]
        raw_code = code.split(".", 1)[1] if "." in code else code
        stocks.append(
            {
                "code": code,
                "raw_code": raw_code,
                "name": row["stock_name"] or "",
            }
        )
    return stocks


def build_from_db(concept):
    constituents = load_constituents_from_db(concept["concept_code"])
    if not constituents:
        return None
    return {
        **concept,
        "constituent_count": len(constituents),
        "constituents": constituents,
        "source": "local_stock_concept_db",
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "page_count": None,
    }


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def make_failure_result(concept, error):
    return {
        **concept,
        "constituent_count": 0,
        "constituents": [],
        "source": "ths_q_10jqka",
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "page_count": 0,
        "fetch_error": str(error),
    }


def build(max_workers=8, max_pages=None, refresh=False):
    concepts = load_concepts_from_csv()
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    if refresh and BY_CONCEPT_DIR.exists():
        shutil.rmtree(BY_CONCEPT_DIR)
    BY_CONCEPT_DIR.mkdir(parents=True, exist_ok=True)

    results = {}
    failures = []
    pending = []
    for concept in concepts:
        path = BY_CONCEPT_DIR / f"{concept['concept_code']}.json"
        if path.exists() and not refresh:
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
                results[concept["concept_code"]] = cached
                continue
            except Exception:
                pass
        pending.append(concept)

    print(f"concepts={len(concepts)} cached={len(results)} fetch={len(pending)}")
    still_pending = []
    for concept in pending:
        item = build_from_db(concept)
        if item:
            results[concept["concept_code"]] = item
            write_json(BY_CONCEPT_DIR / f"{concept['concept_code']}.json", item)
        else:
            still_pending.append(concept)
    if len(still_pending) != len(pending):
        print(f"loaded_from_db={len(pending) - len(still_pending)}")
    pending = still_pending

    if pending:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(fetch_constituents, concept, max_pages): concept
                for concept in pending
            }
            done_count = 0
            for future in as_completed(future_map):
                concept = future_map[future]
                done_count += 1
                code = concept["concept_code"]
                name = concept["concept_name"]
                try:
                    item = future.result()
                    results[code] = item
                    write_json(BY_CONCEPT_DIR / f"{code}.json", item)
                    print(f"[{done_count}/{len(pending)}] ok {code} {name} stocks={item['constituent_count']}")
                except Exception as exc:
                    failures.append({"concept_code": code, "concept_name": name, "error": str(exc)})
                    results[code] = make_failure_result(concept, exc)
                    write_json(BY_CONCEPT_DIR / f"{code}.json", results[code])
                    print(f"[{done_count}/{len(pending)}] fail {code} {name}: {exc}")

    concept_list = [results[c["concept_code"]] for c in concepts if c["concept_code"] in results]
    summary = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "concept_count": len(concepts),
        "concept_with_constituents": sum(1 for item in concept_list if item.get("constituent_count", 0) > 0),
        "failure_count": len(failures),
        "total_constituent_relations": sum(item.get("constituent_count", 0) for item in concept_list),
        "source": "data/concept CSV + ths_q_10jqka constituents",
        "failures": failures,
    }

    write_json(BASE_DIR / "concepts.json", concept_list)
    write_json(BASE_DIR / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description="Build concept base JSON files.")
    parser.add_argument("--workers", type=int, default=8, help="Concurrent fetch workers.")
    parser.add_argument("--max-pages", type=int, default=None, help="Limit THS pages per concept.")
    parser.add_argument("--refresh", action="store_true", help="Re-fetch existing concept JSON files.")
    args = parser.parse_args()
    build(max_workers=args.workers, max_pages=args.max_pages, refresh=args.refresh)


if __name__ == "__main__":
    main()
