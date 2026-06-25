#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Google Maps Reviews Scraper (ฟรี, รันบน GitHub Actions / Windows server)
เวอร์ชันปรับปรุง: ดึง "ดาวรายรีวิว" ด้วยหลายกลยุทธ์ + มีโหมด DEBUG
------------------------------------------------------------------------
อ่าน hotels.csv -> เปิด Google Maps -> ดึงรีวิว -> เขียน data/summary.csv + data/reviews.csv (กันซ้ำ)

โหมด:
  ปกติ:      python scrape_reviews.py --max-reviews 40
  Backfill:  python scrape_reviews.py --max-reviews 300
  Debug:     python scrape_reviews.py --max-reviews 20 --debug
             (จะ log โครงสร้างการ์ดรีวิวใบแรกของโรงแรมแรก -> ใช้ดูว่า selector ดาวควรเป็นอะไร)
"""

import csv, os, sys, time, random, hashlib, argparse, datetime, re
from playwright.sync_api import sync_playwright

DATA_DIR = "data"
SUMMARY_CSV = os.path.join(DATA_DIR, "summary.csv")
REVIEWS_CSV = os.path.join(DATA_DIR, "reviews.csv")

# ---- selector หลัก (จุดเดียวที่ต้องแก้ถ้า Google เปลี่ยนหน้า) ----
SELECTORS = {
    "consent_btn": 'button[aria-label*="Accept"], button[aria-label*="ยอมรับ"], form[action*="consent"] button',
    "result_link": 'a.hfpxzc',
    "reviews_tab": 'button[aria-label*="Reviews"], button[aria-label*="รีวิว"], button[role="tab"]',
    "sort_btn":    'button[aria-label*="Sort"], button[aria-label*="จัดเรียง"], button[data-value="Sort"]',
    "review_card": 'div.jftiEf, div[data-review-id], div.gws-localreviews__google-review',
    "author":      '.d4r55, .TSUbDb',
    "rel_time":    '.rsqaWe, .dehysf',
    "text":        '.wiI7pd, .MyEned, .review-full-text',
    "more_btn":    'button.w8nwRe, button[aria-label*="More"], button[jsaction*="review.expandReview"]',
    "scroll_pane": 'div[role="main"] div.m6QErb[aria-label], div.m6QErb.DxyBCb, div.dS8AEf',
}

# regex จับ "เลข 1-5 ที่ตามด้วยคำว่า ดาว/star//5/out of 5/จาก 5"
RATING_RE = re.compile(r'([0-5](?:[.,]\d)?)\s*(?:★|stars?|ดาว|/\s*5|out of\s*5|จาก\s*5|of\s*5)', re.IGNORECASE)
# กรณี aria-label เป็นตัวเลขล้วน เช่น "5.0"
NUM_ONLY_RE = re.compile(r'^\s*([0-5](?:[.,]\d)?)\s*$')


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
            btn.click(); sleep()
    except Exception:
        pass


def open_hotel(page, query):
    page.goto("https://www.google.com/maps/search/" + query.replace(" ", "+"),
              wait_until="domcontentloaded", timeout=60000)
    sleep(1.6, 2.6)
    accept_consent(page)
    link = page.query_selector(SELECTORS["result_link"])
    if link:
        link.click(); sleep(1.6, 2.6)


def open_reviews_tab(page):
    # ลองปุ่ม/แท็บที่มีคำว่า Reviews / รีวิว
    for b in page.query_selector_all('button, [role="tab"]'):
        try:
            lab = (b.get_attribute("aria-label") or "") + "|" + (b.inner_text() or "")
        except Exception:
            lab = ""
        if re.search(r'reviews|รีวิว', lab, re.IGNORECASE):
            try:
                b.click(); sleep(1.4, 2.2); return True
            except Exception:
                pass
    return False


def sort_newest(page):
    try:
        page.query_selector(SELECTORS["sort_btn"]).click()
        sleep()
        for it in page.query_selector_all('div[role="menuitemradio"], div[role="menuitem"], li[role="menuitemradio"]'):
            t = (it.inner_text() or "").lower()
            if "newest" in t or "ใหม่" in t or "latest" in t:
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
            m = re.search(r"([\d,]+)", el.get_attribute("aria-label") or el.inner_text() or "")
            if m: total = m.group(1).replace(",", "")
    except Exception:
        pass
    return rating, total


def scroll_pane(page):
    try:
        pane = page.query_selector(SELECTORS["scroll_pane"])
        if pane:
            box = pane.bounding_box()
            if box:
                page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                page.mouse.wheel(0, 3000); return
            page.evaluate("(el)=>el.scrollBy(0, el.scrollHeight)", pane); return
    except Exception:
        pass
    try:
        page.keyboard.press("PageDown")
    except Exception:
        pass


def card_rating(card):
    """ดึงดาวของรีวิวด้วยหลายกลยุทธ์ (ภาษาไหนก็ได้)"""
    # 1) element ที่น่าจะเป็นดาว (role=img / คลาส kvMYJc / aria-label มีคำว่า star/ดาว)
    sels = '[role="img"][aria-label], span.kvMYJc, [aria-label*="star"], [aria-label*="Star"], [aria-label*="ดาว"]'
    try:
        cands = card.query_selector_all(sels)
    except Exception:
        cands = []
    for el in cands:
        lab = (el.get_attribute("aria-label") or "").strip()
        if not lab:
            continue
        m = RATING_RE.search(lab) or NUM_ONLY_RE.match(lab)
        if m:
            return m.group(1).replace(",", ".")
    # 2) ไล่ดู aria-label ทุกตัวในการ์ด
    try:
        for el in card.query_selector_all('[aria-label]'):
            lab = (el.get_attribute("aria-label") or "").strip()
            m = RATING_RE.search(lab)
            if m:
                return m.group(1).replace(",", ".")
    except Exception:
        pass
    # 3) นับดาวที่ "ถูกเติม" (Google ใช้ span ดาวเต็ม/ว่างต่างกัน)
    try:
        filled = card.query_selector_all('span.hCCjke.google-symbols.NhBTye.elGi1d, span.elGi1d')
        if filled:
            n = len(filled)
            if 1 <= n <= 5:
                return str(n)
    except Exception:
        pass
    return ""


def debug_dump_card(card):
    """พิมพ์โครงสร้างการ์ดรีวิวใบแรก เพื่อหา selector ดาวจากระบบจริง"""
    try:
        labels = []
        for el in card.query_selector_all('[aria-label]')[:12]:
            tag = el.evaluate("e=>e.tagName.toLowerCase()")
            cls = (el.get_attribute("class") or "").split(" ")[0]
            lab = (el.get_attribute("aria-label") or "")[:40]
            labels.append(f"{tag}.{cls}[{lab}]")
        log("DEBUG aria-label els: " + " ;; ".join(labels))
        roleimgs = []
        for el in card.query_selector_all('[role="img"]')[:8]:
            cls = (el.get_attribute("class") or "").split(" ")[0]
            lab = (el.get_attribute("aria-label") or "")[:40]
            roleimgs.append(f"{cls}[{lab}]")
        log("DEBUG role=img els: " + " ;; ".join(roleimgs))
    except Exception as e:
        log("DEBUG dump error: " + str(e))


def scrape_reviews(page, code, max_reviews, debug=False):
    seen_cards, collected = 0, []
    stagnant = 0
    dumped = False
    while len(collected) < max_reviews and stagnant < 6:
        cards = page.query_selector_all(SELECTORS["review_card"])
        for b in page.query_selector_all(SELECTORS["more_btn"])[:20]:
            try: b.click()
            except Exception: pass
        if debug and not dumped and cards:
            debug_dump_card(cards[0]); dumped = True
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
        stagnant = stagnant + 1 if new_count == seen_cards else 0
        seen_cards = new_count
        scroll_pane(page)
        sleep(0.8, 1.6)
    return collected[:max_reviews]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-reviews", type=int, default=40)
    ap.add_argument("--headful", action="store_true")
    ap.add_argument("--debug", action="store_true", help="log โครงสร้างการ์ดรีวิวใบแรก")
    args = ap.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)
    hotels = load_hotels()
    seen_keys, existing_rows = load_existing_keys()
    log(f"โรงแรม {len(hotels)} แห่ง • รีวิวเดิม {len(existing_rows)} • max/โรงแรม={args.max_reviews} • debug={args.debug}")

    now_iso = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    summary_rows, new_review_rows = [], []
    stars_found_total = 0

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headful,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(locale="th-TH",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            viewport={"width": 1280, "height": 900})
        page = ctx.new_page()

        for idx, h in enumerate(hotels):
            code, name, loc = h["Code"], h["SearchName"], h.get("Location", "")
            q = f"{name} {loc}".strip()
            try:
                log(f"→ {code}: {name}")
                open_hotel(page, q)
                rating, total = get_place_meta(page)
                open_reviews_tab(page)
                sort_newest(page)
                revs = scrape_reviews(page, code, args.max_reviews, debug=(idx == 0))

                stars_here = sum(1 for r in revs if r["rating"])
                stars_found_total += stars_here
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
                log(f"   rating={rating} scraped={len(revs)} มีดาว={stars_here} new={added}")
            except Exception as e:
                log(f"   ⚠️ พลาด {code}: {e}")
                summary_rows.append({"Code": code, "Hotel": name, "Location": loc, "Rating": "",
                    "TotalReviews": "", "ReviewsScraped": 0, "NewThisRun": 0,
                    "LatestReview": f"ERROR: {e}", "LastUpdated": now_iso})
            sleep(2.0, 4.0)

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

    log(f"เสร็จ • รีวิวใหม่ {len(new_review_rows)} • รีวิวที่มีดาวรอบนี้ {stars_found_total} • คลังรวม {len(all_rows)}")


if __name__ == "__main__":
    main()
