# -*- coding: utf-8 -*-
"""
네이버 항공권 주말 최저가 감시기 — 클라우드(브라우저 없는) 버전

서울(인천·김포) → 오사카(간사이) 2박 3일 왕복(금~일, 토~월)을 조회 가능한 날짜 전체로 조회해
가는 편·오는 편 출발 시각 조건, 피치항공 제외 조건에서 결제수단(네이버페이·우리카드 등)별 실제 결제가 기준
최저가 TOP N 을 메일로 보낸다. GitHub Actions 에서 하루 2번 실행하도록 만들었다.

네이버 항공권 사이트가 내부적으로 쓰는 검색 API(flight-api.naver.com, 비공식)를 직접 부른다.
  - 요청: POST /flight/international/searchFlights (SSE 스트림, 끝까지 읽고 마지막 이벤트 사용)
  - 운임: fareMappings[].fares[] 의 fareType 이 'A01'(성인/모든 결제수단) 또는 'A01/Bxx'(카드·간편결제 할인)
          이름은 status.fareTypesCodeMap 에 있음
  - 결제수단별 실제 결제가 = min(그 결제수단 할인 운임, 모든 결제수단 운임)
    (해당 결제수단 할인이 없는 항공권도 그 결제수단으로 일반 운임 결제는 가능하므로)

사용법
  python naver_api_watch.py                 # 전체 조회 + 메일
  python naver_api_watch.py --probe 2026-11-21 [--origin ICN]
                                            # 1건만 조회해 원본 응답과 해석 결과를 data/probe 에 저장
  python naver_api_watch.py --limit 4       # 앞 4개 일정만 시험 조회 + 메일
  python naver_api_watch.py --no-mail
  python naver_api_watch.py --test-mail
메일 설정은 환경변수(MAIL_USER, MAIL_APP_PASSWORD, MAIL_TO) 또는 config.json 의 mail 항목.
"""
import argparse
import csv
import datetime as dt
import html
import json
import os
import random
import re
import smtplib
import sys
import time
import traceback
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
CONFIG_PATH = BASE / "config.json"
KST = dt.timezone(dt.timedelta(hours=9))
WEEKDAY_KO = "월화수목금토일"
API_BASE = os.environ.get("NFW_API_BASE", "https://flight-api.naver.com")
SEARCH_PATH = "/flight/international/searchFlights"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
HOUR_SLOTS = ["00:00-06:00", "06:00-09:00", "09:00-12:00", "12:00-15:00",
              "15:00-18:00", "18:00-21:00", "21:00-24:00"]

DEFAULT_CONFIG = {
    "origins": ["ICN", "GMP"],
    "destination": "KIX",
    "patterns": ["fri_sun", "sat_mon"],
    "min_days_ahead": 1,
    "days_ahead": 400,
    "stop_after_empty_trips": 4,
    "nonstop_only": True,
    "outbound_depart_between": ["06:00", "12:00"],
    "return_depart_between": ["12:00", "18:00"],
    "exclude_airlines": ["피치", "Peach", "MM"],
    # 결제구분: 운임 이름(fareTypesCodeMap)에 아래 글자가 들어간 할인 운임을 그 결제수단 가격으로 봄
    "payment_groups": {
        "네이버페이": ["네이버페이", "네이버 페이", "Npay", "N pay"],
        "우리카드": ["우리카드"],
    },
    "show_general_fare": True,  # 표에 '모든 결제수단' 가격도 참고로 표시
    "top_n": 10,
    "adults": 1,
    "fare_limit": 100,          # 검색 1건에서 받을 운임 묶음 수
    "delay_sec": [2, 4],
    "search_timeout_sec": 60,
    "max_run_minutes": 100,
    # 자동 대응: 한 번 실행에서 검색 오류 비율이 이 값 이상이면
    #   1단계) auto_drop_origins 의 공항(김포)을 자동으로 빼고 메일로 알림
    #   2단계) 이미 뺀 뒤에도 오류가 계속되면 몇 시간씩 쉬었다가 다시 시도 (3→6→12→24시간)
    "auto_guard_error_ratio": 0.3,
    "auto_drop_origins": ["GMP"],
    "mail": {
        "enabled": True,
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
        "user": "",
        "app_password": "",
        "to": [],
        "only_when_changed": False,
    },
}


