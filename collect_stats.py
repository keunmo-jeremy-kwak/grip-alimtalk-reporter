#!/usr/bin/env python3
"""
UMS 통계 수집기
매일 오전 6시(KST) GitHub Actions에서 실행
전일자 알림톡 발송/성공 건수를 고객사별로 수집하여 Google Sheets에 적재
"""

import asyncio
import json
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import gspread
from google.oauth2.service_account import Credentials
from playwright.async_api import Page, async_playwright

# ── 설정 ──────────────────────────────────────────────────────────────────────
LOGIN_URL   = "https://ums.dktechinmsg.com/user/login"
STATS_URL   = "https://ums.dktechinmsg.com/user/statistics/real-time-result"
ADMIN_EMAIL = os.environ["ADMIN_EMAIL"]
ADMIN_PW    = os.environ["ADMIN_PASSWORD"]

SPREADSHEET_ID = "1dxhCoJ1kGJGRnfz5OaNJrBL6UqXPjuSPtzh5PFWjLtE"
SHEET_NAME     = "report"
GCP_SCOPES     = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
REPORT_TZ      = ZoneInfo("Asia/Seoul")
TARGET_DATE    = os.environ.get("TARGET_DATE")


# ── Google Sheets ─────────────────────────────────────────────────────────────
def get_worksheet():
    info  = json.loads(os.environ["GCP_SERVICE_ACCOUNT_JSON"])
    creds = Credentials.from_service_account_info(info, scopes=GCP_SCOPES)
    gc    = gspread.authorize(creds)
    return gc.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)


def resolve_target_date() -> str:
    if TARGET_DATE:
        try:
            return datetime.strptime(TARGET_DATE, "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(
                f"TARGET_DATE 형식이 올바르지 않습니다: {TARGET_DATE} (예: 2026-04-30)"
            ) from exc
    return (datetime.now(REPORT_TZ) - timedelta(days=1)).strftime("%Y-%m-%d")

def upsert_rows(ws, rows: list[list]) -> None:
    if not rows:
        return

    existing_values = ws.get_all_values()
    existing_map: dict[tuple[str, str], int] = {}

    for idx, row in enumerate(existing_values, start=1):
        if len(row) < 2:
            continue
        date_value = row[0].strip()
        customer_value = row[1].strip()
        if (date_value, customer_value) == ("date", "customer"):
            continue
        if not date_value or not customer_value:
            continue
        existing_map[(date_value, customer_value)] = idx

    updates = []
    appends = []

    for row in rows:
        key = (str(row[0]).strip(), str(row[1]).strip())
        row_index = existing_map.get(key)
        if row_index:
            updates.append({"range": f"A{row_index}:E{row_index}", "values": [row]})
        else:
            appends.append(row)

    if updates:
        ws.batch_update(updates, value_input_option="USER_ENTERED")

    if appends:
        ws.append_rows(appends, value_input_option="USER_ENTERED")


# ── 로그인 ────────────────────────────────────────────────────────────────────
async def do_login(page: Page) -> bool:
    await page.goto(LOGIN_URL, wait_until="networkidle")
    await page.wait_for_timeout(1000)
    print(f"  로그인 페이지 URL: {page.url}")

    filled = False
    for sel in [
        'input[name="email"]', 'input[type="email"]',
        'input[name="loginId"]', 'input[name="userId"]',
        'input[name="id"]', '#email',
    ]:
        loc = page.locator(sel)
        if await loc.count():
            await loc.first.fill(ADMIN_EMAIL)
            filled = True
            print(f"  이메일 입력: {sel}")
            break

    if not filled:
        print("  [ERROR] 이메일 입력 필드를 찾지 못함")
        return False

    pw_loc = page.locator('input[name="userPassword"], input[type="password"]')
    if not await pw_loc.count():
        print("  [ERROR] 비밀번호 입력 필드를 찾지 못함")
        return False
    await pw_loc.first.fill(ADMIN_PW)

    # 제출: 버튼 클릭 우선, fallback Enter
    submitted = False
    for sel in [
        'button[type="submit"]', 'button:has-text("로그인")',
        'button:has-text("LOGIN")', 'input[type="submit"]',
    ]:
        btn = page.locator(sel)
        if await btn.count():
            await btn.first.click()
            submitted = True
            print(f"  로그인 버튼 클릭: {sel}")
            break
    if not submitted:
        await page.keyboard.press("Enter")
        print("  Enter 키로 제출")

    # 페이지 전환 대기 (최대 10초)
    try:
        await page.wait_for_url(lambda url: "login" not in url, timeout=10_000)
    except Exception:
        pass
    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(2000)

    print(f"  로그인 후 URL: {page.url}")
    return "login" not in page.url.lower()


