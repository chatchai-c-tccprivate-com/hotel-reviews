#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Google Maps Reviews Scraper (ฟรี, รันบน GitHub Actions / Windows server)
------------------------------------------------------------------------
อ่านรายชื่อโรงแรมจาก hotels.csv → เปิด Google Maps → ดึงรีวิว →
เขียนผลลง data/summary.csv (สรุปต่อโรงแรม) และ data/reviews.csv (ทุกรีวิว, กันซ้ำ)

โหมด:
  ปกติ (รายสัปดาห์):  python scrape_reviews.py --max-reviews 40
  Backfill รอบแรก :   python scrape_reviews.py --max-reviews 1000

หมายเหตุสำคัญ: Google เปลี่ยนหน้าเว็บ/คลาส CSS เป็นระยะ ถ้าดึงไม่ได้
ให้ปรับ SELECTORS ด้านล่าง (จุดเดียวที่ต้องแก้เวลาพัง)
"""

import csv, os, sys, time, random, hashlib, argparse, datetime, re
from playwright.sync_api import sync_playwright

DATA_DIR = "data"
SUMMARY_CSV = os.path.join(DATA_DIR, "summary.csv")
REVIEWS_CSV = os.path.join(DATA_DIR, "reviews.csv")

# ---- จุดเดียวที่ต้องแก้ถ้า Google เปลี่ยนหน้า ----
SELECTORS = {
    "consent_btn": 'button[aria-label*="Accept"], button[aria-label*="ยอมรับ"], form[action*="consent"] button',
    "result_link": 'a.hfpxzc',                 # ลิงก์ผลลัพธ์แรกในหน้า search
    "reviews_tab": 'button[aria-label*="Reviews"], button[aria-label*="รีวิว"]',
    "sort_btn":    'button[aria-label*="Sort"], button[aria-label*="จัดเรียง"]',
    "scroll_pane": 'div[role="main"] div.m6QErb[aria-label], div.m6QErb.DxyBCb',
    "review_card": 'div.jftiEf',
    "author":      '.d4r55',
    "rating_aria": '.kvMYJc',                  # aria-label เช่น "5 ดาว"
    "rel_time":    '.rsqaWe',
    "text":        '.wiI7pd',
    "more_btn":    'button.w8nwRe',            # ปุ่ม "เพิ่มเติม/More" ในรีวิวยาว
}
# -------------------------------------------------


def log(msg):
    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def sleep(a=0.6, b=1.4):
    time.sleep(random.uniform(a, b))


def load_hotels(path="hotels.csv"):
    rows = []
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r.get("SearchName"):
                rows.append(r)
    return rows


def load_existing_keys():
    """โหลดคีย์รีวิวเดิม เพื่อกันซ้ำ และเก็บแถวเดิมไว้เขียนกลับ"""
    keys, rows = set(), []
    if os.path.exists(REVIEWS_CSV):
        with open(REVIEWS_CSV, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows.append(r)
                keys.add(r["ReviewKey"])
    return keys, rows


def review_key(code, author, rel, text):
    h = hashlib.md5((author + "|" + rel + "|" + text[:60]).encode("utf-8")).hexdigest()[:10]
    return f"{code}|{h}"


def accept_consent(page):
    try:
        btn = page.query_selector(SELECTORS["consent_btn"])
        if btn:
            btn.click()
            sleep()
    except Exception:
        pass


def open_hotel(page, query):
    page.goto("https://www.google.com/maps/search/" + query.replace(" ", "+"),
              wait_until="domcontentloaded", timeout=60000)
    sleep(1.5, 2.5)
    accept_consent(page)
    # ถ้าเป็นหน้า list ให้คลิกผลแรก; ถ้าเด้งเข้า place เลยก็ข้าม
    link = page.query_selector(SELECTORS["result_link"])
    if link:
        link.click()
        sleep(1.5, 2.5)


def open_reviews_tab(page):
    tab = page.query_selector(SELECTORS["reviews_tab"])
    if tab:
        tab.click()
        sleep(1.2, 2.0)


def sort_newest(page):
    try:
        page.query_selector(SELECTORS["sort_btn"]).click()
        sleep()
        # เมนูจัดเรียง: เลือกรายการ "ใหม่สุด/Newest" (มักเป็นตัวที่ 2)
        items = page.query_selector_all('div[role="menuitemradio"], div[role="menuitem"]')
        for it in items:
            t = (it.inner_text() or "").lower()
            if "newest" in t or "ใหม่" in t:
                it.click(); break
        sleep(1.0, 1.6)
    except Exception:
        pass


def get_place_meta(page):
    rating, total = "", ""
    try:
        el = page.query_selector('div.jANrlb div.fontDisplayLarge, div.F7nice span[aria-hidden="true"]')
        if el: rating = (el.inner_text() or "").strip()
    except Exception:
        pass
    try:
        el = page.query_selector('div.F7nice span[aria-label*="review"], button[aria-label*="reviews"]')
        if el:
            import re
            m = re.search(r"([\d,]+)", el.get_attribute("aria-label") or el.inner_text() or "")
            if m: total = m.group(1).replace(",", "")
    except Exception:
        pass
    return rating, total


def scroll_pane(page):
    """เลื่อนหน้ารีวิวแบบ re-query ทุกครั้ง กัน handle หลุด (เคยทำ IPL พลาด)"""
    try:
        pane = page.query_selector(SELECTORS["scroll_pane"])
        if pane:
            box = pane.bounding_box()
            if box:
                page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                page.mouse.wheel(0, 3000)
                return
            page.evaluate("(el)=>el.scrollBy(0, el.scrollHeight)", pane)
            return
    except Exception:
        pass
    try:
        page.keyboard.press("PageDown")
    except Exception:
        pass


def card_rating(card):
    """ดึงดาวของรีวิว — ลองหลาย selector เพราะ Google มีหลายเลย์เอาต์ (จุดที่ต้องแก้ถ้าดาวยังว่าง)"""
    sels = ['span.kvMYJc', 'span[role="img"][aria-label]',
            'g-review-stars span', '[aria-label*="ดาว"]',
            '[aria-label*="star"]', '[aria-label*="Star"]']
    for sel in sels:
        try:
            el = card.query_selector(sel)
        except Exception:
            el = None
        if el:
            lab = el.get_attribute("aria-label") or ""
            m = re.search(r'([0-5](?:[.,]\d)?)', lab)
            if m:
                return m.group(1).replace(",", ".")
    return ""


def scrape_reviews(page, code, max_reviews):
    seen_cards, collected = 0, []
    stagnant = 0
    while len(collected) < max_reviews and stagnant < 6:
        cards = page.query_selector_all(SELECTORS["review_card"])
        # กดปุ่ม "เพิ่มเติม" เพื่อขยายข้อความเต็ม
        for b in page.query_selector_all(SELECTORS["more_btn"])[:20]:
            try: b.click()
            except Exception: pass
        for card in cards[seen_cards:]:
            try:
                author = (card.query_selector(SELECTORS["author"]).inner_text() or "").strip()
            except Exception:
                author = ""
            rating = card_rating(card)
            try:
                rel = (card.query_selector(SELECTORS["rel_time"]).inner_text() or "").strip()
            except Exception:
                rel = ""
            try:
                text = (card.query_selector(SELECTORS["text"]).inner_text() or "").strip()
            except Exception:
                text = ""
            if author or text:
                collected.append({"author": author, "rating": rating, "relative": rel, "text": text})
        new_count = len(cards)
        if new_count == seen_cards:
            stagnant += 1
        else:
            stagnant = 0
        seen_cards = new_count
        scroll_pane(page)
        sleep(0.8, 1.6)
    return collected[:max_reviews]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-reviews", type=int, default=40, help="จำนวนรีวิวสูงสุด/โรงแรม")
    ap.add_argument("--headful", action="store_true", help="เปิดเบราว์เซอร์ให้เห็น (debug)")
    args = ap.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)
    hotels = load_hotels()
    seen_keys, existing_rows = load_existing_keys()
    log(f"โรงแรม {len(hotels)} แห่ง • รีวิวเดิมในคลัง {len(existing_rows)} แถว • max/โรงแรม={args.max_reviews}")

    now_iso = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    summary_rows, new_review_rows = [], []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headful,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(locale="th-TH",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            viewport={"width": 1280, "height": 900})
        page = ctx.new_page()

        for h in hotels:
            code, name, loc = h["Code"], h["SearchName"], h.get("Location", "")
            q = f"{name} {loc}".strip()
            try:
                log(f"→ {code}: {name}")
                open_hotel(page, q)
                rating, total = get_place_meta(page)
                open_reviews_tab(page)
                sort_newest(page)
                revs = scrape_reviews(page, code, args.max_reviews)

                added = 0
                for r in revs:
                    k = review_key(code, r["author"], r["relative"], r["text"])
                    if k in seen_keys:
                        continue
                    seen_keys.add(k)
                    new_review_rows.append({
                        "ReviewKey": k, "CheckedAt": now_iso, "Code": code, "Hotel": name,
                        "Author": r["author"], "Rating": r["rating"],
                        "RelativeTime": r["relative"], "Text": r["text"].replace("\n", " ").strip()
                    })
                    added += 1
                latest = revs[0]["text"][:300] if revs else ""
                summary_rows.append({
                    "Code": code, "Hotel": name, "Location": loc,
                    "Rating": rating, "TotalReviews": total,
                    "ReviewsScraped": len(revs), "NewThisRun": added,
                    "LatestReview": latest.replace("\n", " "), "LastUpdated": now_iso
                })
                log(f"   rating={rating} total={total} scraped={len(revs)} new={added}")
            except Exception as e:
                log(f"   ⚠️ พลาด {code}: {e}")
                summary_rows.append({"Code": code, "Hotel": name, "Location": loc, "Rating": "",
                    "TotalReviews": "", "ReviewsScraped": 0, "NewThisRun": 0,
                    "LatestReview": f"ERROR: {e}", "LastUpdated": now_iso})
            sleep(2.0, 4.0)  # หน่วงระหว่างโรงแรม ลดโอกาสโดนบล็อก

        browser.close()

    with open(SUMMARY_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["Code", "Hotel", "Location", "Rating", "TotalReviews",
            "ReviewsScraped", "NewThisRun", "LatestReview", "LastUpdated"])
        w.writeheader(); w.writerows(summary_rows)

    all_rows = existing_rows + new_review_rows
    with open(REVIEWS_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["ReviewKey", "CheckedAt", "Code", "Hotel",
            "Author", "Rating", "RelativeTime", "Text"])
        w.writeheader(); w.writerows(all_rows)

    log(f"เสร็จ • รีวิวใหม่รอบนี้ {len(new_review_rows)} • คลังรวม {len(all_rows)}")


if __name__ == "__main__":
    main()