# ---------------------------------------------------------------- 공통
def now_kst():
    return dt.datetime.now(KST)


def log(msg):
    line = f"[{now_kst():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        DATA.mkdir(exist_ok=True)
        with open(DATA / "log.txt", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if CONFIG_PATH.exists():
        user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        mail = user.pop("mail", {})
        cfg.update(user)
        cfg["mail"].update(mail)
    m = cfg["mail"]  # 비밀값은 환경변수(GitHub Secrets)가 우선
    m["user"] = os.environ.get("MAIL_USER") or m["user"]
    m["app_password"] = os.environ.get("MAIL_APP_PASSWORD") or m["app_password"]
    to = os.environ.get("MAIL_TO")
    if to:
        m["to"] = [x.strip() for x in to.split(",") if x.strip()]
    if not m["to"] and m["user"]:
        m["to"] = [m["user"]]
    return cfg


def build_trips(cfg, today=None):
    today = today or now_kst().date()
    d = today + dt.timedelta(days=cfg["min_days_ahead"])
    end = today + dt.timedelta(days=cfg["days_ahead"])
    trips = []
    while d <= end:
        if d.weekday() == 4 and "fri_sun" in cfg["patterns"]:
            trips.append((d, d + dt.timedelta(days=2), "금~일"))
        if d.weekday() == 5 and "sat_mon" in cfg["patterns"]:
            trips.append((d, d + dt.timedelta(days=2), "토~월"))
        d += dt.timedelta(days=1)
    return trips


def hhmm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def slots_for(window):
    """['06:00','12:00'] -> 네이버 출발시각 칸 ['06:00-09:00','09:00-12:00'] (칸 전체가 범위 안인 것만)"""
    lo, hi = hhmm(window[0]), hhmm(window[1].replace("23:59", "24:00"))
    out = []
    for s in HOUR_SLOTS:
        a, b = s.split("-")
        if hhmm(a) >= lo and hhmm(b) <= hi:
            out.append(s)
    return [] if len(out) == len(HOUR_SLOTS) else out


def naver_url(cfg, origin, dep, ret):
    d = cfg["destination"]
    return (f"https://flight.naver.com/flights/international/{origin}-{d}-{dep:%Y%m%d}/"
            f"{d}-{origin}-{ret:%Y%m%d}?adult={cfg['adults']}&fareType=Y")


# ---------------------------------------------------------------- 네이버 API
def search_body(cfg, origin, dep, ret):
    d = cfg["destination"]
    leg = lambda a, b, day: {"departureLocationCode": a, "departureLocationType": "airport",
                             "arrivalLocationCode": b, "arrivalLocationType": "airport",
                             "departureDate": day.strftime("%Y%m%d")}
    return {
        "adultCount": cfg["adults"], "childCount": 0, "infantCount": 0,
        "device": "pc", "isNonstop": bool(cfg["nonstop_only"]), "seatClass": "Y", "tripType": "RT",
        "itineraries": [leg(origin, d, dep), leg(d, origin, ret)],
        "openReturnDays": 0,
        "flightFilter": {
            "filter": {
                "airlines": [], "departureAirports": [[origin], []], "arrivalAirports": [[], [origin]],
                "departureTime": [slots_for(cfg["outbound_depart_between"]),
                                  slots_for(cfg["return_depart_between"])],
                "fareTypes": [], "flightDurationSeconds": [], "hasCardBenefit": True,
                "isIndividual": False, "isLowCarbonEmission": False, "isSameAirlines": False,
                "isSameDepArrAirport": True, "isTravelClub": False, "minFare": {}, "viaCount": [],
                "selectedItineraries": [],
            },
            "limit": cfg["fare_limit"], "skip": 0, "sort": {"adultMinFare": 1},
        },
        "initialRequest": True,
    }


def call_search(cfg, body, raw_dump=None):
    """SSE 를 끝까지 읽고 마지막 이벤트(JSON) 반환. raw_dump 에 원본 일부 저장(진단용)"""
    headers = {
        "Content-Type": "application/json", "Accept": "text/event-stream",
        "User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
        "Origin": "https://flight.naver.com", "Referer": "https://flight.naver.com/",
        "Cache-Control": "no-cache",
    }
    t0 = time.time()
    last, n_events = None, 0
    with requests.post(API_BASE + SEARCH_PATH, json=body, headers=headers, stream=True,
                       timeout=(15, 25)) as r:
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        buf = []
        for raw in r.iter_lines(decode_unicode=False):
            line = raw.decode("utf-8", "replace") if raw is not None else ""
            if line.startswith("data:"):
                buf.append(line[5:].lstrip())
            elif line == "" and buf:
                payload = "\n".join(buf)
                buf = []
                try:
                    last = json.loads(payload)
                    n_events += 1
                except json.JSONDecodeError:
                    pass
            if last and (last.get("status") or {}).get("isCompleted"):
                pass  # 서버가 스트림을 닫을 때까지 계속 읽음
            if time.time() - t0 > cfg["search_timeout_sec"]:
                break
        if buf:
            try:
                last = json.loads("\n".join(buf))
                n_events += 1
            except json.JSONDecodeError:
                pass
    if raw_dump is not None:
        raw_dump.update(events=n_events, seconds=round(time.time() - t0, 1))
    if last is None:
        raise RuntimeError("응답 이벤트 없음")
    return last


# ---------------------------------------------------------------- 응답 해석
def _strings(v):
    if isinstance(v, str):
        return [v]
    if isinstance(v, dict):
        return [s for x in v.values() for s in _strings(x)]
    if isinstance(v, list):
        return [s for x in v for s in _strings(x)]
    return []


def fare_type_name(code, code_map):
    """'A01/B80' -> '성인/모든 결제수단 / 현대 M2/M3 ...' 처럼 이름을 모아 반환"""
    names = []
    for key in [code] + code.split("/"):
        if key in code_map:
            names += _strings(code_map[key])
    return " ".join(dict.fromkeys(names)) or code


def _seg_time(point):
    s = point.get("dateTime") or ""
    if len(s) >= 12:
        return s[8:10] + ":" + s[10:12]
    t = (point.get("time") or "").replace(":", "")
    return f"{t[:2]}:{t[2:4]}" if len(t) >= 4 else ""


def _airline_name(code, amap):
    v = amap.get(code)
    if isinstance(v, str):
        return v
    s = _strings(v)
    return s[0] if s else code


def parse_result(ev, cfg):
    """마지막 이벤트 -> 항공권 목록 [{prices:{group:가격}, general, out, ret, airlines, ...}]"""
    st = ev.get("status") or {}
    code_map = st.get("fareTypesCodeMap") or {}
    amap = st.get("airlinesCodeMap") or {}
    its = {it.get("itineraryId"): it for it in ev.get("itineraries") or []}
    excl = [x.lower() for x in cfg["exclude_airlines"]]
    groups = cfg["payment_groups"]
    flights = []
    for fm in ev.get("fareMappings") or []:
        ids = (fm.get("itineraryIds") or "").split("-")
        legs = [its.get(i) for i in ids]
        if len(legs) != 2 or not all(legs):
            continue
        codes, names, times = [], [], []
        for leg in legs:
            segs = leg.get("segments") or []
            if not segs:
                break
            for sg in segs:
                for k in ("marketingCarrier", "operatingCarrier"):
                    c = (sg.get(k) or {}).get("airlineCode") or ""
                    if c:
                        codes.append(c)
            names.append(_airline_name((segs[0].get("marketingCarrier") or {}).get("airlineCode", ""), amap))
            times += [_seg_time(segs[0].get("departure") or {}), _seg_time(segs[-1].get("arrival") or {})]
        if len(times) != 4:
            continue
        label = " ".join(codes + names + [_airline_name(c, amap) for c in codes]).lower()
        excluded = any(x in label for x in excl)
        general, by_group = None, {}
        for f in fm.get("fares") or []:
            ft = f.get("fareType") or ""
            try:
                price = int(float((f.get("adult") or {}).get("totalFare")))
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue
            nm = fare_type_name(ft, code_map)
            if not ft.startswith("A01") or "장애" in nm or "소아" in nm or "유아" in nm:
                continue  # 성인 운임(A01 계열)만 사용. 장애인(A07) 등 대상 제한 운임 제외
            if ft == "A01":
                general = price if general is None else min(general, price)
            for g, kws in groups.items():
                if any(k in nm for k in kws):
                    by_group[g] = price if g not in by_group else min(by_group[g], price)
        prices = {}
        for g in groups:  # 실제 결제가 = min(그 결제수단 할인가, 모든 결제수단 운임)
            cand = [p for p in (by_group.get(g), general) if p]
            if cand:
                prices[g] = {"price": min(cand), "discount": g in by_group and by_group[g] <= (general or 10**12)}
        if not prices:
            continue
        flights.append({
            "prices": prices, "general": general, "excluded": excluded,
            "airlines": names if names[0] != names[1] else names[:1],
            "times": times,  # 가는편 출발·도착, 오는편 출발·도착
        })
    return flights


def in_window(t, w):
    return hhmm(w[0]) <= hhmm(t) <= hhmm(w[1])


def usable(f, cfg):
    return (not f["excluded"] and in_window(f["times"][0], cfg["outbound_depart_between"])
            and in_window(f["times"][2], cfg["return_depart_between"]))


# ---------------------------------------------------------------- 조회
def search_trip(cfg, origin, dep, ret):
    body = search_body(cfg, origin, dep, ret)
    last_err = None
    for attempt in range(2):
        try:
            ev = call_search(cfg, body)
            st = ev.get("status") or {}
            if ev.get("status") is None and "isExpired" not in ev:
                raise RuntimeError("예상과 다른 응답(status 없음)")
            return "ok", parse_result(ev, cfg), st
        except Exception as e:  # 연결 끊김 등은 한 번 더
            last_err = e
            time.sleep(8)
    return f"error: {type(last_err).__name__}: {str(last_err)[:120]}", [], {}


def run_scan(cfg, limit=None):
    trips = build_trips(cfg)
    if limit:
        trips = trips[:limit]
    deadline = time.time() + cfg["max_run_minutes"] * 60
    results, failures = [], []
    done, empty_run, seen_any = 0, 0, False
    log(f"조회 시작: 최대 일정 {len(trips)}개 x 출발공항 {len(cfg['origins'])}개")
    for dep, ret, pattern in trips:
        if time.time() > deadline:
            failures.append({"trip": f"{dep} 이후", "group": "시간초과", "reason": "최대 실행 시간 초과"})
            log(f"최대 실행 시간을 넘어 {dep} 출발분부터는 다음 실행으로 넘깁니다.")
            break
        trip_any = False
        for origin in cfg["origins"]:
            done += 1
            tag = f"{origin} {dep:%Y-%m-%d}({WEEKDAY_KO[dep.weekday()]})~{ret:%m/%d}"
            status, flights, st = search_trip(cfg, origin, dep, ret)
            ok = [f for f in flights if usable(f, cfg)]
            if flights:
                trip_any = True
            msgs = []
            for g in cfg["payment_groups"]:
                cand = [f for f in ok if g in f["prices"]]
                if status == "ok" and cand:
                    best = min(cand, key=lambda f: f["prices"][g]["price"])
                    results.append({
                        "group": g, "dep": dep.isoformat(), "ret": ret.isoformat(), "pattern": pattern,
                        "origin": origin, "price": best["prices"][g]["price"],
                        "discount": best["prices"][g]["discount"], "general": best["general"],
                        "airlines": " → ".join(best["airlines"]), "times": best["times"],
                        "url": naver_url(cfg, origin, dep, ret)})
                    msgs.append(f"{g} {best['prices'][g]['price']:,}원")
                else:
                    reason = status if status != "ok" else (
                        "검색 결과 없음(판매 전/운항 없음)" if not flights else "조건에 맞는 항공편 없음")
                    failures.append({"trip": tag, "group": g, "reason": reason})
                    msgs.append(f"{g} 없음({reason})")
            log(f"[{done}] {tag}: " + " / ".join(msgs))
            time.sleep(random.uniform(*cfg["delay_sec"]))
        if trip_any:
            seen_any, empty_run = True, 0
        elif seen_any and (dep - now_kst().date()).days > 270:
            # 시간대 조건 때문에 빈 날도 있으므로, 판매 시작 경계(보통 출발 330~360일 전) 근처에서만 종료 판단
            empty_run += 1
            if empty_run >= cfg["stop_after_empty_trips"]:
                log(f"{dep} 이후는 판매 전으로 보고 조회를 마칩니다.")
                break
    return results, failures, done


# ---------------------------------------------------------------- 결과·메일
def rank(results, group, top_n):
    best = {}
    for r in results:
        if r["group"] != group:
            continue
        k = (r["dep"], r["ret"])
        if k not in best or r["price"] < best[k]["price"]:
            best[k] = r
    return sorted(best.values(), key=lambda r: (r["price"], r["dep"]))[:top_n]


def fmt_day(iso):
    d = dt.date.fromisoformat(iso)
    return f"{d:%y.%m.%d}({WEEKDAY_KO[d.weekday()]})"


def build_report(cfg, tops, last, results, failures, total):
    now = f"{now_kst():%Y-%m-%d %H:%M}"
    ow, rw = cfg["outbound_depart_between"], cfg["return_depart_between"]
    deps = sorted({r["dep"] for r in results})
    span = f"{deps[0]} ~ {deps[-1]} 출발" if deps else "조회된 일정 없음"
    cond = (f"가는 편 {ow[0]}~{ow[1]} · 오는 편 {rw[0]}~{rw[1]} 출발 · {'직항 · ' if cfg['nonstop_only'] else ''}"
            f"성인 {cfg['adults']}명 · 제외: {', '.join(cfg['exclude_airlines'][:2])}")
    text = [f"서울→오사카 2박3일(토·일 포함) 최저가 ({now} KST)", f"비교 기간: {span}", cond, ""]
    sections = []
    for g in cfg["payment_groups"]:
        prev = {(r["dep"], r["ret"]): r["price"] for r in last.get(g, [])}
        rows = []
        for i, r in enumerate(tops.get(g, []), 1):
            p = prev.get((r["dep"], r["ret"]))
            chg = "-" if not last else ("신규" if p is None else ("변동 없음" if p == r["price"] else
                  f"{'▲' if r['price'] > p else '▼'} {abs(r['price'] - p):,}원"))
            rows.append((i, r, chg))
        text.append(f"[{g}] TOP {len(rows)}")
        for i, r, chg in rows:
            t = r["times"]
            text.append(f"{i:>2}. {fmt_day(r['dep'])}~{fmt_day(r['ret'])} {r['origin']} {r['price']:,}원 "
                        f"{r['airlines']} {t[0]}→{t[1]} / {t[2]}→{t[3]} [{chg}]")
        text.append("")
        gen_col = cfg.get("show_general_fare")
        tr = "".join(
            f"<tr><td>{i}</td><td>{fmt_day(r['dep'])} ~ {fmt_day(r['ret'])}<br><small>{r['pattern']}</small></td>"
            f"<td>{r['origin']}</td><td style='text-align:right'><b>{r['price']:,}원</b>"
            f"{'<br><small>할인 적용</small>' if r['discount'] else ''}</td>"
            + (f"<td style='text-align:right'>{(r['general'] or 0):,}원</td>" if gen_col else "")
            + f"<td>{html.escape(r['airlines'])}</td>"
            f"<td>{r['times'][0]}→{r['times'][1]}<br>{r['times'][2]}→{r['times'][3]}</td><td>{chg}</td>"
            f"<td><a href='{html.escape(r['url'])}'>네이버</a></td></tr>"
            for i, r, chg in rows) or "<tr><td colspan='9'>결과 없음</td></tr>"
        sections.append(
            f"<h4>{html.escape(g)} 결제 기준 TOP {len(rows)}</h4>"
            "<table border='1' cellpadding='5' style='border-collapse:collapse;font-size:13px'>"
            "<tr style='background:#eef'><th>#</th><th>일정</th><th>출발</th><th>결제가</th>"
            + ("<th>모든 결제수단</th>" if gen_col else "")
            + "<th>항공사</th><th>가는 편 / 오는 편</th><th>지난번 대비</th><th>링크</th></tr>"
            f"{tr}</table>")
    errs = [f for f in failures if f["reason"].startswith("error")]
    n_err = len({f["trip"] for f in errs})
    warn = ""
    if total and n_err > total * 0.3:
        warn = (f"<p style='color:#b00'>⚠ 조회 오류 {n_err}/{total}건 ({html.escape(errs[0]['reason'])}). "
                "네이버 API가 바뀌었거나 접속이 막혔을 수 있습니다.</p>")
    if any(f["group"] == "시간초과" for f in failures):
        warn += "<p style='color:#b00'>⚠ 최대 실행 시간을 넘어 일부 날짜를 조회하지 못했습니다(부분 결과).</p>"
    body = (f"<html><body style=\"font-family:'Malgun Gothic',sans-serif\">"
            f"<h3>서울→오사카 2박3일(토·일 포함) 최저가</h3><p>{now} KST · <b>비교 기간: {span}</b><br>"
            f"{html.escape(cond)} · 조회 {total}건</p>{warn}{''.join(sections)}"
            "<p style='color:#666;font-size:12px'>결제가 = 그 결제수단 할인 운임과 일반 운임 중 낮은 값(1인, 세금 포함). "
            "할인은 카드 실적 등 조건이 붙을 수 있고 수하물 미포함일 수 있으니 예매 전 네이버에서 확인하세요.</p>"
            "</body></html>")
    sig = lambda t: [(r["dep"], r["ret"], r["price"]) for r in t]
    changed = any(sig(tops.get(g, [])) != sig(last.get(g, [])) for g in cfg["payment_groups"])
    return "\n".join(text), body, changed


def send_mail(cfg, subject, text, body):
    m = cfg["mail"]
    if not (m["user"] and m["app_password"] and m["to"]):
        raise RuntimeError("메일 설정 없음 (MAIL_USER / MAIL_APP_PASSWORD / MAIL_TO)")
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, m["user"], ", ".join(m["to"])
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(body, "html", "utf-8"))
    pw = re.sub(r"\s", "", m["app_password"])
    mask = lambda a: (a[:2] + "***" + a[a.find("@"):]) if "@" in a else "***"
    log(f"메일 발송 시도: 보내는 {mask(m['user'])} → 받는 {', '.join(mask(x) for x in m['to'])} "
        f"(앱 비밀번호 {len(pw)}자{'' if len(pw) == 16 and pw.isalpha() else ' ⚠ Gmail 앱 비밀번호는 영문 16자'})")
    last = None
    for attempt, (port, ssl_mode) in enumerate([(m["smtp_port"], False), (465, True), (m["smtp_port"], False)]):
        try:  # 587(STARTTLS) → 안 되면 465(SSL) → 다시 587, 사이사이 대기
            if ssl_mode:
                s = smtplib.SMTP_SSL(m["smtp_host"], port, timeout=30)
            else:
                s = smtplib.SMTP(m["smtp_host"], port, timeout=30)
                s.starttls()
            with s:
                s.login(m["user"], pw)
                s.sendmail(m["user"], m["to"], msg.as_string())
            if attempt:
                log(f"메일 발송: {attempt + 1}번째 시도({port}번 포트)에서 성공")
            return
        except smtplib.SMTPAuthenticationError as e:
            raise RuntimeError(f"Gmail 로그인 거부(앱 비밀번호·주소 확인 필요): {e.smtp_code} "
                               f"{e.smtp_error.decode(errors='replace')[:150]}") from e
        except (smtplib.SMTPException, OSError) as e:
            last = e
            log(f"메일 발송 실패({port}번 포트): {type(e).__name__}: {e}")
            time.sleep(15)
    raise RuntimeError(f"Gmail 연결이 계속 끊김 — Google이 로그인을 막았을 수 있음 "
                       f"(Google 계정 보안 알림 확인 필요): {type(last).__name__}: {last}")


