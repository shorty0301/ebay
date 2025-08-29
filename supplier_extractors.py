# supplier_extractors.py
# -*- coding: utf-8 -*-
"""
各ECサイトのHTMLから 在庫 / 数量 / 価格 を抽出するヘルパ
GAS版からPython移植
"""

import re, json, functools, requests
from typing import Dict, Any
from bs4 import BeautifulSoup

# ========= 共通ユーティリティ ==========
def strip_tags(s: str) -> str:
    if not s: return ""
    soup = BeautifulSoup(s, "html.parser")
    t = soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", t.replace("\u00A0"," ").replace("\u202F"," ").replace("\u2009"," ")).strip()

def z2h_digits(s: str) -> str:
    Z="０１２３４５６７８９"; H="0123456789"
    return re.sub(r"[０-９]", lambda m: H[Z.index(m.group(0))], s or "")

def parse_yen_strict(raw: str) -> float:
    s = str(raw or "")
    if not re.search(r"[¥￥円,，]", s): return float("nan")
    t = re.sub(r"[^\d.]", "", z2h_digits(s))
    try:
        n = float(t)
        if 0 < n < 1e7: return n
    except: pass
    return float("nan")

def pick_best_price(cands) -> float:
    nums=[]
    for s in cands:
        s=str(s)
        n = parse_yen_strict(s) if re.search(r"[¥￥円,，]", s) else float(re.sub(r"[^\d]", "", z2h_digits(s)) or "nan")
        if n==n and 0<n<1e7: nums.append(n)
    return min(nums) if nums else float("nan")

# ========== fetch_html ==========
def fetch_html(url: str) -> str:
    ua_pc  = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36"
    ua_sp  = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
    headers = lambda ua: {"User-Agent": ua, "Accept-Language": "ja,en;q=0.8"}

    def try_get(u, ua):
        try:
            r=requests.get(u, headers=headers(ua), timeout=20)
            if r.status_code==200: return r.text
        except: return ""
        return ""

    html_pc = try_get(url, ua_pc)
    html_mb = try_get(url, ua_sp)
    return (html_pc or "") + "\n<!-- MOBILE MERGE -->\n" + (html_mb or "")

# ========== サイト別価格抽出 ==========
def price_from_rakuma(html, text): return pick_best_price(re.findall(r"[¥￥]?\s?\d{1,3}(?:[,，]\d{3})+", text))
def price_from_offmall(html, text): return pick_best_price(re.findall(r"[¥￥]?\s?\d{1,3}(?:[,，]\d{3})+", text))
def price_from_surugaya(html, text): return pick_best_price(re.findall(r"[¥￥]?\s?\d{1,3}(?:[,，]\d{3})+", text))

# ========== 在庫判定 ==========
def extract_supplier_info(url: str, html: str, debug: bool = False) -> Dict[str, Any]:
    """
    stock: IN_STOCK / OUT_OF_STOCK / LAST_ONE / UNKNOWN
    qty  : "1" など（あれば）
    price: int | None
    """
    # --- 共通下ごしらえ ---
    host = re.sub(r"^www\.", "", re.findall(r"https?://([^/]+)/?", url)[0]).lower() if url else ""
    text = strip_tags(html)

    # ★ここで必ず初期化（これが無いと NameError になる）
    stock = "UNKNOWN"
    qty   = ""
    price = None

    if debug:
        print(f"[DEBUG] host={host}")
        print(f"[DEBUG] snippet={text[:160]}")

    # --- 在庫判定 ---
    # 「残り n 点」
    m = re.search(r"残り\s*([0-9０-９]+)\s*(?:点|個|枚|本)", text)
    if m:
        try:
            n = int(z2h_digits(m.group(1)))
            qty = str(n)
            stock = "LAST_ONE" if n == 1 else "IN_STOCK"
        except Exception:
            pass
    else:
        if re.search(r"(売り切れ|在庫切れ|SOLD\s*OUT|販売終了|取扱い終了|販売停止)", text, re.I):
            stock = "OUT_OF_STOCK"
        elif re.search(r"(在庫あり|通常.*発送|即日発送|お取り寄せ|カートに入れる|購入手続き|今すぐ購入)", text, re.I):
            stock = "IN_STOCK"

    # --- 価格抽出（汎用・送料/ポイント等の文脈は除外） ---
    stop = re.compile(r"(送料|配送料|手数料|ポイント|付与|獲得|実質|クーポン|割引|値引|上限|注文合計|代引|着払い)", re.I)
    hits = list(re.finditer(r"[¥￥]?\s?\d{1,3}(?:[,，]\d{3})+|\b\d{3,7}\b", text))
    prices: List[int] = []
    for h in hits:
        s = h.group(0)
        i = h.start()
        ctx = text[max(0, i-15): min(len(text), i+len(s)+15)]
        if stop.search(ctx):
            continue
        n = yen_to_int(s)
        if n and 0 < n < 10_000_000:
            prices.append(n)
    if prices:
        price = min(prices)

    return {
        "stock": stock,
        "qty": qty,
        "price": price,
        "_debug": {
            "host": host,
            "text_snippet": text[:200],
        },
    }
# ========== キャッシュ付き ==========
@functools.lru_cache(maxsize=256)
def fetch_and_extract(url: str) -> Dict[str, Any]:
    return extract_supplier_info(url, fetch_html(url))

def extract_supplier_info(url: str, html: str, debug: bool = False):
    # …既存処理…
    if debug:
        print(f"[DEBUG] Extracting {url} ...")
    return {
            "stock": stock,
            "qty": qty,
            "price": price,
            "_debug": {
                "host": host,
                "text_snippet": text[:200]
            }
     }