async def login(page: Page) -> None:
    ok = await do_login(page)
    if not ok:
        # 한 번 더 시도
        print("  로그인 재시도...")
        ok = await do_login(page)
    if not ok:
        await page.screenshot(path="debug_login_fail.png")
        raise RuntimeError(f"로그인 실패. URL: {page.url}")
    print("  로그인 완료")


# ── 커스텀 드롭다운 클릭 헬퍼 ────────────────────────────────────────────────
TRIGGER_SELECTORS = [
    "select",
    ".el-select", ".el-select__wrapper",
    ".ant-select", ".ant-select-selector",
    "[class*='select']", "[class*='Select']",
    "[class*='dropdown']", "[class*='Dropdown']",
]

OPTION_SELECTORS = [
    "option",
    ".el-select-dropdown__item", ".el-option",
    ".ant-select-item", ".ant-select-item-option",
    "li[class*='option']", "[class*='option-item']",
    "[class*='dropdown-item']", "[class*='DropdownItem']",
    "li",
]


async def _find_option_in_dom(page: Page, option_text: str):
    for sel in OPTION_SELECTORS:
        locs = page.locator(sel)
        count = await locs.count()
        for i in range(count):
            loc = locs.nth(i)
            txt = (await loc.text_content() or "").strip()
            if txt == option_text:
                return loc
    return None


async def click_option(page: Page, label_keyword: str, option_text: str) -> bool:
    # 1. native select via label
    labels = page.locator("label")
    for i in range(await labels.count()):
        lb = labels.nth(i)
        txt = (await lb.text_content() or "").strip()
        if label_keyword not in txt:
            continue
        for_id = await lb.get_attribute("for")
        if for_id:
            native = page.locator(f"#{for_id}")
            if await native.count():
                tag = await native.evaluate("el => el.tagName.toLowerCase()")
                if tag == "select":
                    opts = await native.locator("option").all_text_contents()
                    if option_text in [o.strip() for o in opts]:
                        await native.select_option(label=option_text)
                        return True
        parent = lb.locator("xpath=ancestor::*[contains(@class,'form') or contains(@class,'filter') or contains(@class,'search')][1]")
        if await parent.count():
            native = parent.first.locator("select")
            if await native.count():
                opts = await native.locator("option").all_text_contents()
                if option_text in [o.strip() for o in opts]:
                    await native.select_option(label=option_text)
                    return True

    # 2. native select fallback
    selects = page.locator("select")
    for i in range(await selects.count()):
        s = selects.nth(i)
        opts = await s.locator("option").all_text_contents()
        if option_text in [o.strip() for o in opts]:
            await s.select_option(label=option_text)
            return True

    # 3. custom dropdown via label proximity
    for i in range(await labels.count()):
        lb = labels.nth(i)
        txt = (await lb.text_content() or "").strip()
        if label_keyword not in txt:
            continue
        for ancestor_level in range(1, 5):
            xpath = "xpath=ancestor::*[" + str(ancestor_level) + "]"
            container = lb.locator(xpath)
            if not await container.count():
                continue
            container = container.first
            for trig_sel in TRIGGER_SELECTORS:
                triggers = container.locator(trig_sel)
                for j in range(await triggers.count()):
                    trig = triggers.nth(j)
                    try:
                        await trig.click(timeout=2000)
                        await page.wait_for_timeout(400)
                        opt = await _find_option_in_dom(page, option_text)
                        if opt:
                            await opt.click()
                            return True
                        await page.keyboard.press("Escape")
                    except Exception:
                        pass

    # 4. global fallback
    for trig_sel in TRIGGER_SELECTORS:
        triggers = page.locator(trig_sel)
        for j in range(await triggers.count()):
            trig = triggers.nth(j)
            try:
                await trig.click(timeout=2000)
                await page.wait_for_timeout(400)
                opt = await _find_option_in_dom(page, option_text)
                if opt:
                    await opt.click()
                    return True
                await page.keyboard.press("Escape")
            except Exception:
                pass

    return False