def save(tops, results, keep_days=120):
    DATA.mkdir(exist_ok=True)
    hist = DATA / "history.csv"
    if hist.exists():  # 기록은 최근 keep_days 일치만 유지 (저장소가 계속 커지지 않게)
        cut = f"{now_kst() - dt.timedelta(days=keep_days):%Y-%m-%d}"
        rows = list(csv.reader(hist.open(encoding="utf-8-sig")))
        if rows:
            kept = [rows[0]] + [r for r in rows[1:] if r and r[0][:10] >= cut]
            with hist.open("w", newline="", encoding="utf-8-sig") as f:
                csv.writer(f).writerows(kept)
    (DATA / "last_top.json").write_text(json.dumps(tops, ensure_ascii=False, indent=1), encoding="utf-8")
    hist = DATA / "history.csv"
    new = not hist.exists()
    with hist.open("a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["조회시각(KST)", "결제구분", "출발일", "귀국일", "출발공항", "결제가", "일반운임", "항공사", "시각"])
        stamp = f"{now_kst():%Y-%m-%d %H:%M}"
        for r in results:
            w.writerow([stamp, r["group"], r["dep"], r["ret"], r["origin"], r["price"], r["general"],
                        r["airlines"], " ".join(r["times"])])


# ---------------------------------------------------------------- 자동 대응 (상태는 data/state.json)
STATE_PATH = DATA / "state.json"


def load_state():
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(st):
    DATA.mkdir(exist_ok=True)
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def apply_state(cfg, st):
    drop = set(st.get("excluded_origins", []))
    cfg["origins"] = [o for o in cfg["origins"] if o not in drop] or cfg["origins"]
    return cfg


def auto_guard(cfg, st, failures, total):
    """오류가 많으면 김포 제외 → 그래도 계속되면 쉬기. 알릴 내용(문자열) 또는 None 반환"""
    err_trips = {f["trip"] for f in failures if f["reason"].startswith("error")}
    ratio = len(err_trips) / total if total else 0
    sample = next((f["reason"] for f in failures if f["reason"].startswith("error")), "")
    now = now_kst()
    if total >= 4 and ratio >= cfg["auto_guard_error_ratio"]:
        drop = [o for o in cfg["auto_drop_origins"] if o in cfg["origins"]]
        if drop and len(drop) < len(cfg["origins"]):
            st["excluded_origins"] = sorted(set(st.get("excluded_origins", [])) | set(drop))
            st["excluded_since"] = f"{now:%Y-%m-%d %H:%M}"
            st["excluded_reason"] = f"검색 오류 {len(err_trips)}/{total}건 ({sample[:100]})"
            save_state(st)
            return (f"검색 오류가 {len(err_trips)}/{total}건({ratio:.0%}) 발생해 다음 실행부터 "
                    f"{', '.join(drop)} 출발을 자동으로 제외합니다. (예: {sample})")
        hours = min(24, max(3, st.get("pause_hours", 0) * 2 or 3))
        st["pause_hours"] = hours
        st["pause_until"] = (now + dt.timedelta(hours=hours)).isoformat()
        save_state(st)
        return (f"출발 공항을 줄인 뒤에도 검색 오류가 {len(err_trips)}/{total}건({ratio:.0%}) 발생해 "
                f"{hours}시간 동안 조회를 쉬었다가 다시 시도합니다. 계속되면 네이버 차단일 수 있습니다. (예: {sample})")
    if st.get("pause_hours") or st.get("pause_until"):
        st.pop("pause_hours", None)
        st.pop("pause_until", None)
        save_state(st)
        return None
    return None


# ---------------------------------------------------------------- 진단
def probe(cfg, date_str, origin):
    dep = dt.date.fromisoformat(date_str)
    ret = dep + dt.timedelta(days=2)
    out = DATA / "probe"
    out.mkdir(parents=True, exist_ok=True)
    body = search_body(cfg, origin, dep, ret)
    info = {}
    ev = call_search(cfg, body, info)
    (out / "request.json").write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
    slim = dict(ev)
    slim["itineraries"] = (ev.get("itineraries") or [])[:30]
    slim["fareMappings"] = (ev.get("fareMappings") or [])[:30]
    (out / "response_sample.json").write_text(json.dumps(slim, ensure_ascii=False, indent=1)[:3_000_000],
                                              encoding="utf-8")
    st = ev.get("status") or {}
    (out / "fare_types.json").write_text(json.dumps(st.get("fareTypesCodeMap"), ensure_ascii=False, indent=1),
                                         encoding="utf-8")
    flights = parse_result(ev, cfg)
    log(f"진단 {origin} {dep}~{ret}: 이벤트 {info.get('events')}개, {info.get('seconds')}초, "
        f"완료={st.get('isCompleted')}, 판매처 {st.get('completedPartnerCount')}/{st.get('requestedPartnerCount')}")
    log(f"  항공편 {len(ev.get('itineraries') or [])}개, 운임 묶음 {len(ev.get('fareMappings') or [])}개, "
        f"해석된 항공권 {len(flights)}개 (조건 충족 {sum(usable(f, cfg) for f in flights)}개)")
    names = {k: fare_type_name(k, st.get("fareTypesCodeMap") or {}) for k in (st.get("fareTypesCodeMap") or {})}
    for g, kws in cfg["payment_groups"].items():
        hit = [f"{k}={v}" for k, v in names.items() if any(x in v for x in kws)]
        log(f"  [{g}] 운임 코드: {', '.join(hit) or '없음 → 일반 운임(모든 결제수단)으로 계산'}")
        cand = [f for f in flights if usable(f, cfg) and g in f["prices"]]
        if cand:
            b = min(cand, key=lambda f: f["prices"][g]["price"])
            log(f"  [{g}] 최저 {b['prices'][g]['price']:,}원 (일반 {b['general']:,}원) "
                f"{' → '.join(b['airlines'])} {' '.join(b['times'])}")
    log(f"  진단 파일: {out}")


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", metavar="YYYY-MM-DD")
    ap.add_argument("--origin", default=None)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--no-mail", action="store_true")
    ap.add_argument("--test-mail", action="store_true")
    a = ap.parse_args()
    cfg = load_config()
    DATA.mkdir(exist_ok=True)
    if a.test_mail:
        send_mail(cfg, "[항공권 감시] 메일 설정 확인", "메일 설정이 정상입니다.", "<p>메일 설정이 정상입니다.</p>")
        log("테스트 메일을 보냈습니다.")
        return
    if a.probe:
        probe(cfg, a.probe, a.origin or cfg["origins"][0])
        return
    st = load_state()
    if not a.limit and st.get("pause_until") and dt.datetime.fromisoformat(st["pause_until"]) > now_kst():
        log(f"자동 대응으로 {st['pause_until'][:16]} 까지 조회를 쉬는 중이라 이번 실행은 건너뜁니다.")
        return
    apply_state(cfg, st)
    if st.get("excluded_origins"):
        log(f"자동 제외 중인 출발 공항: {', '.join(st['excluded_origins'])} ({st.get('excluded_since', '')}부터)")
    last_p = DATA / "last_top.json"
    last = json.loads(last_p.read_text(encoding="utf-8")) if last_p.exists() else {}
    results, failures, total = run_scan(cfg, a.limit)
    notice = None if a.limit else auto_guard(cfg, st, failures, total)
    if notice:
        log("자동 대응: " + notice)
        if cfg["mail"]["enabled"] and not a.no_mail:
            send_mail(cfg, "[항공권 감시] 자동 조정 알림", notice, f"<p>{html.escape(notice)}</p>"
                      "<p>되돌리려면 저장소의 data/state.json 파일을 삭제하세요.</p>")
    tops = {g: rank(results, g, cfg["top_n"]) for g in cfg["payment_groups"]}
    text, body, changed = build_report(cfg, tops, last, results, failures, total)
    (DATA / "last_report.html").write_text(body, encoding="utf-8")
    print("\n" + text, flush=True)
    if results and not a.limit:
        save(tops, results)
    errors = sum(1 for f in failures if f["reason"].startswith("error"))
    if not results and total and errors >= total * len(cfg["payment_groups"]) * 0.8 and not notice:
        raise RuntimeError(f"조회가 거의 모두 실패했습니다 ({errors}건). 예: {failures[0]['reason']}")
    if a.no_mail or not cfg["mail"]["enabled"]:
        log("메일 발송 안 함. 결과는 data/last_report.html")
        return
    if cfg["mail"]["only_when_changed"] and not changed and results:
        log("TOP 목록 변동 없음 → 메일 생략")
        return
    lows = [f"{g} {tops[g][0]['price']:,}원({fmt_day(tops[g][0]['dep'])})" for g in cfg["payment_groups"] if tops.get(g)]
    send_mail(cfg, "[항공권 감시] 오사카 주말 최저 " + (" / ".join(lows) or "결과 없음"), text, body)
    log("메일 발송 완료")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        traceback.print_exc()
        log(f"오류로 중단: {type(e).__name__}: {str(e)[:200]}")
        try:
            cfg = load_config()
            if cfg["mail"]["enabled"] and "--test-mail" not in sys.argv:
                send_mail(cfg, "[항공권 감시] 실행 오류", traceback.format_exc(),
                          "<pre>" + html.escape(traceback.format_exc()) + "</pre>")
        except Exception:
            pass
        sys.exit(1)
