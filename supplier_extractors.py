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

def to_int_yen(s: str) -> int | None:
    """
    金額文字列を整数（円）に変換。
    ・全角→半角
    ・カンマ/記号を除去
    ・範囲: 1〜9,999,999
    """
    t = re.sub(r"[^\d]", "", z2h_digits(str(s or "")))
    if not t:
        return None
    try:
        v = int(t)
        return v if 0 < v < 10_000_000 else None
    except:
        return None

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
def price_from_offmall(html: str, text: str) -> int | None:
    """
    HardOff NetMall（オフモール）価格抽出
    - 「販売価格 / 税込 / 価格」近傍を最優先
    - JSON/LD の "price" があれば強く採用
    - 3桁もふつうに採用（送料表記が出ない前提）
    """
    STOP = re.compile(
        r"(ポイント|pt|付与|獲得|還元|実質|送料|手数料|上限|クーポン|値引|割引|合計|参考価格|相場|%|％)",
        re.I,
    )
    PRICE_WORD = re.compile(
        r"(販売価格|税込|税抜|価格|本体価格|販売金額|セール価格|特価)",
        re.I,
    )
    OLD_PRICE_WORD = re.compile(r"(通常価格|定価|旧価格|値下げ前|参考価格)", re.I)

    def add(cands, val: int | None, score: int):
        if val and 0 < val < 10_000_000:
            cands.append((score, val))

    cands: list[tuple[int, int]] = []

    # 1) 構造化データ
    for m in re.finditer(r'"price"\s*:\s*"?(\d{3,7})"?', html):
        add(cands, to_int_yen(m.group(1)), 8)
    for m in re.finditer(r'itemprop=["\']price["\'][^>]*content=["\']?(\d{3,7})', html):
        add(cands, to_int_yen(m.group(1)), 8)
    for m in re.finditer(r'data-(?:price|amount)\s*=\s*["\']?(\d{3,7})', html, re.I):
        add(cands, to_int_yen(m.group(1)), 7)

    # 2) 画面テキスト
    pat_money = re.compile(r"([¥￥]?\s*\d{1,3}(?:[,，]\d{3})+|[¥￥]?\s*\d{3,7})(?:\s*円)?")
    for m in pat_money.finditer(text):
        s = m.group(1)
        i = m.start(1)
        ctx = text[max(0, i - 40): i + len(s) + 40]

        if STOP.search(ctx):
            continue

        v = to_int_yen(s)
        if not v:
            continue

        has_currency  = bool(re.search(r"[¥￥]|円", s) or re.search(r"[¥￥]|円", ctx))
        has_priceword = bool(PRICE_WORD.search(ctx))

        score = 0
        if has_priceword:
            score += 5
        if has_currency:
            score += 3
        if re.search(r"\d{1,3}(?:[,，]\d{3})+", s):
            score += 1
        if v >= 10_000:
            score += 1
        if OLD_PRICE_WORD.search(ctx):
            score -= 1

        add(cands, v, score)

    if not cands:
        return None

    best = max(s for s, _ in cands)
    return min(v for s, v in cands if s == best)




def price_from_rakuma(html: str, text: str) -> int | None:
    # Rakuma/Fril
    cands = []
    for m in re.finditer(r'"price"\s*:\s*"?(\d{3,7})"?', html):
        v = to_int_yen(m.group(1))
        if v: cands.append(v)
    for m in re.finditer(r'(?:[¥￥]\s*\d{3,7}|\d{1,3}(?:[,，]\d{3})+|\d{3,7})\s*円', text):
        v = to_int_yen(m.group(0))
        if v: cands.append(v)
    return min(cands) if cands else None


