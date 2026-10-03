"""덕계도서관(양주시립도서관) 대출현황 -> 노션 대출 기록 DB 동기화.

환경변수
  NOTION_TOKEN, LIBRARY_ID, LIBRARY_PW   (GitHub Secrets)
  DRY_RUN=true  : 노션에 쓰지 않고 건수만 출력
공개 저장소 로그가 공개되므로 책 제목은 절대 출력하지 않는다.
"""
import base64
import os
import re
import sys
import time
from datetime import date

import requests
from bs4 import BeautifulSoup

# ---------- 설정 ----------
BASE = "https://www.libyj.go.kr"
LOGIN_PAGE = BASE + "/dklib/menu/10543/program/30003/memberLogin.do"
LOGIN_PROC = BASE + "/dklib/menu/10543/program/30003/memberLoginProc.do"
LOAN_PATH = "/dklib/menu/10531/program/30026/mypage/loanStatusList.do"
LOAN_URL = BASE + LOAN_PATH

LOAN_DB = "1c6edb47440280de8157e7915a0cbb68"
LIB_DB = "1c4edb4744028051a183c54632991fb6"

P_STATUS = "상태"
P_PERIOD = "대출 기간"
P_LIB = "빌린 도서관"

S_ACTIVE = {"대출중", "연체 중"}
S_RESERVED = {"예약완료", "담아두기"}
S_LOANING = "대출중"
S_RETURNED = "반납완료"

# 이 사이트(양주시립도서관)에서 빌릴 수 있는 도서관. 공백 무시하고 비교한다.
DEFAULT_SCOPE = (
    "양주시도서관,옥정호수도서관,덕정도서관,꿈나무도서관,남면도서관,고읍도서관,"
    "덕계도서관,양주희망도서관,광적도서관,장흥작은도서관,고암작은도서관"
)

UA = {"User-Agent": "Mozilla/5.0 (library-sync personal script)"}


# ---------- 문자열 유틸 ----------
def norm_space(s):
    return re.sub(r"\s+", " ", s or "").strip()


def title_key(s):
    """' : ' 앞부분만 남기고 공백/기호 제거."""
    main = norm_space(s).split(":")[0]
    return re.sub(r"[\W_]+", "", main.lower())


def full_key(s):
    return re.sub(r"[\W_]+", "", (s or "").lower())


def title_match(site_title, notion_title):
    a, b = title_key(site_title), title_key(notion_title)
    if not a or not b:
        return False
    if a == b:
        return True
    if full_key(notion_title) and full_key(notion_title) == full_key(site_title):
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) >= 4 and long_.startswith(short):
        return True
    # 노션에 짧게 적은 제목이 사이트의 긴 제목(앞 괄호·부제 포함) 안에 들어 있는 경우
    site_full, notion_full = full_key(site_title), full_key(notion_title)
    if len(b) >= 4 and b in site_full:
        return True
    return len(a) >= 4 and a in notion_full


def lib_key(s):
    return re.sub(r"\s+", "", s or "")


def to_iso(d):
    return d.replace(".", "-").replace("/", "-")


# ---------- 도서관 사이트 ----------
def parse_loans(html):
    soup = BeautifulSoup(html, "html.parser")
    items = []
    for li in soup.select("ul.article-list > li"):
        t = li.select_one("p.title")
        if not t:
            continue
        text = li.get_text(" ", strip=True)
        lib = re.search(r"도서관\s*:\s*(\S+)", text)
        start = re.search(r"대출일\s*:\s*(\d{4}\.\d{2}\.\d{2})", text)
        end = re.search(r"반납예정일\s*:\s*(\d{4}\.\d{2}\.\d{2})", text)
        if not (start and end):
            raise RuntimeError("대출 항목 형식이 예상과 다릅니다 (사이트 개편 가능성)")
        items.append(
            {
                "title": norm_space(t.get_text()),
                "library": lib.group(1) if lib else "",
                "start": to_iso(start.group(1)),
                "end": to_iso(end.group(1)),
            }
        )
    return items