# ── 고객사 목록 수집 ───────────────────────────────────────────────────────────
async def get_customer_list(page: Page) -> list[dict]:
    selects = page.locator("select")
    for i in range(await selects.count()):
        s = selects.nth(i)
        opts = await s.locator("option").all()
        texts = []
        for opt in opts:
            v = (await opt.get_attribute("value") or "").strip()
            t = (await opt.text_content() or "").strip()
            if v and v not in ("", "0", "all", "ALL"):
                texts.append({"value": v, "text": t, "type": "native", "index": i})
        if len(texts) > 1:
            return texts

    labels = page.locator("label")
    for i in range(await labels.count()):
        lb = labels.nth(i)
        txt = (await lb.text_content() or "").strip()
        if "고객사" not in txt:
            continue
        for ancestor_level in range(1, 5):
            xpath = "xpath=ancestor::*[" + str(ancestor_level) + "]"
            container = lb.locator(xpath)
            if not await container.count():
                continue
            container = container.first
            for trig_sel in TRIGGER_SELECTORS:
                trig = container.locator(trig_sel).first
                if not await trig.count():
                    continue
                try:
                    await trig.click(timeout=2000)
                    await page.wait_for_timeout(500)
                    result = []
                    for opt_sel in OPTION_SELECTORS:
                        opts = page.locator(opt_sel)
                        cnt = await opts.count()
                        if cnt > 1:
                            for j in range(cnt):
                                opt_txt = (await opts.nth(j).text_content() or "").strip()
                                if opt_txt and opt_txt not in ("전체", "선택", "", "고객사"):
                                    result.append({"value": opt_txt, "text": opt_txt, "type": "custom"})
                            if result:
                                await page.keyboard.press("Escape")
                                return result
                    await page.keyboard.press("Escape")
                except Exception:
                    pass
    return []


# ── 날짜 설정 ─────────────────────────────────────────────────────────────────
async def set_date_range(page: Page, date_str: str) -> None:
    date_inputs = page.locator('input[type="date"]')
    n = await date_inputs.count()
    if n >= 2:
        await date_inputs.nth(0).fill(date_str)
        await date_inputs.nth(1).fill(date_str)
        return
    if n == 1:
        await date_inputs.nth(0).fill(date_str)
        return

    date_inputs2 = page.locator(
        'input[placeholder*="날짜"], input[placeholder*="date"],'
        'input[class*="date"], input[id*="date"]'
    )
    n2 = await date_inputs2.count()
    for i in range(min(n2, 2)):
        inp = date_inputs2.nth(i)
        await inp.triple_click()
        await inp.fill(date_str)
        await inp.press("Tab")


# ── 조회 버튼 ────────────────────────────────────────────────────────────────
async def click_search(page: Page) -> None:
    for sel in [
        'button:has-text("조회")', 'a:has-text("조회")',
        'button:has-text("검색")', 'input[value="조회"]',
        'button[type="submit"]',
    ]:
        btn = page.locator(sel)
        if await btn.count():
            await btn.first.click()
            return
    raise RuntimeError("조회 버튼을 찾을 수 없습니다.")


async def get_total_count(page: Page) -> int:
    try:
        await page.wait_for_selector(".emph_g", timeout=10_000)
        text = await page.locator(".emph_g").first.text_content() or "0"
        return int(re.sub(r"[^\d]", "", text))
    except Exception:
        return 0


# ── 디버그 스냅샷 ─────────────────────────────────────────────────────────────
async def debug_snapshot(page: Page, tag: str) -> None:
    await page.screenshot(path=f"debug_{tag}.png", full_page=False)
    print(f"[DEBUG:{tag}] URL={page.url}")
    print(f"[DEBUG:{tag}] title={await page.title()}")
    html = await page.evaluate("""() => {
        const candidates = [
            document.querySelector('form'),
            document.querySelector('.filter'),
            document.querySelector('.search-wrap'),
            document.querySelector('.el-form'),
            document.querySelector('main'),
            document.body,
        ];
        const el = candidates.find(e => e);
        return el ? el.innerHTML.slice(0, 4000) : '';
    }""")
    print(f"[DEBUG:{tag}] HTML(4000):\n{html}")


