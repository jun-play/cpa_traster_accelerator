#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
웹 호스팅용 앱 (Render/Railway 등에 그대로 배포 가능)

로컬 버전(server.py)과 하는 일은 동일함: index.html을 서빙하고,
/api/kicpa 요청이 오면 KICPA를 실시간으로 긁어서 반환.
차이는 실행 방식뿐 — 이건 로컬이 아니라 인터넷에 상시 떠 있는 서버에서 돈다는 전제로
gunicorn(운영용 서버 프로그램)이 이 파일의 `app` 객체를 직접 구동함.

로컬에서 테스트하려면:
    pip install -r requirements.txt
    python app.py
    브라우저에서 http://localhost:8642
"""

import os
import re
import threading
import time
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HEADERS = {"User-Agent": "Mozilla/5.0"}
CACHE_TTL_SECONDS = 30 * 60  # 30분 — 이 시간 안에는 다시 안 긁고 캐시를 씀

app = Flask(__name__, static_folder=BASE_DIR, static_url_path="")

# ---------------------------------------------------------------------------
# 여러 사람이 동시에 써도 안전하게: 메모리 캐시 + 락
# 락(lock)이 하는 일: 캐시가 오래돼서 다시 긁어야 할 때, 여러 명이 동시에
# 몰려도 실제로 KICPA를 긁는 건 1명(첫 요청)만 하고, 나머지는 그 결과를
# 그냥 같이 받아감. 이게 없으면 5명이 동시에 눌렀을 때 KICPA를 5번 긁게 됨.
# ---------------------------------------------------------------------------
_cache = {"data": None, "fetched_at": 0}
_cache_lock = threading.Lock()


def parse_deadline(s: str):
    if not s:
        return None
    m = re.search(r"(\d{4})\.(\d{2})\.(\d{2})", s)
    if not m:
        return None
    y, mo, d = map(int, m.groups())
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def is_expired(s: str) -> bool:
    """호출되는 그 순간의 날짜(date.today())를 기준으로 판단.
    서버가 며칠씩 켜져 있어도 매번 '지금' 기준으로 계산됨 — 서버 시작 시점에
    고정되지 않도록 하는 게 핵심."""
    d = parse_deadline(s)
    return d is not None and d < date.today()


def collect_kicpa_cpa():
    s = requests.Session()
    s.headers.update(HEADERS)
    s.get("https://www.kicpa.or.kr/portal/default/kicpa/gnb/kr_pc/menu05/menu09/menu01.page")

    resp = s.post("https://www.kicpa.or.kr/home/jobOffrSrchGnrl/list.face",
                   data={"ijJobSep": "1", "listCnt": "300"})
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "html.parser")

    rows = soup.select("table tbody tr")
    postings = []
    for tr in rows:
        tds = tr.find_all("td")
        if len(tds) < 6:
            continue
        a = tds[1].find("a")
        title = a.get_text(strip=True) if a else tds[1].get_text(strip=True)
        onclick = a.get("onclick", "") if a else ""
        bltn_no = ""
        if "fn_detail(" in onclick:
            bltn_no = onclick.split("fn_detail('")[1].split("')")[0]
        if not bltn_no:
            continue
        postings.append({
            "bltn_no": bltn_no,
            "title": title,
            "company": tds[2].get_text(strip=True),
            "region": tds[3].get_text(" ", strip=True),
        })

    for p in postings:
        p["url"] = f"https://www.kicpa.or.kr/home/jobOffrSrchGnrl/detail.face?ijIdNum={p['bltn_no']}"
        try:
            r = s.post("https://www.kicpa.or.kr/home/jobOffrSrchGnrl/detail.face",
                        data={"ijIdNum": p["bltn_no"]})
            r.encoding = "utf-8"
            soup2 = BeautifulSoup(r.text, "html.parser")
            field_map = {}
            for th in soup2.find_all("th"):
                td = th.find_next_sibling("td")
                field_map[th.get_text(strip=True)] = td.get_text(" ", strip=True) if td else ""
            pre = soup2.find("pre", class_="txt_infor")
            p["deadline"] = field_map.get("마감일", "")
            p["experience"] = field_map.get("경력", "")
            p["company_type"] = field_map.get("회사구분", "")
            content = pre.get_text(" ", strip=True) if pre else ""
            p["content"] = re.sub(r"\s{2,}", " ", content)  # 상세보기가 스크롤되는 창이라 잘라낼 필요 없음 — 전체 원문 그대로
        except Exception:
            p["deadline"] = ""
            p["experience"] = ""
            p["company_type"] = ""
            p["content"] = ""
        time.sleep(0.12)

    return [p for p in postings if not is_expired(p.get("deadline", ""))]


@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/api/kicpa")
def api_kicpa():
    now = time.time()

    # 캐시가 신선하면(30분 이내) 즉시 반환 — 대부분의 요청은 여기서 끝남
    with _cache_lock:
        if _cache["data"] is not None and (now - _cache["fetched_at"]) < CACHE_TTL_SECONDS:
            age_min = int((now - _cache["fetched_at"]) / 60)
            return jsonify({
                "postings": _cache["data"],
                "cached": True,
                "fetched_minutes_ago": age_min,
            })

    # 캐시가 없거나 오래됐으면, 락을 잡은 사람만 실제로 긁음
    # (동시에 여러 명이 여기 도달해도 lock 덕분에 한 번만 실행됨)
    with _cache_lock:
        # 락을 기다리는 동안 다른 사람이 이미 갱신했을 수 있으니 재확인
        if _cache["data"] is not None and (time.time() - _cache["fetched_at"]) < CACHE_TTL_SECONDS:
            return jsonify({
                "postings": _cache["data"],
                "cached": True,
                "fetched_minutes_ago": int((time.time() - _cache["fetched_at"]) / 60),
            })
        try:
            data = collect_kicpa_cpa()
            _cache["data"] = data
            _cache["fetched_at"] = time.time()
            return jsonify({"postings": data, "cached": False, "fetched_minutes_ago": 0})
        except Exception as e:
            return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8642))
    print(f"로컬 테스트: http://localhost:{port}")
    app.run(host="0.0.0.0", port=port)