def login(session, uid, pw):
    r = session.get(LOGIN_PAGE, headers=UA, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    form = soup.find("form", id="loginForm")
    if not form:
        raise RuntimeError("로그인 화면 구조가 바뀌었습니다")
    data = {i.get("name"): i.get("value", "") for i in form.find_all("input") if i.get("name")}
    data["userId"] = uid
    data["password"] = pw
    data["returnUrl"] = base64.b64encode(LOAN_PATH.encode()).decode()
    r = session.post(LOGIN_PROC, data=data, headers={**UA, "Referer": LOGIN_PAGE}, timeout=30)
    r.raise_for_status()


def fetch_loans(session):
    loans, seen_pages, page = [], set(), 1
    while page not in seen_pages and page <= 30:
        seen_pages.add(page)
        r = session.get(
            LOAN_URL,
            params={"currentPageNo": page, "searchSort": "RETURNPLANDATE"},
            headers=UA,
            timeout=30,
        )
        r.raise_for_status()
        if 'name="loginForm"' in r.text or "통합로그인" in r.text:
            raise RuntimeError("로그인에 실패했습니다 (아이디/비밀번호 확인)")
        if "대출현황" not in r.text or "article-list" not in r.text:
            raise RuntimeError("대출현황 화면 구조가 바뀌었습니다")
        loans += parse_loans(r.text)
        nums = {int(n) for n in re.findall(r"fnList\((\d+)\)", r.text)}
        nxt = sorted(n for n in nums if n not in seen_pages)
        if not nxt:
            break
        page = nxt[0]
    # 중복 제거
    uniq, seen = [], set()
    for ln in loans:
        k = (full_key(ln["title"]), ln["start"], lib_key(ln["library"]))
        if k not in seen:
            seen.add(k)
            uniq.append(ln)
    return uniq


def fetch_yangju(uid, pw):
    with requests.Session() as s:
        login(s, uid, pw)
        return fetch_loans(s)


# ---------- 의정부(가재울도서관 포함 의정부시 도서관 통합) ----------
UI_BASE = "https://www.uilib.go.kr"
UI_LOGIN = UI_BASE + "/gajaeul/intro/login/index.do?menu_idx=55"
UI_LOAN = UI_BASE + "/gajaeul/intro/search/loan/index.do?menu_idx=41"
UI_SCOPE = "의정부시도서관,정보도서관,과학도서관,미술도서관,음악도서관,영어도서관,가재울도서관,작은도서관"


def parse_ui_loans(html):
    """의정부 대출현황 표 -> (항목들, 통합대출권수, 페이지 번호들)."""
    soup = BeautifulSoup(html, "html.parser")
    summary = soup.select_one("table.loanTbl td.td1")
    table = soup.select_one("table.default-tbl")
    if summary is None or table is None:
        raise RuntimeError("의정부 대출현황 화면 구조가 바뀌었습니다")
    expected = int(re.sub(r"\D", "", summary.get_text()) or 0)
    items = []
    for tr in table.select("tbody tr"):
        cells = {c: tr.select_one(f"td.{c}") for c in ("td3", "td5", "td6", "td7")}
        if any(v is None for v in cells.values()):
            continue  # '내역이 없습니다' 같은 안내 행
        d1 = re.search(r"\d{4}[./]\d{2}[./]\d{2}", cells["td6"].get_text())
        d2 = re.search(r"\d{4}[./]\d{2}[./]\d{2}", cells["td7"].get_text())
        if not (d1 and d2):
            raise RuntimeError("의정부 대출 항목 형식이 예상과 다릅니다 (사이트 개편 가능성)")
        lib = norm_space(cells["td5"].get_text())
        if lib and not lib.endswith("도서관"):
            lib += "도서관"
        items.append(
            {
                "title": norm_space(cells["td3"].get_text()),
                "library": lib,
                "start": to_iso(d1.group(0)),
                "end": to_iso(d2.group(0)),
            }
        )
    pages = {int(a["keyvalue"]) for a in soup.select("#board_paging a[keyvalue]") if a.get("keyvalue", "").isdigit()}
    return items, expected, pages


def goto_retry(page, url, tries=3):
    """페이지 열기만 재시도한다 (로그인 제출은 계정 잠김 방지를 위해 재시도하지 않는다)."""
    last = None
    for i in range(tries):
        try:
            page.goto(url, wait_until="domcontentloaded")
            return
        except Exception as e:
            last = e
            print(f"  의정부 접속 재시도 {i + 1}/{tries}: {type(e).__name__}")
            time.sleep(10)
    raise RuntimeError(f"의정부 사이트에 접속하지 못했습니다 ({tries}회 시도, {type(last).__name__})")


def fetch_uijeongbu(uid, pw):
    # 사이트가 아이디/비밀번호를 자바스크립트로 암호화해서 보내므로, 실제 브라우저로 사이트의 로그인 함수를 그대로 쓴다.
    from playwright.sync_api import sync_playwright

    loans, expected, seen, queue = [], None, set(), [1]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_context(locale="ko-KR").new_page()
            page.set_default_timeout(60000)
            page.route(
                "**/*",
                lambda route: route.abort() if route.request.resource_type in ("image", "media", "font") else route.continue_(),
            )
            goto_retry(page, UI_LOGIN)
            page.fill("#member_id_tmp", uid)
            page.fill("#member_pw_tmp", pw)
            try:
                with page.expect_navigation(wait_until="domcontentloaded"):
                    page.evaluate("doLoginSubmit()")
            except Exception:
                raise RuntimeError("의정부 로그인 응답이 없습니다 (아이디/비밀번호 또는 사이트 상태 확인)")
            while queue:
                n_ = queue.pop(0)
                seen.add(n_)
                goto_retry(page, f"{UI_LOAN}&viewPage={n_}")
                if page.query_selector("#member_id_tmp"):
                    raise RuntimeError("의정부 로그인에 실패했습니다 (아이디/비밀번호 확인)")
                items, exp, pages = parse_ui_loans(page.content())
                expected = exp if expected is None else expected
                loans += items
                queue += sorted(x for x in pages if x not in seen and x not in queue)
                if len(seen) > 30:
                    break
        finally:
            browser.close()
    uniq, keys = [], set()
    for ln in loans:
        k = (full_key(ln["title"]), ln["start"], lib_key(ln["library"]))
        if k not in keys:
            keys.add(k)
            uniq.append(ln)
    if expected is not None and len(uniq) != expected:
        raise RuntimeError(f"의정부 대출 권수가 맞지 않습니다 (표 {len(uniq)}건 / 사이트 표기 {expected}건)")
    return uniq


# ---------- 노션 ----------
class Notion:
    def __init__(self, token):
        self.h = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        }

    def call(self, method, path, **kw):
        for attempt in range(5):
            r = requests.request(method, "https://api.notion.com/v1" + path, headers=self.h, timeout=30, **kw)
            if r.status_code == 429:
                time.sleep(float(r.headers.get("Retry-After", 2)))
                continue
            if r.status_code >= 400:
                # 응답 본문에 제목이 섞일 수 있어 상태 코드만 남긴다
                raise RuntimeError(f"노션 API 오류 {r.status_code} ({method} {path.split('/')[1]})")
            time.sleep(0.35)
            return r.json()
        raise RuntimeError("노션 API 요청이 계속 제한되었습니다")

    def schema(self, db):
        return self.call("GET", f"/databases/{db}")["properties"]

    def query_all(self, db):
        out, cursor = [], None
        while True:
            body = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            res = self.call("POST", f"/databases/{db}/query", json=body)
            out += res["results"]
            if not res.get("has_more"):
                return out
            cursor = res["next_cursor"]