# ── 통계 페이지 이동 (SPA 내부 라우팅 우선) ──────────────────────────────────
async def navigate_to_stats(page: Page) -> None:
    # SPA 내부 링크 클릭 시도
    for sel in [
        'a[href*="statistics"]', 'a:has-text("통계")',
        'a:has-text("실시간")', '[class*="nav"] a[href*="stat"]',
    ]:
        links = page.locator(sel)
        if await links.count():
            try:
                await links.first.click()
                await page.wait_for_load_state("networkidle")
                await page.wait_for_timeout(1000)
                if "login" not in page.url.lower():
                    print(f"  SPA 링크로 이동 성공: {page.url}")
                    return
            except Exception:
                pass

    # 직접 이동
    await page.goto(STATS_URL, wait_until="networkidle")
    await page.wait_for_timeout(2000)
    print(f"  goto 이동 후 URL: {page.url}")

    # 세션 만료 시 재로그인 후 재시도
    if "login" in page.url.lower():
        print("  세션 만료 감지, 재로그인...")
        await login(page)
        await page.goto(STATS_URL, wait_until="networkidle")
        await page.wait_for_timeout(2000)
        if "login" in page.url.lower():
            raise RuntimeError(f"재로그인 후에도 통계 페이지 접근 불가: {page.url}")


# ── 수집 메인 로직 ────────────────────────────────────────────────────────────
async def collect_all(page: Page, yesterday: str) -> list[dict]:
    await navigate_to_stats(page)
    await debug_snapshot(page, "stats_page")
    ok = await click_option(page, "메시지 유형", "알림톡")
    if not ok:
        raise RuntimeError("'메시지 유형' 알림톡 선택 실패")
    await page.wait_for_timeout(500)

    customers = await get_customer_list(page)
    if not customers:
        raise RuntimeError("고객사 목록을 찾을 수 없습니다.")
    print(f"  고객사 {len(customers)}개: {[c['text'] for c in customers]}")

    results: list[dict] = []

    for customer in customers:
        row = {"date": yesterday, "customer": customer["text"], "total": 0, "success": 0}
        print(f"  처리 중: {customer['text']}")

        for success_type in ["선택", "성공"]:
            ok = await click_option(page, "메시지 유형", "알림톡")
            if not ok:
                raise RuntimeError(f"{customer['text']} 처리 중 '메시지 유형=알림톡' 재선택 실패")
            await page.wait_for_timeout(200)

            ctype = customer.get("type", "native")
            if ctype == "native":
                customer_index = customer.get("index")
                if customer_index is None:
                    raise RuntimeError(f"{customer['text']} 고객사 select index 누락")
                selects = page.locator("select")
                if await selects.count() <= customer_index:
                    raise RuntimeError(f"{customer['text']} 고객사 select index 범위 초과")
                await selects.nth(customer_index).select_option(value=customer["value"])
            else:
                ok = await click_option(page, "고객사", customer["text"])
                if not ok:
                    raise RuntimeError(f"{customer['text']} 고객사 선택 실패")
            await page.wait_for_timeout(200)

            await set_date_range(page, yesterday)
            await page.wait_for_timeout(200)

            ok = await click_option(page, "성공여부", success_type)
            if not ok:
                raise RuntimeError(f"{customer['text']} 처리 중 성공여부 '{success_type}' 선택 실패")
            await page.wait_for_timeout(200)

            await click_search(page)
            await page.wait_for_load_state("networkidle")
            await page.wait_for_timeout(800)

            count = await get_total_count(page)

            if success_type == "선택":
                row["total"] = count
                print(f"    발송: {count:,}")
            else:
                row["success"] = count
                print(f"    성공: {count:,}")

        results.append(row)

    return results


# ── 진입점 ────────────────────────────────────────────────────────────────────
async def async_main() -> None:
    yesterday = resolve_target_date()
    source = "TARGET_DATE" if TARGET_DATE else "KST 전일 자동 계산"
    print(f"[UMS 통계 수집] 대상 일자: {yesterday} ({source})")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx  = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = await ctx.new_page()
        try:
            print("로그인 중...")
            await login(page)

            print("통계 수집 중...")
            data = await collect_all(page, yesterday)
        finally:
            await browser.close()

    ws   = get_worksheet()
    rows = []
    for d in data:
        total   = d["total"]
        success = d["success"]
        rate    = f"{success / total * 100:.1f}%" if total > 0 else "0.0%"
        rows.append([d["date"], d["customer"], total, success, rate])

    if rows:
        upsert_rows(ws, rows)
        print(f"[완료] {len(rows)}행 Google Sheets 업서트 완료")
    else:
        print("[완료] 적재할 데이터 없음")


if __name__ == "__main__":
    asyncio.run(async_main())
