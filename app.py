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
import time
from datetime import date

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TODAY = date.today()
HEADERS = {"User-Agent": "Mozilla/5.0"}

app = Flask(__name__, static_folder=BASE_DIR, static_url_path="")


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
    d = parse_deadline(s)
    return d is not None and d < TODAY


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
            p["content"] = re.sub(r"\s{2,}", " ", content)[:600]
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
    try:
        data = collect_kicpa_cpa()
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8642))
    print(f"로컬 테스트: http://localhost:{port}")
    app.run(host="0.0.0.0", port=port)