def plain(rich):
    return "".join(x.get("plain_text", "") for x in rich)


def read_loan_pages(raw, title_prop):
    pages = []
    for p in raw:
        pr = p["properties"]
        st = pr.get(P_STATUS) or {}
        status = (st.get("select") or st.get("status") or {}).get("name")
        per = (pr.get(P_PERIOD) or {}).get("date") or {}
        pages.append(
            {
                "id": p["id"],
                "title": plain(pr[title_prop]["title"]),
                "status": status,
                "start": (per.get("start") or "")[:10] or None,
                "end": (per.get("end") or "")[:10] or None,
                "libs": [x["id"] for x in (pr.get(P_LIB) or {}).get("relation", [])],
            }
        )
    return pages


# ---------- 비교 계획 ----------
def days_apart(a, b):
    if not a or not b:
        return None
    return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)


def pick_page(ln, pages, used):
    """같은 책의 기존 기록을 고른다. (페이지, 종류) 반환.
    종류: same(같은 대출) / fix(날짜가 며칠 어긋난 직접 입력) / convert(예약->대출) / reuse(재대출)
    """
    cands = [p for p in pages if p["id"] not in used and title_match(ln["title"], p["title"])]
    if not cands:
        return None, None
    for p in cands:
        if p["start"] == ln["start"]:
            return p, "same"
    near = [
        p for p in cands
        if p["status"] in S_ACTIVE and days_apart(p["start"], ln["start"]) is not None and days_apart(p["start"], ln["start"]) <= 3
    ]
    if near:
        return min(near, key=lambda p: days_apart(p["start"], ln["start"])), "fix"
    reserved = [p for p in cands if p["status"] in S_RESERVED]
    if reserved:
        return reserved[0], "convert"
    returned = [p for p in cands if p["status"] == S_RETURNED]
    if returned:
        return max(returned, key=lambda p: p["start"] or ""), "reuse"
    active = [p for p in cands if p["status"] in S_ACTIVE]
    if active:
        return max(active, key=lambda p: p["start"] or ""), "reuse"
    return None, None