def price_from_surugaya(html: str, text: str) -> int | None:
    # 駿河屋
    cands = []
    for m in re.finditer(r'itemprop=["\']price["\'][^>]*content=["\']?(\d{3,7})', html):
        v = to_int_yen(m.group(1))
        if v: cands.append(v)
    for m in re.finditer(r'(?:販売価格|税込|税抜)[^0-9¥￥]{0,10}([¥￥]?\s*\d{1,3}(?:[,，]\d{3})+|[¥￥]?\s*\d{3,7})', text):
        v = to_int_yen(m.group(1))
        if v: cands.append(v)
    for m in re.finditer(r'(\d{1,3}(?:[,，]\d{3})+|\d{3,7})\s*円', text):
        v = to_int_yen(m.group(1))
        if v: cands.append(v)
    return min(cands) if cands else None

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
    # ★デフォルト値（早期return禁止）
    stock: str = "UNKNOWN"
    qty:   str = ""
    price: Any = None

    host = re.findall(r"https?://([^/]+)/?", url)[0].lower()
    text = strip_tags(html).replace("\u3000", " ").replace("\u00A0", " ")

    if debug:
        print(f"[DEBUG] host={host}")

    # 残り数量 → LAST_ONE / IN_STOCK
    m = re.search(r"残り\s*([0-9０-９]+)\s*(?:点|個|枚|本)", text)
    if m:
        n = int(z2h_digits(m.group(1)))
        qty = str(n)
        stock = "LAST_ONE" if n == 1 else "IN_STOCK"

        # --- 在庫判定（共通・スコア方式＋soldout強制） ---

    # HTMLに soldout/sold-out/sold_out があれば強制的に在庫なし寄り
    SOLDOUT_HTML = bool(re.search(r"(sold[\s_\-]?out)", html, re.I))

    # 0個系は最優先で在庫なし
    if re.search(r"(残り|在庫)\s*0\s*(?:点|個|枚|本)?", text):
        stock = "OUT_OF_STOCK"

    # ラスト1点系は強い肯定
    if re.search(r"(残り\s*1\s*(?:点|個|枚|本)|ラスト\s*1)", text):
        stock = "LAST_ONE"

    # 近傍に否定/注意語がある「売り切れ」は除外して集計
    NEG_STOP = re.compile(r"(場合|こと|可能性|恐れ|注意|お問い合わせ|ご了承ください)")
    POS_WORD = re.compile(r"(在庫あり|購入手続き|今すぐ購入|カートに入れる|ご購入|購入する|注文手続き|お買い物かご)", re.I)
    NEG_WORD = re.compile(r"(売り切れ|在庫なし|在庫切れ|完売|販売終了|取扱(?:い)?終了|SOLD\s*OUT)", re.I)

    pos_score = 0
    for m in POS_WORD.finditer(text):
        i = m.start()
        ctx = text[max(0, i-25): i+len(m.group(0))+25]
        # 「できません/不可」などの否定近傍は無効化
        if re.search(r"(できません|不可|入れられない|品切)", ctx):
            continue
        pos_score += 3

    neg_score = 0
    for m in NEG_WORD.finditer(text):
        i = m.start()
        ctx = text[max(0, i-20): i+len(m.group(0))+20]
        if NEG_STOP.search(ctx):  # 注意書きはスキップ
            continue
        neg_score += 4

    # soldout がHTMLにあれば強めに加点（事実上OUT優先）
    if SOLDOUT_HTML:
        neg_score += 6

    # 決定ロジック（LAST_ONEが最優先）
    if stock != "LAST_ONE":
        if neg_score >= 5 and pos_score < 3:
            stock = "OUT_OF_STOCK"
        elif pos_score >= 3 and neg_score < 5:
            stock = "IN_STOCK"
        else:
            # 強弱が拮抗 or どちらも弱い → 既定を維持
            if stock == "UNKNOWN" and SOLDOUT_HTML:
                stock = "OUT_OF_STOCK"



    # 価格抽出（まずサイト別 → なければ汎用）
    if ("hardoff" in host) or ("offmall" in host) or ("netmall.hardoff.co.jp" in host):
        price = price_from_offmall(html, text)
    elif ("fril" in host) or ("rakuma" in host) or ("fril.jp" in host) or ("rakuten" in host):
        price = price_from_rakuma(html, text)
    elif ("suruga-ya" in host) or ("surugaya" in host):
        price = price_from_surugaya(html, text)

    if price is None:
        # 汎用の価格抽出ロジック（3桁も許容・文脈で絞る）
        STOP = re.compile(r"(ポイント|pt|付与|獲得|還元|実質|送料|手数料|上限|クーポン|値引|割引|合計\s*\d+|合計金額ではない|%|％)", re.I)
        UNIT_NOISE = re.compile(r"(個|点|件|cm|mm|g|kg|W|V|GB|MB|TB|時間|日|年|サイズ|型番|JAN|品番)", re.I)
        PRICE_KEY = re.compile(r"(価格|税込|税抜|販売|支払|お支払い|お買い上げ|円|¥|￥)", re.I)

        def iter_numbers_with_ctx(txt: str):
            # 通貨コンテキストあり（¥12,345 / 12,345円 / ￥999 など）優先
            pat_money = re.compile(r"(?:[¥￥]\s*\d{1,3}(?:[,，]\d{3})+|\d{1,3}(?:[,，]\d{3})+\s*円|[¥￥]\s*\d{3,7}|\d{3,7}\s*円)")
            for m in pat_money.finditer(txt):
                yield m
            # 裸数字（3〜7桁）
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

            # HTTPコード等は、通貨/円の文脈が無ければ除外
            if v in (100,101,200,201,202,204,301,302,303,304,307,308,400,401,403,404,408,500,502,503,504) and not PRICE_KEY.search(ctx):
                continue
            # ノイズ語・単位の近傍は除外
            if STOP.search(ctx) or UNIT_NOISE.search(ctx):
                continue

            # スコアリング
            score = 0
            if re.search(r"[¥￥]|円", h) or re.search(r"[¥￥]|円", ctx):
                score += 3  # 通貨記号/円
            if PRICE_KEY.search(ctx):
                score += 2  # 価格キーワード
            if re.search(r"\d{1,3}(?:[,，]\d{3})+", h):
                score += 1  # カンマ区切り

            # 3桁は文脈弱いと除外
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
