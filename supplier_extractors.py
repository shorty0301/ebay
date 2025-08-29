# supplier_extractors.py
# -*- coding: utf-8 -*-
"""
各ECサイトのHTMLから 在庫 / 数量 / 価格 を抽出するヘルパ
GAS版からPython移植
"""

import re, json, functools, requests
from typing import Dict, Any
from bs4 import BeautifulSoup

# ======== 共通ユーティリティ ==========
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

def yen_to_int(s: str) -> int | None:
    """
    金額文字列を整数に変換（全角→半角、数字以外を除去）
    """
    t = re.sub(r"[^\d]", "", z2h_digits(str(s or "")))
    return int(t) if t else None

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

# ========== 在庫・価格 抽出のメイン ==========
from typing import Dict, Any
import re

def extract_supplier_info(url: str, html: str, debug: bool = False) -> Dict[str, Any]:
    """
    戻り値:
      {
        "stock": "IN_STOCK|OUT_OF_STOCK|LAST_ONE|UNKNOWN",
        "qty":   "数字文字列 or ''",
        "price": int or None,
        "_debug": {"host": "...", "text_snippet": "..."}  # debug時のみ
      }
    """
    # ★ここが肝：最初にデフォルト値を用意（早期returnは禁止）
    stock: str = "UNKNOWN"
    qty:   str = ""
    price: Any = None

    host = re.findall(r"https?://([^/]+)/?", url)[0].lower()
    text = strip_tags(html).replace("\u3000", " ").replace("\u00A0", " ")

    if debug:
        print(f"[DEBUG] host={host}")

    # 残り数量 → LAST_ONE/IN_STOCK に上書き
    m = re.search(r"残り\s*([0-9０-９]+)\s*(?:点|個|枚|本)", text)
    if m:
        n = int(z2h_digits(m.group(1)))
        qty = str(n)
        stock = "LAST_ONE" if n == 1 else "IN_STOCK"

    # 強い在庫ワード
    if re.search(r"(売り切れ|在庫切れ|完売|販売終了|取扱(い)?終了|SOLD\s*OUT)", text, re.I):
        stock = "OUT_OF_STOCK"
    elif re.search(r"(在庫あり|購入手続き|今すぐ購入|カートに入れる|即日発送)", text, re.I):
        if stock != "LAST_ONE":
            stock = "IN_STOCK"
            
    # 価格抽出（3桁価格も許容。ただし文脈で絞り込み）
    STOP = re.compile(r"(ポイント|pt|付与|獲得|還元|実質|送料|手数料|上限|クーポン|値引|割引|合計\s*\d+|合計金額ではない|%|％)", re.I)
    UNIT_NOISE = re.compile(r"(個|点|件|cm|mm|g|kg|W|V|GB|MB|TB|時間|日|年|サイズ|型番|JAN|品番)", re.I)
    PRICE_KEY = re.compile(r"(価格|税込|税抜|販売|支払|お支払い|お買い上げ|円|¥|￥)", re.I)

    def iter_numbers_with_ctx(txt: str):
        # ¥12,345 / 12,345円 / ￥999 などの「通貨コンテキストあり」を優先
        pat_money = re.compile(r"(?:[¥￥]\s*\d{1,3}(?:[,，]\d{3})+|\d{1,3}(?:[,，]\d{3})+\s*円|[¥￥]\s*\d{3,7}|\d{3,7}\s*円)")
        for m in pat_money.finditer(txt):
            yield m

        # 裸数字も拾う（3〜7桁）。ただし文脈チェック必須。
        pat_bare = re.compile(r"\b\d{3,7}\b")
        for m in pat_bare.finditer(txt):
            yield m

    price_cands = []  # (score, value)
    for m in iter_numbers_with_ctx(text):
        h = m.group(0)
        i = m.start()
        ctx = text[max(0, i-24): i+len(h)+24]

        # 数値へ変換（全角対応）
        n = parse_yen_strict(h)
        if n != n:  # NaN
            # 裸数字は parse_yen_strict だとNaNになりやすいので素直に整数化
            t = re.sub(r"[^\d]", "", z2h_digits(h))
            n = float(t) if t else float("nan")
        if n != n or not (0 < n < 10_000_000):
            continue
        v = int(n)

        # 404/200/302などのHTTPコードやエラーコードっぽい数字は除外（通貨記号や円が近傍にない場合）
        if v in (100,101,200,201,202,204,301,302,303,304,307,308,400,401,403,404,408,500,502,503,504) and not PRICE_KEY.search(ctx):
            continue

        # ノイズ（ポイント・単位・送料表現など）の近傍は除外
        if STOP.search(ctx) or UNIT_NOISE.search(ctx):
            continue

        # スコアリング
        score = 0
        # 通貨記号/円の明示 → 強く加点
        if re.search(r"[¥￥]|円", h) or re.search(r"[¥￥]|円", ctx):
            score += 3
        # 価格キーワード近傍
        if PRICE_KEY.search(ctx):
            score += 2
        # カンマフォーマット（12,345）っぽい → 価格らしさ+1
        if re.search(r"\d{1,3}(?:[,，]\d{3})+", h):
            score += 1
        # 3桁は誤検出が多いので、通貨/価格キーワードの裏付けが無ければ減点
        if re.fullmatch(r"\d{3}", re.sub(r"[^\d]", "", h)) and score < 3:
            continue

        price_cands.append((score, v))

    if price_cands:
        best_score = max(s for s, _ in price_cands)
        price = min(v for s, v in price_cands if s == best_score)

    out = {"stock": stock, "qty": qty, "price": price}
    if debug:
        out["_debug"] = {"host": host, "text_snippet": text[:200]}
    return out

# ========== キャッシュ付き ==========
@functools.lru_cache(maxsize=256)
def fetch_and_extract(url: str) -> Dict[str, Any]:
    return extract_supplier_info(url, fetch_html(url))