def make_plan(loans, pages, lib_by_key, scope_ids):
    used, plan = set(), []
    stats = {"create": 0, "convert": 0, "reuse": 0, "fix": 0, "return": 0, "same": 0}

    for ln in loans:
        lib_id = lib_by_key.get(lib_key(ln["library"]))
        page, kind = pick_page(ln, pages, used)

        if page is None:
            plan.append(("create", None, {"title": ln["title"], "status": S_LOANING, "period": (ln["start"], ln["end"]), "lib": lib_id}, {"type": "create", "site": ln["title"]}))
            stats["create"] += 1
            continue

        used.add(page["id"])
        if kind == "same":
            props = {}
            if page["end"] != ln["end"]:
                props["period"] = (ln["start"], ln["end"])
            if lib_id and not page["libs"]:
                props["lib"] = lib_id
            if props:
                plan.append(("update", page["id"], props, {"type": kind, "site": ln["title"], "notion": page["title"], "old": (page["status"], page["start"], page["end"]), "new": props.get("period")}))
                stats["fix"] += 1
            else:
                stats["same"] += 1
        elif kind == "fix":
            props = {"period": (ln["start"], ln["end"])}
            if lib_id and not page["libs"]:
                props["lib"] = lib_id
            plan.append(("update", page["id"], props, {"type": kind, "site": ln["title"], "notion": page["title"], "old": (page["status"], page["start"], page["end"]), "new": props.get("period")}))
            stats["fix"] += 1
        else:  # convert / reuse: 대출중으로 바꾸고 기간과 도서관을 새 대출에 맞춘다
            props = {"status": S_LOANING, "period": (ln["start"], ln["end"])}
            if lib_id:
                props["lib"] = lib_id
            plan.append(("update", page["id"], props, {"type": kind, "site": ln["title"], "notion": page["title"], "old": (page["status"], page["start"], page["end"]), "new": props.get("period")}))
            stats[kind] += 1

    for p in pages:
        if p["id"] in used or p["status"] not in S_ACTIVE or not p["libs"]:
            continue
        if all(l in scope_ids for l in p["libs"]):
            plan.append(("update", p["id"], {"status": S_RETURNED}, {"type": "return", "notion": p["title"], "old": (p["status"], p["start"], p["end"]), "new": None}))
            stats["return"] += 1
    return plan, stats


def build_props(props, schema, title_prop):
    out = {}
    if "title" in props:
        out[title_prop] = {"title": [{"text": {"content": props["title"][:1900]}}]}
    if "status" in props:
        kind = schema[P_STATUS]["type"]  # select 또는 status
        out[P_STATUS] = {kind: {"name": props["status"]}}
    if "period" in props:
        s, e = props["period"]
        out[P_PERIOD] = {"date": {"start": s, "end": e}}
    if props.get("lib"):
        out[P_LIB] = {"relation": [{"id": props["lib"]}]}
    return out


# ---------- 진단 (404일 때만 실행) ----------
def diagnose(n, ids):
    """어떤 DB가 보이는지 알려준다. 책 제목은 출력하지 않는다."""
    print("--- 진단 시작 ---")
    try:
        me = n.call("GET", "/users/me")
        ws = (me.get("bot") or {}).get("workspace_name")
        print(f"토큰 확인: 정상 (연결된 워크스페이스: {ws})")
    except RuntimeError as e:
        print(f"토큰 확인: 실패 -> {e} (NOTION_TOKEN 값을 다시 확인하세요)")
        return

    try:
        res = n.call("POST", "/search", json={"filter": {"property": "object", "value": "database"}, "page_size": 100})
        found = res.get("results", [])
        print(f"이 연결이 볼 수 있는 DB: {len(found)}개")
        for d in found:
            title = plain(d.get("title", []))[:30]
            print(f"  - {d['id'].replace('-', '')}  ({title})")
    except RuntimeError as e:
        print(f"DB 목록 조회 실패 -> {e}")

    for name, db in ids.items():
        print(f"[{name}] 설정된 ID: {db}")
        try:
            n.call("GET", f"/pages/{db}")
        except RuntimeError:
            continue
        print(f"  -> 이 ID는 DB가 아니라 '페이지'입니다. 그 안의 DB를 찾습니다.")
        try:
            kids = n.call("GET", f"/blocks/{db}/children?page_size=100").get("results", [])
            for k in kids:
                if k.get("type") == "child_database":
                    print(f"  -> 안에 있는 DB: {k['id'].replace('-', '')} ({k['child_database'].get('title', '')[:30]})")
        except RuntimeError as e:
            print(f"  -> 페이지 안을 읽지 못했습니다: {e}")
    print("--- 진단 끝 ---")
    print("위 '볼 수 있는 DB' 목록에 대출 기록/도서관 DB의 ID가 없으면, 그 DB에 연결이 추가되지 않은 것입니다.")


def get_schema(n, db):
    try:
        return n.schema(db)
    except RuntimeError as e:
        if "404" in str(e):
            diagnose(n, {"대출 기록 DB": LOAN_DB, "도서관 DB": LIB_DB})
        raise


# ---------- 실행 ----------
SITES = [
    ("덕계(양주)", fetch_yangju, DEFAULT_SCOPE),
    ("가재울(의정부)", fetch_uijeongbu, UI_SCOPE),
]


def main():
    dry = os.environ.get("DRY_RUN", "true").lower() == "true"
    print("모드: " + ("확인만 (DRY_RUN, 노션에 쓰지 않음)" if dry else "실제 반영"))
    uid, pw, token = os.environ["LIBRARY_ID"], os.environ["LIBRARY_PW"], os.environ["NOTION_TOKEN"]

    n = Notion(token)
    loan_schema = get_schema(n, LOAN_DB)
    for name in (P_STATUS, P_PERIOD, P_LIB):
        if name not in loan_schema:
            raise RuntimeError(f"대출 기록 DB에 '{name}' 속성이 없습니다")
    title_prop = next(k for k, v in loan_schema.items() if v["type"] == "title")
    lib_title_prop = next(k for k, v in get_schema(n, LIB_DB).items() if v["type"] == "title")

    lib_by_key = {}
    for p in n.query_all(LIB_DB):
        lib_by_key[lib_key(plain(p["properties"][lib_title_prop]["title"]))] = p["id"]

    failed = []
    for site, fetcher, default_scope in SITES:
        try:
            loans = fetcher(uid, pw)
        except Exception as e:  # 한 사이트가 실패해도 다른 사이트는 계속 처리
            print(f"[{site}] 실패: {str(e)[:300]}")
            failed.append(site)
            continue
        print(f"[{site}] 도서관 대출 {len(loans)}건 확인")

        scope_names = {lib_key(x) for x in default_scope.split(",")} | {lib_key(ln["library"]) for ln in loans}
        scope_ids = {lib_by_key[k] for k in scope_names if k in lib_by_key}
        missing = {ln["library"] for ln in loans if lib_key(ln["library"]) not in lib_by_key}
        if missing:
            print(f"[{site}] 경고: 도서관 DB에서 못 찾은 도서관 {len(missing)}곳 (빌린 도서관을 비워둡니다)")

        pages = read_loan_pages(n.query_all(LOAN_DB), title_prop)  # 앞 사이트가 쓴 내용을 반영해 매번 새로 읽는다
        plan, st = make_plan(loans, pages, lib_by_key, scope_ids)
        print(
            f"[{site}] 계획: 새로 만듦 {st['create']}, 예약→대출중 {st['convert']}, 기존 기록 재사용(반납완료 등→대출중) {st['reuse']}, "
            f"날짜/도서관 갱신 {st['fix']}, 반납완료 {st['return']}, 변경 없음 {st['same']}"
        )

        if dry:
            if os.environ.get("SHOW_TITLES", "false").lower() == "true":
                mask = lambda t: (t or "")[:3] + "…"
                for _k, _id, _props, m in plan:
                    print(f"  [{m['type']}] 사이트 {mask(m.get('site'))} / 노션 {mask(m.get('notion'))} / 기존 {m.get('old')} -> {m.get('new')}")
            print(f"[{site}] DRY_RUN: 노션에 아무것도 쓰지 않았습니다")
            continue
        for kind, page_id, props, _meta in plan:
            body = build_props(props, loan_schema, title_prop)
            if kind == "create":
                n.call("POST", "/pages", json={"parent": {"database_id": LOAN_DB}, "properties": body})
            else:
                n.call("PATCH", f"/pages/{page_id}", json={"properties": body})
        print(f"[{site}] 노션 반영 완료")

    if failed:
        raise RuntimeError("실패한 사이트: " + ", ".join(failed))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # 실패하면 아무것도 바꾸지 않고 종료 코드로 알림
        print(f"실패: {e}")
        sys.exit(1)
