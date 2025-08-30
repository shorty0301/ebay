# supplier_extractors.py
# -*- coding: utf-8 -*-
"""
各ECサイトのHTMLから 在庫 / 数量 / 価格 を抽出するヘルパ
GAS版からPython移植
"""

import re, json, functools, requests
from typing import Dict, Any
from bs4 import BeautifulSoup
import os

# ======== 共通ユーティリティ ==========
CI_MODE = bool(os.getenv("GITHUB_ACTIONS") or os.getenv("CI"))
HTTP_TIMEOUT = 7 if CI_MODE else 20           # 20s → CIでは 7s
AMZ_FOLLOW_ENABLED = not CI_MODE              # CI中は /dp/ 追撃取得しない
PLAYWRIGHT_ENABLED = not CI_MODE              # CI中は Playwright 呼ばない

def strip_tags(s: str) -> str:
    """HTMLからテキストを抽出（alt/title/aria-labelも補完）"""
    if not s:
        return ""
    soup = BeautifulSoup(s, "html.parser")

    # alt/title/aria-label をテキストとして追加
    for tag in soup.find_all(True):
        texts = []
        if tag.has_attr("alt"):
            texts.append(tag["alt"])
        if tag.has_attr("title"):
            texts.append(tag["title"])
        if tag.has_attr("aria-label"):
            texts.append(tag["aria-label"])
        if texts:
            tag.append(" ".join(texts))

    t = soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ",
                  t.replace("\u00A0", " ")
                   .replace("\u202F", " ")
                   .replace("\u2009", " ")).strip()

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
    
def to_int_yen_fuzzy(s: str, lo: int = 1, hi: int = 10_000_000) -> int | None:
    if not s:
        return None
    raw = str(s)

    x = z2h_digits(raw)
    x = (x.replace("\u00A0", " ")
           .replace("\u202F", " ")
           .replace("\u2009", " "))
    x = re.sub(r"[ \t\r\n]", "", x)
    x = x.replace("円税込", "円").replace("税込", "").replace("税抜", "")

    x = re.sub(r"[~〜\-ｰ－—–‒]+", "〜", x)  # 記号を統一
    if "〜" in x:
        x = x.split("〜", 1)[0]

    x = re.sub(r"[()（）〔〕［］【】<>＜＞]", "", x)
    x = re.sub(r"[^\d万,，.¥￥円]", "", x)  # 記号を限定

    m = re.match(r"^(\d+(?:\.\d+)?)[万]\s*([¥￥]?\s*\d{1,4}(?:[,，]\d{3})*|[¥￥]?\s*\d+)?", x)
    if m:
        a = float(m.group(1))
        base = int(a * 10000)
        b = None
        if m.group(2):
            b = re.sub(r"[^\d]", "", m.group(2)) or ""
            b = int(b) if b.isdigit() else None
        v = (base + (b or 0))
        if lo <= v <= hi:
            return v

    m = re.match(r"^(\d+(?:\.\d+)?)[万]", x)
    if m:
        v = int(float(m.group(1)) * 10000)
        if lo <= v <= hi:
            return v

    m = re.search(r"([¥￥]?\s*\d{1,3}(?:[,，]\d{3})+|[¥￥]?\s*\d{3,7}|\d+)", x)
    if not m:
        return None
    t = re.sub(r"[^\d]", "", m.group(1))
    if not t:
        return None
    try:
        v = int(t)
    except Exception:
        return None
    if lo <= v <= hi:
        return v
    return None

def pick_best_price(cands) -> float:
    nums=[]
    for s in cands:
        s=str(s)
        n = parse_yen_strict(s) if re.search(r"[¥￥円,，]", s) else float(re.sub(r"[^\d]", "", z2h_digits(s)) or "nan")
        if n==n and 0<n<1e7: nums.append(n)
    return min(nums) if nums else float("nan")

def to_int_yen(s: str) -> int | None:
    t = re.sub(r"[^\d]", "", z2h_digits(str(s or "")))
    if not t:
        return None
    try:
        v = int(t)
        return v if 0 < v < 10_000_000 else None
    except:
        return None
        
# ============================================================
# Playwright版 価格抽出（任意利用・既存処理へは未接続：副作用なし）
# ============================================================
import asyncio as _asyncio
import json as _json
import re as _re
from collections import Counter as _Counter

_YEN_RE = _re.compile(r"(?:￥|¥)\s*([0-9０-９][0-9０-９,，]{2,})")
_DEFAULT_LABEL_WORDS: tuple[str, ...] = ("税込", "送料込", "送料込み")

def _z2h_digits_play(s: str) -> str:
    table = str.maketrans("０１２３４５６７８９，，", "0123456789,,")
    return (s or "").translate(table)

def _to_int_play(s: str, lo: int = 100, hi: int = 3_000_000) -> int | None:
    t = _re.sub(r"[^\d]", "", _z2h_digits_play(s))
    if not t:
        return None
    try:
        v = int(t)
        return v if lo <= v <= hi else None
    except Exception:
        return None

def _pick_mode_min_play(nums: list[int]) -> int | None:
    arr = [n for n in nums if isinstance(n, int)]
    if not arr:
        return None
    freq = _Counter(arr).most_common()
    top_vals = [v for v, c in freq if c == freq[0][1]]
    return min(top_vals)

async def fetch_price_playwright(
    url: str,
    *,
    timeout_ms: int = 60_000,
    headless: bool = True,
    retries: int = 2,
    buy_button_texts: tuple[str, ...] = ("購入手続き", "今すぐ購入", "カートに入れる"),
    label_words: tuple[str, ...] = _DEFAULT_LABEL_WORDS,
    return_extra: bool = False,
) -> Dict[str, Any]:
    
    try:
        from playwright.async_api import async_playwright, Error as PWError, TimeoutError as PWTimeout
        from lxml import html as LH
    except Exception:
        # Playwright等が未導入の場合も、既存フローに影響しないよう失敗のみ返す
        return {"status": "price_not_found"}

    UA_WIN_CHROME = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    )

    async def _once() -> Dict[str, Any]:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=headless)
            ctx = await browser.new_context(
                user_agent=UA_WIN_CHROME,
                locale="ja-JP",
                extra_http_headers={"Accept-Language": "ja,en;q=0.8"},
                device_scale_factor=1.0,
            )
            page = await ctx.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            except (PWTimeout, PWError):
                await ctx.close(); await browser.close()
                return {"status": "timeout_goto"}

            await page.wait_for_timeout(700)
            content = await page.content()
            soup = BeautifulSoup(content, "lxml")
            doc = LH.fromstring(content)

            # 1) 購入ボタン近傍
            btn_or = " or ".join([f"contains(., '{t}')" for t in buy_button_texts])
            btn_pred = f"((self::button or self::a) and ({btn_or}))"
            # 直近の ¥ 含有要素を後方優先で
            def _scan_near(with_label: bool) -> int | None:
                nodes = doc.xpath(f"//*[contains(., '¥') or contains(., '￥')][preceding::*[{btn_pred}]]")
                for el in reversed(nodes):
                    txt = " ".join(el.xpath(".//text()")).strip()
                    if not txt:
                        continue
                    if with_label and not any(w in txt for w in label_words):
                        continue
                    m = _YEN_RE.search(txt)
                    if not m:
                        continue
                    v = _to_int_play(m.group(1))
                    if v is not None:
                        return v
                return None
            v1a = _scan_near(True)
            if v1a is not None:
                out = {"price": v1a, "source": "dom:near_buy+label"}
                if return_extra:
                    out.update(_collect_extras_play(soup))
                await ctx.close(); await browser.close()
                return out
            v1b = _scan_near(False)
            if v1b is not None:
                out = {"price": v1b, "source": "dom:near_buy"}
                if return_extra:
                    out.update(_collect_extras_play(soup))
                await ctx.close(); await browser.close()
                return out

            # 2) JSON-LD Offer/AggregateOffer
            try:
                def _walk(o) -> int | None:
                    if isinstance(o, dict):
                        t = (o.get("@type") or o.get("type") or "").lower()
                        if t in {"offer", "aggregateoffer"}:
                            for k in ("price", "lowPrice", "lowprice"):
                                if k in o:
                                    v = _to_int_play(str(o[k]))
                                    if v is not None:
                                        return v
                        for v in o.values():
                            r = _walk(v)
                            if r is not None:
                                return r
                    elif isinstance(o, list):
                        for it in o:
                            r = _walk(it)
                            if r is not None:
                                return r
                    return None
                for sc in soup.find_all("script", attrs={"type": _re.compile(r"ld\+json", _re.I)}):
                    raw = (sc.string or sc.get_text() or "").strip()
                    if not raw:
                        continue
                    raw = _re.sub(r"//.*?$|/\*.*?\*/", "", raw, flags=_re.S | _re.M)
                    raw = _re.sub(r",\s*([}\]])", r"\1", raw)
                    try:
                        data = _json.loads(raw)
                    except Exception:
                        continue
                    v2 = _walk(data)
                    if v2 is not None:
                        out = {"price": v2, "source": "jsonld:price"}
                        if return_extra:
                            out.update(_collect_extras_play(soup))
                        await ctx.close(); await browser.close()
                        return out
            except Exception:
                pass

            # 3) <meta name="product:price:amount">
            meta = soup.find("meta", attrs={"name": "product:price:amount"})
            if meta and meta.get("content"):
                v3 = _to_int_play(meta["content"])
                if v3 is not None:
                    out = {"price": v3, "source": "meta:product:price:amount"}
                    if return_extra:
                        out.update(_collect_extras_play(soup))
                    await ctx.close(); await browser.close()
                    return out

            # 4) 可視テキスト全体
            label_hits: list[int] = []
            generic_hits: list[int] = []
            for el in soup.find_all(text=_re.compile(r"[¥￥]")):
                try:
                    block = el.parent.get_text(" ", strip=True)
                except Exception:
                    continue
                if not block:
                    continue
                m = _YEN_RE.search(block)
                if not m:
                    continue
                v = _to_int_play(m.group(1))
                if v is None:
                    continue
                if any(w in block for w in label_words):
                    label_hits.append(v)
                else:
                    generic_hits.append(v)
            if label_hits:
                v4a = _pick_mode_min_play(label_hits)
                if v4a is not None:
                    out = {"price": v4a, "source": "text:labeled"}
                    if return_extra:
                        out.update(_collect_extras_play(soup))
                    await ctx.close(); await browser.close()
                    return out
            full = soup.get_text(" ", strip=True)
            all_nums = [_to_int_play(m.group(1)) for m in _YEN_RE.finditer(full)]
            all_nums = [n for n in all_nums if isinstance(n, int)]
            v4b = _pick_mode_min_play(all_nums or generic_hits)
            if v4b is not None:
                out = {"price": v4b, "source": "text:mode"}
                if return_extra:
                    out.update(_collect_extras_play(soup))
                await ctx.close(); await browser.close()
                return out

            await ctx.close(); await browser.close()
            return {"status": "price_not_found"}

    def _collect_extras_play(soup: BeautifulSoup) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        ttl = None
        if soup.title and soup.title.string:
            ttl = soup.title.string.strip()
        if not ttl:
            ogt = soup.find("meta", property="og:title")
            if ogt and ogt.get("content"):
                ttl = ogt["content"].strip()
        if ttl:
            out["title"] = ttl[:200]
        brand = None
        for meta_key, attr in (("product:brand", "property"), ("brand", "name")):
            tag = soup.find("meta", attrs={attr: meta_key})
            if tag and tag.get("content"):
                brand = tag["content"].strip()
                break
        if brand:
            out["brand"] = brand[:100]
        return out

    last: Dict[str, Any] | None = None
    for _ in range(max(1, retries)):
        res = await _once()
        if "price" in res:
            return res
        last = res
        await _asyncio.sleep(0.3)
    return last or {"status": "price_not_found"}

# 任意：Mercari専用の薄いアダプタ（外部から明示的に呼ぶ想定。既存フローに未接続）
async def mercari_price_via_playwright(url: str, **kw) -> int | None:
    if not _re.search(r"https?://([^/]*\.)?mercari\.com", url):
        return None
    res = await fetch_price_playwright(url, **kw)
    return res.get("price") if isinstance(res, dict) else None

# ========== fetch_html ==========
def fetch_html(url: str) -> str:
    ua_pc  = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36"
    ua_sp  = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
    headers = lambda ua: {"User-Agent": ua, "Accept-Language": "ja,en;q=0.8"}

    def try_get(u, ua):
        try:
            r=requests.get(u, headers=headers(ua), timeout=HTTP_TIMEOUT)
            if r.status_code==200: return r.text
        except requests.RequestException:
            return ""
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
    pat_money = re.compile(
        r"(?:[¥￥]\s*\d{1,3}(?:[,，]\d{3})+"
        r"|\d{1,3}(?:[,，]\d{3})+\s*円"
        r"|[¥￥]?\s*\d{3,7}\s*円?"
        r"|(?:\d+(?:\.\d+)?)\s*万\s*\d{0,4}\s*円?"
        r")"
    )
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
    """
    Rakuma/Fril 価格抽出（見出しの実価格を最優先・決め打ち）
    1) ページ先頭〜3000文字だけを見る
    2) 「¥2,500 送料込/送料込み/税込」など “価格の直近に送料/税込” があるパターンを最優先
    3) それが無ければ、先頭付近(〜1200字)で最初に出る「¥付き価格」を採用
    ※ キャンペーン語(OFF/最大/還元/ポイント/以上/まで etc.)の近傍は除外
    """
    head = text[:3000]

    def to_v(s: str) -> int | None:
        return to_int_yen(s)

    # ノイズ（キャンペーン・条件）
    STOP = re.compile(
        r"(最大|OFF|円OFF|割引|クーポン|ポイント|pt|還元|相当|円相当|"
        r"上限|参考|キャンペーン|セール|特典|抽選|進呈|付与|"
        r"以上|以下|未満|超|から|〜|~|まで|条件|対象|合計|総額|合算|月|分割|ローン)",
        re.I,
    )

    # 1) 「価格の直近に 送料/税込」パターン（最優先）
    p1 = re.compile(
        r"[¥￥]\s*(\d{1,3}(?:[,，]\d{3})+|\d{3,7})\s*(?:円)?\s*(?:送料込|送料込み|税込)",
        re.I,
    )
    for m in p1.finditer(head[:1800]):               # まずは見出しエリア
        s = m.group(1)
        i = m.start()
        ctx = head[max(0, i-80): i+80]
        if STOP.search(ctx): 
            continue
        v = to_v(s)
        if v: 
            return v

    # 2) 「送料/税込 の直後に価格」パターン（語→価格）
    p2 = re.compile(
        r"(?:送料込|送料込み|税込)[^\d]{0,12}[¥￥]?\s*(\d{1,3}(?:[,，]\d{3})+|\d{3,7})",
        re.I,
    )
    for m in p2.finditer(head[:1800]):
        s = m.group(1)
        i = m.start()
        ctx = head[max(0, i-80): i+80]
        if STOP.search(ctx):
            continue
        v = to_v(s)
        if v:
            return v

    # 3) フォールバック：先頭〜1200字で “最初の” ¥付き価格（ノイズ文脈は除外）
    p3 = re.compile(r"[¥￥]\s*(\d{1,3}(?:[,，]\d{3})+|\d{3,7})", re.I)
    for m in p3.finditer(head[:1200]):
        s = m.group(1)
        i = m.start()
        ctx = head[max(0, i-80): i+80]
        if STOP.search(ctx):
            continue
        v = to_v(s)
        if v:
            return v

    return None

def price_from_surugaya(html: str, text: str) -> int | None:
    """
    駿河屋 価格抽出
    優先度:
      1) JSON-LD / meta の price
      2) 「販売価格/税込/通販価格/ネット価格」近傍
      3) 末尾が「円」の金額（保険）
    除外: 買取価格/定価/参考価格/ポイント/割引/送料 等
    """
    def to_v(s): return to_int_yen(s)

    # --- 1) 構造化データ（最優先） ---
    for rx in [
        r'"price"\s*:\s*"?(\d{2,8})"?',
        r'"lowPrice"\s*:\s*"?(\d{2,8})"?',
        r'itemprop=["\']price["\'][^>]*content=["\']?(\d{2,8})',
        r'(?:og:price:amount|product:price:amount)"?\s*content=["\']?(\d{2,8})',
    ]:
        m = re.search(rx, html, re.I)
        if m:
            v = to_v(m.group(1))
            if v: return v

    # --- 2) ラベル近傍 ---
    STOP = re.compile(r"(ポイント|pt|還元|%|％|クーポン|OFF|円OFF|割引|値引|送料|手数料|相当|円相当|定価|参考価格|買取価格)", re.I)
    LABEL = re.compile(r"(販売価格|税込価格|税込|税抜|通販価格|ネット価格|価格)", re.I)
    YEN   = r"(?:[¥￥]\s*\d{1,3}(?:[,，]\d{3})+|[¥￥]?\s*\d{3,7}|\d{1,3}(?:[,，]\d{3})+\s*円|\d{3,7}\s*円)"

    cands: list[int] = []
    P = re.compile(r"(販売価格|税込価格|税込|税抜|通販価格|ネット価格|価格)[^\d¥￥]{0,12}("+YEN+")", re.I)
    for m in P.finditer(text[:20000]):
        s = m.group(2)
        v = to_v(s)
        if not v: 
            continue
        ctx = text[max(0, m.start()-100): m.end()+100]
        if STOP.search(ctx): 
            continue
        cands.append(v)

    if cands:
        return min(cands)

    # --- 3) 保険：末尾が「円」の金額（上部優先 & ノイズ除外） ---
    head = text[:7000]
    for m in re.finditer(r"([¥￥]?\s*\d{1,3}(?:[,，]\d{3})+|[¥￥]?\s*\d{3,7})\s*円", head):
        s = m.group(1)
        ctx = head[max(0, m.start()-60): m.end()+60]
        if STOP.search(ctx): 
            continue
        v = to_v(s)
        if v: 
            return v

    return None

def price_from_yahoo_auction(html: str, text: str) -> int | None:
    """ヤフオク価格抽出"""
    def to_v(s): return to_int_yen(s)

    # ラベル近傍（最優先）
    P = re.compile(r"(落札価格|現在価格|即決価格)[^\d¥￥]{0,8}([¥￥]?\s*\d{1,3}(?:[,，]\d{3})+|[¥￥]?\s*\d{3,7})")
    cands = []
    for m in P.finditer(text[:8000]):
        label, num = m.group(1), m.group(2)
        v = to_v(num)
        if v:
            pri = {"落札価格": 3, "現在価格": 2, "即決価格": 1}.get(label, 0)
            cands.append((pri, v))

    if cands:
        best_pri = max(p for p, _ in cands)
        return min(v for p, v in cands if p == best_pri)

    # 構造化データのフォールバック
    for rx in [r'"price"\s*:\s*"?(\d{3,7})"?', r'itemprop=["\']price["\'][^>]*content=["\']?(\d{3,7})']:
        m = re.search(rx, html, re.I)
        if m:
            v = to_v(m.group(1))
            if v: return v
    return None

def price_from_yshopping(html: str, text: str) -> int | None:
    """
    Yahoo!ショッピング / PayPayモール 価格抽出（購入価格優先・キャンペーン除外）
    """
    def to_v(s): return to_int_yen(s)

    # A) 構造化データ / meta / data-*（最優先）
    for rx in [
        r'"price"\s*:\s*"?(\d{2,8})"?',
        r'"lowPrice"\s*:\s*"?(\d{2,8})"?',
        r'itemprop=["\']price["\'][^>]*content=["\']?(\d{2,8})',
        r'(?:og:price:amount|product:price:amount)"?\s*content=["\']?(\d{2,8})',
        r'data-(?:price|amount|y-price|item-price|paypay-price|price-value)\s*=\s*["\']?(\d{2,8})',
    ]:
        m = re.search(rx, html, re.I)
        if m:
            v = to_v(m.group(1))
            if v: return v

    # B) 右側カラム/ボタン周り（「カートに入れる/今すぐ購入」近傍）
    STOP = re.compile(r"(ポイント|pt|獲得|進呈|付与|相当|円相当|PayPay|%|％|クーポン|OFF|円OFF|割引|最大|上限|還元|キャンペーン|条件|対象)", re.I)
    PRICE_LABEL = re.compile(r"(価格|販売価格|本体価格|セール価格|税込|税抜|お支払い金額|支払金額)", re.I)
    BUY = re.compile(r"(カートに入れる|今すぐ購入|注文手続き|注文に進む|購入手続き)", re.I)
    YEN = r"(?:[¥￥]\s*\d{1,3}(?:[,，]\d{3})+|[¥￥]?\s*\d{3,7}|\d{1,3}(?:[,，]\d{3})+\s*円|\d{3,7}\s*円)"

    cands: list[tuple[int,int]] = []

    # 右側（テキスト冒頭～2万字の中で、購入ボタン付近広めに）
    for m in BUY.finditer(text[:20000]):
        i = m.start()
        ctx = text[max(0, i-1000): i+1000]
        for n in re.finditer(YEN, ctx):
            s = n.group(0)
            v = to_v(s)
            if not v: continue
            win = ctx[max(0, n.start()-120): n.end()+120]
            if STOP.search(win): 
                continue
            score = 10
            if PRICE_LABEL.search(win): score += 3
            if re.search(r"[¥￥]|円", s): score += 1
            cands.append((score, v))

    # C) 「価格ラベル」近傍（本文全体）
    P = re.compile(r"(価格|販売価格|本体価格|セール価格|税込|税抜|お支払い金額|支払金額)[^\d¥￥]{0,12}("+YEN+")", re.I)
    for m in P.finditer(text[:25000]):
        s = m.group(2)
        v = to_v(s)
        if not v: continue
        ctx = text[max(0, m.start()-120): m.end()+120]
        if STOP.search(ctx): continue
        score = 7
        cands.append((score, v))

    if not cands:
        # D) 保険：lowPriceがあれば採用
        m = re.search(r'"lowPrice"\s*:\s*"?(\d{2,8})"?', html, re.I)
        if m:
            v = to_v(m.group(1))
            if v: return v
        return None

    best = max(s for s,_ in cands)
    return min(v for s,v in cands if s == best)

def price_from_paypay_fleamarket(html: str, text: str) -> int | None:
    """
    PayPayフリマ 価格抽出（行スキャン+ボタン近傍+保険）
    - まず見出しの「○○円」だけが書かれた行を優先
    - 次に『購入手続きへ』近傍の金額
    - それでもなければ先頭域から¥付き金額を保険で拾う
    - クーポン/実質/相当/pt/PayPay/%/OFF 等は除外
    """
    def to_v(s): return to_int_yen(s)

    STOP = re.compile(r"(クーポン|適用|実質|相当|円相当|ポイント|pt|PayPay|%|％|OFF|円OFF|割引|最大|上限|ボーナス|還元)", re.I)
    # 「600円」「12,300円」など（カンマ・全角半角対応）
    LINE_PRICE = re.compile(r"^(?:[¥￥]?\s*)?(\d{1,3}(?:[,，]\d{3})+|\d{3,7})\s*円$")
    # 任意の場所に現れる金額（保険用）
    ANY_PRICE = re.compile(r"(?:[¥￥]\s*)?(\d{1,3}(?:[,，]\d{3})+|\d{3,7})\s*円")

    # 1) 行スキャン：上の方に出る“素の価格行”を最優先
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in lines[:120]:  # 冒頭〜120行くらいを見る
        if STOP.search(ln):
            continue
        m = LINE_PRICE.match(ln)
        if m:
            v = to_v(m.group(1))
            if v:
                return v

    # 2) 『購入手続きへ / 購入に進む』近傍の金額を拾う
    joined = "\n".join(lines)
    btn = re.search(r"(購入手続きへ|購入に進む)", joined)
    if btn:
        i = btn.start()
        ctx = joined[max(0, i-1200): i+1200]  # ボタン周辺を広めに
        for m in ANY_PRICE.finditer(ctx):
            win = ctx[max(0, m.start()-80): m.end()+80]
            if STOP.search(win):
                continue
            v = to_v(m.group(1))
            if v:
                return v

    # 3) 保険：先頭域（見出し〜説明）の¥付き金額から、STOP近傍を除いて最初の1つ
    head = text[:5000]
    for m in ANY_PRICE.finditer(head):
        win = head[max(0, m.start()-60): m.end()+60]
        if STOP.search(win):
            continue
        v = to_v(m.group(1))
        if v:
            return v

    # 4) 最終保険：HTMLの price っぽい数値
    m = re.search(r'"price"\s*:\s*"?(\d{2,8})"?', html, re.I)
    if m:
        v = to_v(m.group(1))
        if v:
            return v

    return None

def stock_from_surugaya(html: str, text: str) -> str | None:
    """
    駿河屋 在庫判定（カートUI最優先 + 否定の注意書き無視）
    優先度:
      1) 強い否定語（ただし注意書き文脈は無視）
      2) 購入UI（カート/数量/購入手続き/フォーム）→ IN_STOCK
      3) 通販在庫（数値 / 記号 ○/△/×）
      4) 一般在庫記号 / 残り数量
    """
    # 正規化（全角空白→半角）
    t = text.replace("\u3000", " ")

    # 否定語の注意書き除外フィルタ
    NEG = re.compile(r"(売り切れ|在庫切れ|在庫なし|品切れ|販売終了|取扱い終了)", re.I)
    NEG_STOP = re.compile(r"(場合|際|ことがあります|可能性|恐れ|注意|ご了承ください|お問い合わせ)", re.I)

    for m in NEG.finditer(t):
        i = m.start()
        ctx = t[max(0, i-30): i+len(m.group(0))+30]
        # 「売り切れの際は〜」のような注意書きは無視
        if not NEG_STOP.search(ctx):
            return "OUT_OF_STOCK"

    # 購入UIが見えていれば確定で在庫あり
    if (re.search(r"(カートに入れる|今すぐ購入|購入手続き|ご注文|注文手続き|お買い物かご)", t) or
        re.search(r"\b数量\b", t) or
        re.search(r'<(form|button|input)[^>]*(add[-_\s]?to[-_\s]?cart|cart|buy|購入)[^>]*>', html, re.I) or
        re.search(r'(id|name|class)=["\'][^"\']*(add[-_\s]?to[-_\s]?cart|cartButton|cart-submit|buyNow|purchase)["\']', html, re.I)):
        return "IN_STOCK"

    # 通販在庫：数（最優先で評価）
    m = re.search(r"(通販在庫|ネット在庫)\s*(?:数|：|:)?\s*([0-9０-９]+)", t)
    if m:
        n = int(z2h_digits(m.group(2)))
        if n <= 0:
            return "OUT_OF_STOCK"
        return "LAST_ONE" if n == 1 else "IN_STOCK"

    # 通販在庫：記号
    if re.search(r"(通販在庫|ネット在庫)\s*[:：]?\s*[×✕ｘX]", t):
        return "OUT_OF_STOCK"
    if re.search(r"(通販在庫|ネット在庫)\s*[:：]?\s*[○〇◯]", t):
        return "IN_STOCK"
    if re.search(r"(通販在庫|ネット在庫)\s*[:：]?\s*[△▲]", t):
        return "LAST_ONE"

    # 一般在庫表現（記号）
    if re.search(r"(在庫|在庫状況|在庫数)\s*[:：]?\s*[×✕ｘX]", t):
        return "OUT_OF_STOCK"
    if re.search(r"(在庫|在庫状況|在庫数)\s*[:：]?\s*[○〇◯]", t):
        return "IN_STOCK"
    if re.search(r"(在庫|在庫状況|在庫数)\s*[:：]?\s*[△▲]", t):
        return "LAST_ONE"

    # 残り数量
    m = re.search(r"残り\s*([0-9０-９]+)\s*(?:点|個|枚|本)", t)
    if m:
        n = int(z2h_digits(m.group(1)))
        return "LAST_ONE" if n == 1 else "IN_STOCK"

    return None

def stock_from_rakuma(html: str, text: str) -> str | None:
    """
    Rakuma/Fril 在庫判定
    - JSON内の sold/sold_out を優先
    - SOLD OUT 表示や購入ボタンで判定
    """
    # JSONの状態
    if re.search(r'"(status|itemState|availability)"\s*:\s*"?(sold[_\- ]?out|sold)"?', html, re.I):
        return "OUT_OF_STOCK"
    if re.search(r'itemprop=["\']availability["\'][^>]*OutOfStock', html, re.I):
        return "OUT_OF_STOCK"

    # 画面テキスト
    if re.search(r"(SOLD\s*OUT|売り切れ|在庫なし|販売終了|売り切れました)", text, re.I):
        return "OUT_OF_STOCK"

    # 購入系（在庫あり）
    if re.search(r"(購入手続き|購入に進む|カートに入れる|今すぐ購入)", text):
        return "IN_STOCK"

    # ラスト1
    if re.search(r"(残り\s*1\s*(?:点|個|枚|本)|ラスト\s*1)", text):
        return "LAST_ONE"

    # HTMLの soldout クラス
    if re.search(r"(sold[\s_\-]?out)", html, re.I):
        return "OUT_OF_STOCK"

    return None

def stock_from_yahoo_auction(html: str, text: str) -> str | None:
    """ヤフオク在庫判定（=出品状態）"""
    if re.search(r"(終了しました|落札されました|出品終了|このオークションは終了)", text):
        return "OUT_OF_STOCK"
    if re.search(r"(入札する|即決で落札|今すぐ落札|入札受付中)", text):
        return "IN_STOCK"
    return None
    
def stock_from_paypay_fleamarket(html: str, text: str) -> str | None:
    """PayPayフリマ在庫判定"""
    if re.search(r"(売り切れました|SOLD\s*OUT|在庫なし|販売終了)", text, re.I):
        return "OUT_OF_STOCK"
    if re.search(r"(購入手続きへ|購入に進む)", text):
        return "IN_STOCK"
    if re.search(r"(残り\s*1\s*(?:点|個|枚|本)|ラスト\s*1)", text):
        return "LAST_ONE"
    return None

def stock_from_yshopping(html: str, text: str) -> str | None:
    """
    Yahoo!ショッピング / PayPayモール 在庫判定
    - JSON-LD の availability を最優先
    - 画面テキストの購入可否ワード / 売り切れワード
    - 残り数量で LAST_ONE
    """
    # 1) JSON-LD availability
    m = re.search(r'itemprop=["\']availability["\'][^>]*(InStock|OutOfStock)', html, re.I)
    if m:
        return "IN_STOCK" if re.search(r'InStock', m.group(0), re.I) else "OUT_OF_STOCK"

    # 2) 購入できる系
    if re.search(r"(在庫あり|カートに入れる|今すぐ購入|注文手続き|購入手続き|注文に進む)", text):
        return "IN_STOCK"

    # 3) 売り切れ/取扱い不可系
    if re.search(r"(在庫なし|在庫切れ|完売|販売終了|お取り扱いできません|取り扱いできません)", text):
        return "OUT_OF_STOCK"

    # 4) 残り数量
    m = re.search(r"残り\s*([0-9０-９]+)\s*(?:点|個)", text)
    if m:
        n = int(z2h_digits(m.group(1)))
        return "LAST_ONE" if n == 1 else "IN_STOCK"

    return None

# ========== 追加：Amazon.co.jp / Mercari / Rakuten Ichiba ==========
def price_from_amazon_jp(html: str, text: str) -> int | None:
    import re
    H_all = str(html or "")

    # --- 1) PC/MOBILE を個別に試す -------------------------
    parts = re.split(r'<!--\s*MOBILE MERGE\s*-->', H_all, flags=re.I) if "<!-- MOBILE MERGE -->" in H_all else [H_all]

    def _to(token: str) -> int | None:
        v = to_int_yen(token)
        if v is not None and 100 <= v <= 3_000_000:
            return v
        t = re.sub(r"[^\d]", "", token or "")
        if not t:
            return None
        try:
            v = int(t)
            return v if 100 <= v <= 3_000_000 else None
        except Exception:
            return None

    def _pick_from_single(H: str) -> int | None:
        # --- DOM直読（lxml があれば最優先） ---
        try:
            from lxml import html as LH
            doc = LH.fromstring(H)
            roots = doc.xpath(
                '//*[@id="priceToPay" or '
                '@id="corePriceDisplay_desktop_feature_div" or '
                '@id="corePrice_feature_div" or '
                '@id="corePriceDisplay_mobile_feature_div" or '
                '@id="apex_desktop"]'
            )

            # (a-1) a-offscreen
            for r in roots:
                for txt in r.xpath('.//span[contains(@class,"a-offscreen")]/text()'):
                    v = _to(txt)
                    if v:
                        return v

            # (a-2) a-price-whole（小数分割のwhole）
            for r in roots:
                wholes = r.xpath('.//span[contains(@class,"a-price-whole")]/text()')
                if wholes:
                    v = _to(wholes[0])
                    if v:
                        return v

            # (a-3) data-a-color="price" 近傍の素テキスト
            for r in roots:
                seg = " ".join(r.xpath('.//*[contains(@data-a-color,"price") or contains(@class,"price")]/text()'))
                m = re.search(r'(?:[¥￥]\s*\d{1,3}(?:[,，]\d{3})+|[¥￥]\s*\d{3,7}|\d{1,3}(?:[,，]\d{3})\s*円|\d{3,7}\s*円)', seg)
                if m:
                    v = _to(m.group(0)); 
                    if v: return v

            # (a-4) ラベル“真横”（±120文字）
            LABELS = ("通常の注文", "税込", "価格", "販売価格", "お支払い金額", "支払金額")
            BAD    = re.compile(r"(ポイント|pt|還元|クーポン|OFF|円OFF|%|％|ギフト券)", re.I)
            THNUM  = re.compile(r'[¥￥]?\s*\d{3,5}\s*円?\s*(?:以上|超|から)', re.I)
            FREE   = re.compile(r'(送料無料|通常配送無料|配送料無料|無料配送)', re.I)
            YEN    = re.compile(r'[¥￥]?\s*\d{1,3}(?:[,，]\d{3})+|[¥￥]?\s*\d{3,7}|\d{1,3}(?:[,，]\d{3})\s*円|\d{3,7}\s*円')

            def _is_threshold(s: str) -> bool:
                return bool(THNUM.search(s) and FREE.search(s))

            for r in roots:
                t = " ".join(r.xpath(".//text()"))
                for lab in LABELS:
                    for m in re.finditer(re.escape(lab), t):
                        win = t[max(0, m.start()-120): m.end()+120]
                        if BAD.search(win) or _is_threshold(win):
                            continue
                        m2 = YEN.search(win)
                        if m2:
                            v = _to(m2.group(0))
                            if v: return v

            # (a-5) 旧ID（念のため）
            for txt in doc.xpath('//*[@id="priceblock_ourprice" or @id="priceblock_dealprice" or @id="sns-base-price"]/text()'):
                v = _to(txt)
                if v:
                    return v

        except Exception:
            pass  # lxml なし or 失敗 → regex fallback

    # --- regex フォールバック（価格箱ブロックだけ） ---
    blk = ""
    for bid, span in (("priceToPay", 3000),
                  ("corePriceDisplay_desktop_feature_div", 6000),
                  ("corePrice_feature_div", 6000),
                  ("corePriceDisplay_mobile_feature_div", 6000),
                  ("apex_desktop", 8000)):
        m = re.search(r'id=["\']%s["\']([\s\S]{0,%d})' % (bid, span), H, re.I)
        if m:
            blk = m.group(1)
            break

    if not blk:
        # ---- 最終保険：上部テキストのラベル近傍（フッター年号は見ない） ----
        try:
            T = strip_tags(H).replace("\u3000", " ").replace("\u00A0", " ")
        except Exception:
            T = re.sub("<[^>]+>", " ", H)
        head = T[:15000]  # 上部だけ見る

        STOP   = re.compile(r"(ポイント|pt|還元|クーポン|OFF|円OFF|割引|%|％|ギフト券|通常配送無料|配送料無料|送料無料|以上で)", re.I)
        LABELS = r"(?:通常の注文|税込|価格|販売価格|お支払い金額|支払金額)"
        YEN    = r"(?:[¥￥]\s*\d{1,3}(?:[,，]\d{3})+|[¥￥]?\s*\d{3,7})"

        # ラベル → 金額
        pat_l2y = re.compile(rf"{LABELS}[^\d¥￥]{{0,20}}({YEN})", re.I)
        for m in pat_l2y.finditer(head):
            tok = m.group(1)
            ctx = head[max(0, m.start()-60): m.end()+60]
            if STOP.search(ctx):
                continue
            v = _to(tok)
            # 年号っぽい裸数字は除外（通貨/円が無い 1900〜2100）
            if v and not (1900 <= v <= 2100 and not re.search(r"[¥￥]|円", tok)):
                return v

        # 金額 → ラベル
        pat_y2l = re.compile(rf"({YEN})[^\d¥￥]{{0,20}}{LABELS}", re.I)
        for m in pat_y2l.finditer(head):
            tok = m.group(1)
            ctx = head[max(0, m.start()-60): m.end()+60]
            if STOP.search(ctx):
                continue
            v = _to(tok)
            if v and not (1900 <= v <= 2100 and not re.search(r"[¥￥]|円", tok)):
                return v

        # 出現多数（モード）で拾う（同額が2回以上出たらそれ）
        from collections import Counter
        vals: list[int] = []
        for m in re.finditer(rf"{YEN}", head, re.I):
            tok = m.group(0)
            v = _to(tok)
            if v is None:
                continue
            if 1900 <= v <= 2100 and not re.search(r"[¥￥]|円", tok):
                continue
            vals.append(v)

        if vals:
            v, cnt = Counter(vals).most_common(1)[0]
            if cnt >= 2:
                return v

        return None


        # offscreen（￥/円 なくてもOK）
        for m in re.finditer(r'class=["\']a-offscreen["\'][^>]*>\s*([¥￥]?\s*[\d,，]{1,10})(?:\s*円)?\s*<', blk, re.I):
            token = m.group(1)
            v = _to(token)
            if v:
                if 1900 <= v <= 2100 and not re.search(r'[¥￥]|円', token):
                    continue
                return v

        # ラベル近傍（±120）
        LABEL_NEAR = re.compile(
            r'(通常の注文|税込|価格|販売価格|お支払い金額|支払金額)[^¥￥\d]{0,40}'
            r'((?:[¥￥]\s*\d{1,3}(?:[,，]\d{3})+|[¥￥]\s*\d{3,7}|\d{1,3}(?:[,，]\d{3})\s*円|\d{3,7}\s*円))',
            re.I
        )
        for m in LABEL_NEAR.finditer(re.sub(r"\s+", " ", blk)):
            win = m.group(0)
            if re.search(r"(ポイント|pt|還元|クーポン|OFF|円OFF|%|％|ギフト券)", win, re.I):
                continue
            if re.search(r'[¥￥]?\s*\d{3,5}\s*円?\s*(以上|超|から)', win) and re.search(r'(送料無料|通常配送無料|配送料無料|無料配送)', win):
                continue
            v = _to(m.group(2))
            if v:
                if 1900 <= v <= 2100 and not re.search(r'[¥￥]|円', m.group(2)):
                    continue
                return v
        return None

    # パートごとに試し、最初に取れたものを返す
    for H in parts:
        v = _pick_from_single(H)
        if isinstance(v, int):
            return v
    return None



def stock_from_amazon_jp(html: str, text: str) -> str | None:
    t = str(text or "")
    if re.search(r"(現在お取り扱いできません|一時的に在庫切れ|再入荷予定は立っておりません)", t):
        return "OUT_OF_STOCK"
    if re.search(r"(在庫あり|カートに入れる|今すぐ買う|今すぐ購入)", t):
        m = re.search(r"残り\s*([0-9０-９]+)\s*(?:点|個|枚|本)", t)
        if m:
            n = int(z2h_digits(m.group(1)))
            return "LAST_ONE" if n == 1 else "IN_STOCK"
        return "IN_STOCK"
    if re.search(r"(売り切れ|在庫切れ|SOLD\s*OUT)", t, re.I):
        return "OUT_OF_STOCK"
    return None


def amazon_price_via_playwright_sync(url: str, timeout_ms: int = 45000, headless: bool = True) -> int | None:
    """
    任意の最終保険。Playwrightがない環境ではNoneを返すだけ（他サイト無影響）。
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except Exception:
        return None

    SELS = [
        '#priceToPay .a-offscreen',
        '#corePriceDisplay_desktop_feature_div .a-offscreen',
        '#corePrice_feature_div .a-offscreen',
        '#corePriceDisplay_mobile_feature_div .a-offscreen',
        '#apex_desktop .a-offscreen',
        '#priceblock_ourprice', '#priceblock_dealprice', '#sns-base-price'
    ]
    UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

    with sync_playwright() as p:
        br = p.chromium.launch(headless=headless)
        ctx = br.new_context(user_agent=UA, locale="ja-JP",
                             extra_http_headers={"Accept-Language": "ja,en;q=0.8"})
        pg = ctx.new_page()
        try:
            pg.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            pg.wait_for_timeout(600)
        except PWTimeout:
            ctx.close(); br.close(); return None

        # canonical が dp/gp/product なら正規URLへ
        try:
            can = pg.get_attribute('link[rel="canonical"]', 'href')
            if can and ('/dp/' in can or '/gp/product/' in can):
                pg.goto(can, wait_until="domcontentloaded", timeout=timeout_ms)
                pg.wait_for_timeout(400)
        except Exception:
            pass

        for sel in SELS:
            try:
                txt = pg.inner_text(sel, timeout=1500)
                v = to_int_yen(txt)
                if v is None:
                    t = re.sub(r"[^\d]", "", txt or "")
                    v = int(t) if t else None
                if v and 100 <= v <= 3_000_000:
                    ctx.close(); br.close(); return v
            except Exception:
                continue

        ctx.close(); br.close()
    return None


def stock_from_mercari(html: str, text: str) -> str | None:
    """
    メルカリ 在庫判定（安全版）
    - まず購入UIがあれば IN_STOCK（最優先）
    - 次に SOLD OUT / 売り切れ（ただし購入UIが見えない時だけ OUT）
    - どちらも取れない＝ボットページの可能性 → None（UNKNOWN）
    """
    t = text

    # 1) 購入UI（最優先）
    if re.search(r"(購入手続きへ|購入に進む|カートに入れる|今すぐ購入)", t):
        m = re.search(r"残り\s*([0-9０-９]+)\s*(?:点|個|枚|本)", t)
        if m:
            n = int(z2h_digits(m.group(1)))
            return "LAST_ONE" if n == 1 else "IN_STOCK"
        return "IN_STOCK"

    # 2) SOLD OUT/売り切れ（購入UIが無い時のみ有効）
    if re.search(r"(SOLD\s*OUT|売り切れ|売り切れました)", t, re.I) or re.search(r"(sold[\s_\-]?out)", html, re.I):
        return "OUT_OF_STOCK"

    # 3) ラスト1
    if re.search(r"(残り\s*1\s*(?:点|個|枚|本)|ラスト\s*1)", t):
        return "LAST_ONE"

    # 4) 判定不能（＝取得HTMLが怪しい）
    return None

def price_from_mercari(html: str, text: str) -> int | None:
    """
    メルカリ 価格抽出
    - 『購入手続きへ/購入に進む』近傍の “○○円” を最優先
    - 先頭域の ‘¥付き’ 価格
    - JSON内 price の保険
    """
    def to_v(s): return to_int_yen(s)
    STOP = re.compile(r"(ポイント|還元|%|％|OFF|円OFF|割引|最大|上限|相当|円相当|クーポン|キャンペーン|実質)", re.I)

    head = text[:8000]

    # 1) ボタン近傍
    for btn in re.finditer(r"(購入手続きへ|購入に進む|カートに入れる|今すぐ購入)", head):
        i = btn.start()
        ctx = head[max(0, i-1500): i+1500]
        for m in re.finditer(r"(?:[¥￥]\s*)?(\d{1,3}(?:[,，]\d{3})+|\d{3,7})\s*円", ctx):
            win = ctx[max(0, m.start()-80): m.end()+80]
            if STOP.search(win): 
                continue
            v = to_v(m.group(1))
            if v: 
                return v

    # 2) 先頭域の ‘¥付き’
    for m in re.finditer(r"[¥￥]\s*(\d{1,3}(?:[,，]\d{3})+|\d{3,7})", head[:3000]):
        win = head[max(0, m.start()-80): m.end()+80]
        if STOP.search(win): 
            continue
        v = to_v(m.group(1))
        if v: 
            return v

    # 3) JSON保険
    m = re.search(r'"price"\s*:\s*"?(\d{2,8})"?', html, re.I)
    if m:
        v = to_v(m.group(1))
        if v: 
            return v
    return None
    
# ==== Mercari 専用：最初から Playwright で price/stock を取る（同期） ====
def mercari_via_playwright_sync(url: str, timeout_ms: int = 90000, headless: bool = False) -> dict | None:
    """
    返り値: {"price": int | None, "stock": "IN_STOCK|OUT_OF_STOCK|LAST_ONE|UNKNOWN"}
    - Playwrightが未インストールなら None を返すだけ（他サイトに影響なし）
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except Exception:
        return None

    import re

    YEN_MARK = re.compile(r"(?:￥|¥)\s*([0-9０-９,，]{3,})")
    LABEL_HIT = re.compile(r"(税込|送料込|送料込み)")
    SOLD = re.compile(r"(SOLD\s*OUT|売り切れ|売り切れました)", re.I)
    BUY  = re.compile(r"(購入手続きへ|購入に進む|カートに入れる|今すぐ購入)")
    LAST1 = re.compile(r"(残り\s*1\s*(?:点|個|枚|本)|ラスト\s*1)")

    def _to_int_yen_fast(s: str) -> int | None:
        t = re.sub(r"[^\d]", "", s.translate(str.maketrans("０１２３４５６７８９，", "0123456789,")))
        if not t: return None
        try:
            v = int(t)
            return v if 100 <= v <= 3_000_000 else None
        except:  # noqa
            return None

    price: int | None = None
    stock: str = "UNKNOWN"

    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(
            user_agent=UA,
            locale="ja-JP",
            extra_http_headers={"Accept-Language": "ja,en;q=0.8"},
            viewport={"width": 1400, "height": 900},
        )
        page = ctx.new_page()

        # ネットワークJSONから price を拾う（一番確実）
        def _on_response(resp):
            nonlocal price
            if price is not None:
                return
            try:
                ct = (resp.headers or {}).get("content-type", "")
                if "json" in ct.lower():
                    body = resp.text()
                    m = re.search(r'"price"\s*:\s*"?(\d{3,7})"?', body)
                    if m:
                        v = _to_int_yen_fast(m.group(1))
                        if v: price = v
            except Exception:
                pass

        page.on("response", _on_response)

        try:
            page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            # 購入ボタンが出るのを少しだけ待つ（無くても続行）
            try:
                page.wait_for_selector(
                    "text=購入手続きへ, text=購入に進む, text=カートに入れる",
                    timeout=2000
                )
            except Exception:
                pass
            page.wait_for_timeout(500)  # 仕上げの短い待機
        except PWTimeout:
            ctx.close(); browser.close()
            return {"price": None, "stock": "UNKNOWN"}


        page.wait_for_timeout(1500)  # JS描画待ち（短め）

        # 画面テキストで在庫を判定
        try:
            body = page.inner_text("body")
        except Exception:
            body = ""

        if SOLD.search(body):
            stock = "OUT_OF_STOCK"
        elif LAST1.search(body):
            stock = "LAST_ONE"
        elif BUY.search(body):
            stock = "IN_STOCK"

        # price がまだ無ければテキストから拾う（税込/送料込が近ければ最優先）
        if price is None:
            # ラベル付き優先
            for block in body.splitlines():
                if LABEL_HIT.search(block):
                    m = YEN_MARK.search(block)
                    if m:
                        v = _to_int_yen_fast(m.group(1))
                        if v: price = v; break
            # それでも無ければ全文から
            if price is None:
                m = YEN_MARK.search(body)
                if m:
                    v = _to_int_yen_fast(m.group(1))
                    if v: price = v

        # さらに最後の保険：DOM HTML に埋め込まれた JSON
        if price is None:
            html = page.content()
            m = re.search(r'"price"\s*:\s*"?(\d{3,7})"?', html)
            if m:
                v = _to_int_yen_fast(m.group(1))
                if v: price = v

        ctx.close(); browser.close()

    return {"price": price, "stock": stock}



# ======== Rakuten helpers (GAS移植) =========
def _availability_from_meta_or_ld(html: str) -> str | None:
    # JSON-LD / microdata の availability を見る
    if re.search(r'itemprop=["\']availability["\'][^>]*InStock', html, re.I):
        return "IN_STOCK"
    if re.search(r'itemprop=["\']availability["\'][^>]*OutOfStock', html, re.I):
        return "OUT_OF_STOCK"
    # JSON-LDをざっくり
    m = re.search(r'"availability"\s*:\s*"([^"]+)"', html, re.I)
    if m:
        v = m.group(1)
        if re.search(r'InStock', v, re.I): return "IN_STOCK"
        if re.search(r'OutOfStock', v, re.I): return "OUT_OF_STOCK"
    # isSoldOut / inStock フラグ
    m = re.search(r'"(?:isSoldOut|soldOut)"\s*:\s*(true|false)', html, re.I)
    if m: return "OUT_OF_STOCK" if m.group(1).lower()=="true" else "IN_STOCK"
    m = re.search(r'"(?:isInStock|inStock)"\s*:\s*(true|false)', html, re.I)
    if m: return "IN_STOCK" if m.group(1).lower()=="true" else "OUT_OF_STOCK"
    return None


def _price_from_rakuten_books(html: str) -> int | None:
    # 楽天ブックスは JSON-LD, itemprop=price, og:price:amount などに出やすい
    for rx in [
        r'"price"\s*:\s*"?([¥￥]?\s*[\d,，]{1,10})(?:\s*円)?"?',
        r'itemprop=["\']price["\'][^>]*content=["\']?([\d,，]{1,10})',
        r'(?:og:price:amount|product:price:amount)"?\s*content=["\']?([\d,，]{1,10})',
        r'data-(?:price|amount|item-price)\s*=\s*["\']?([\d,，]{1,10})',
    ]:
        m = re.search(rx, html, re.I)
        if m:
            v = to_int_yen(m.group(1))
            if v: return v
    return None


def _price_from_rakuten_common(html: str, text: str) -> int | None:
    # GAS側 _priceFromRakuten(html, text) のイメージを再現（既存の楽天関数でもOK）
    STOP     = re.compile(r"(ポイント|pt|還元|%|％|クーポン|OFF|円OFF|割引|最大|上限|実質|相当|円相当|付与|進呈|獲得)", re.I)
    SHIPPING = re.compile(r"(送料|配送料|メール便)", re.I)
    LABEL    = re.compile(r"(税込|税抜|価格|販売価格|本体価格|セール価格|お支払い金額)", re.I)
    BUY      = re.compile(r"(購入手続き|購入手続きへ|買い物かごに入れる|かごに追加|かごに入れる)", re.I)
    YEN      = r"(?:[¥￥]\s*\d{1,3}(?:[,，]\d{3})+|[¥￥]?\s*\d{3,7}|\d{1,3}(?:[,，]\d{3})+\s*円|\d{3,7}\s*円)"

    # 1) JSON-LD/メタ先（カンマ/円付き対応）
    for rx in [
        r'"price"\s*:\s*"?([¥￥]?\s*[\d,，]{1,10})(?:\s*円)?"?',
        r'"lowPrice"\s*:\s*"?([¥￥]?\s*[\d,，]{1,10})(?:\s*円)?"?',
        r'(?:og:price:amount|product:price:amount)"?\s*content=["\']?([\d,，]{1,10})',
        r'itemprop=["\']price["\'][^>]*content=["\']?([\d,，]{1,10})',
        r'data-(?:price|amount|item-price)\s*=\s*["\']?([\d,，]{1,10})',
    ]:
        m = re.search(rx, html, re.I)
        if m:
            v = to_int_yen(m.group(1))
            if v: return v

    # 2) 購入ボックス付近（スコアリング：送料/ポイント近傍は除外）
    cands: list[tuple[int,int]] = []
    for b in BUY.finditer(text[:35000]):
        i = b.start()
        ctx = text[max(0, i-1600): i+1600]
        for n in re.finditer(YEN, ctx):
            s = n.group(0)
            v = to_int_yen(s)
            if not v: continue
            win = ctx[max(0, n.start()-120): n.end()+120]
            if STOP.search(win) or SHIPPING.search(win): 
                continue
            score = 10
            if LABEL.search(win): score += 3
            if re.search(r"[¥￥]|円", s): score += 1
            if re.search(r"\d{1,3}(?:[,，]\d{3})+", s): score += 1
            cands.append((score, v))
    if cands:
        best = max(s for s,_ in cands)
        # キャンペーンの小さい数字を避けるため同点は最大値を採用
        return max(v for s,v in cands if s==best)

    # 3) ラベル近傍
    for m in re.finditer(r"(税込|税抜|価格|販売価格|本体価格|セール価格|お支払い金額)[^\d¥￥]{0,12}("+YEN+")",
                         text[:35000], re.I):
        v = to_int_yen(m.group(2))
        if not v: continue
        ctx = text[max(0, m.start()-120): m.end()+120]
        if STOP.search(ctx) or SHIPPING.search(ctx): 
            continue
        return v

    # 4) テキスト保険（上部）
    head = text[:12000]
    for m in re.finditer(r"(?:[¥￥]\s*)?(\d{1,3}(?:[,，]\d{3})+|\d{3,7})\s*円", head):
        ctx = head[max(0, m.start()-80): m.end()+80]
        if STOP.search(ctx) or SHIPPING.search(ctx): 
            continue
        v = to_int_yen(m.group(1))
        if v: return v

    return None

# ====== 価格抽出：Rakuten（簡易・税込優先 / GAS移植相当） ======
def _collect_jsonld_prices(html: str) -> list[int]:
    out: list[int] = []
    try:
        soup = BeautifulSoup(html or "", "html.parser")
        for sc in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
            raw = (sc.string or sc.get_text() or "").strip()
            if not raw:
                continue
            # コメントや末尾カンマを軽く除去
            raw2 = re.sub(r"//.*?$|/\*.*?\*/", "", raw, flags=re.S | re.M)
            raw2 = re.sub(r",\s*([}\]])", r"\1", raw2)
            try:
                data = json.loads(raw2)
            except Exception:
                continue

            def walk(x):
                if isinstance(x, dict):
                    # price が文字列/数値どちらでも取りうる
                    if "price" in x:
                        v = to_int_yen(str(x["price"]))
                        if v:
                            out.append(v)
                    for v in x.values():
                        walk(v)
                elif isinstance(x, list):
                    for it in x:
                        walk(it)

            walk(data)
    except Exception:
        pass
    return out


def _price_from_rakuten(html: str, text: str) -> int | None:
    H = str(html or "")
    T = re.sub(r"\s+", " ", str(text or ""))  # GAS同様にスペース正規化

    # STOP語（実質/ポイント/クーポン/手数料など）: 価格候補から除外
    STOP = re.compile(r"(参考(?:小売)?価格|実質|ポイント|付与|獲得|クーポン|割引|値引|上限|注文合計|代引|着払い|配送料|手数料|SPU|下取り|買取)")

    def _add(cands: list[int], v: str | int | float):
        s = strip_tags(str(v or "")).strip()
        if not s:
            return
        # 「送料」が含まれるが「送料無料/送料込/送料込み」ではない → 除外
        if re.search(r"送料", s) and not re.search(r"(送料無料|送料込|送料込み)", s):
            return
        if STOP.search(s):
            return
        # 通貨記号あり：厳密パース / なし：カンマ除去して数値化
        if re.search(r"[¥￥円,，]", s):
            n = parse_yen_strict(s)
            if n == n:  # not NaN
                iv = int(n)
            else:
                iv = None
        else:
            t = re.sub(r"[^\d]", "", s)
            iv = int(t) if t else None

        if iv and 0 < iv < 10_000_000:
            cands.append(iv)

    cand: list[int] = []

    # 1) meta / itemprop=price / og:price:amount など
    for rx in [
        r'<meta[^>]+(?:name|property)=["\'](?:product:price:amount|og:price:amount|price)["\'][^>]*content=["\']([^"\']+)["\']',
        r'<(?:meta|data|input)[^>]+itemprop=["\']price["\'][^>]*content=["\']([^"\']+)["\']',
    ]:
        for m in re.finditer(rx, H, re.I | re.S):
            _add(cand, m.group(1))

    # 2) JSON-LD内の price 群
    for v in _collect_jsonld_prices(H):
        _add(cand, v)

    # 3) price系クラスを含む要素のテキスト
    for m in re.finditer(
        r'<(?:span|div|p|em|strong)[^>]+class=["\'][^"\']*(?:\bprice(?:2|3)?\b|Price__value|itemPrice|productPrice|RPrice\b|priceBox|main-price)[^"\']*["\'][^>]*>([\s\S]*?)</(?:span|div|p|em|strong)>',
        H, re.I):
        _add(cand, m.group(1))

    # 4) 「税込」の前後にある金額（優先）
    #    税込 [数値] / [数値] 税込 の両方
    r1 = re.compile(r"税込[^0-9¥￥]{0,10}([¥￥]?\s?\d{1,3}(?:[,，]\d{3})+|\d{3,7})\s*円?", re.I)
    r2 = re.compile(r"([¥￥]?\s?\d{1,3}(?:[,，]\d{3})+|\d{3,7})\s*円[^ぁ-んァ-ヶA-Za-z0-9]{0,8}税込", re.I)
    for m in r1.finditer(T):
        _add(cand, m.group(1))
    for m in r2.finditer(T):
        _add(cand, m.group(1))

    if not cand:
        return None
    return min(cand)  # GAS同様、最小値（通常価格）を採用



# ========== 在庫・価格 抽出のメイン ==========
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

    m_host = re.search(r"https?://([^/]+)/?", url)
    host = m_host.group(1).lower() if m_host else ""
    text = strip_tags(html).replace("\u3000", " ").replace("\u00A0", " ")
    
    def _suspect(h: str, t: str) -> bool:
        if not h or len(h) < 1200:
            return True
        lt = (h or "").lower()
        return bool(re.search(
            r"(captcha|are you a robot|enable cookies|javascriptを有効|cookie|アクセスが集中|ただいまアクセス|redirecting\.\.\.)",
            lt
        ))

    if _suspect(html, text) and "_strong_get_html" in globals():
        try:
            strong = _strong_get_html(url)
            if strong and len(strong) > len(html):
                html = strong
                text = strip_tags(html).replace("\u3000", " ").replace("\u00A0", " ")
        except Exception:
            pass
            
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
    # --- ラクマ
    elif ("fril" in host) or ("rakuma" in host) or ("fril.jp" in host) or ("rakuma.rakuten.co.jp" in host):
        s = stock_from_rakuma(html, text)
        if s:
            stock = s
        price = price_from_rakuma(html, text)
    # --- ヤフオク ---
    elif "auctions.yahoo.co.jp" in host:
         s = stock_from_yahoo_auction(html, text)
         if s: stock = s
         price = price_from_yahoo_auction(html, text)
    # PayPayフリマ
    elif "paypayfleamarket.yahoo.co.jp" in host:
        s = stock_from_paypay_fleamarket(html, text)
        if s: stock = s
        price = price_from_paypay_fleamarket(html, text)

    # Yahoo!ショッピング / PayPayモール
    elif ("shopping.yahoo.co.jp" in host) or ("store.shopping.yahoo.co.jp" in host) or ("paypaymall.yahoo.co.jp" in host):
        s = stock_from_yshopping(html, text)
        if s: stock = s
        price = price_from_yshopping(html, text)
    # 駿河屋
    elif ("suruga-ya" in host) or ("surugaya" in host):
        s = stock_from_surugaya(html, text)
        if s: stock = s
        price = price_from_surugaya(html, text)
    # ========== Amazon.co.jp ==========
    elif ("amazon.co.jp" in host) or host.endswith(".amazon.co.jp"):
        # 1st pass: 価格箱だけ読む軽量版（price_from_amazon_jp は価格箱XPath＋ラベル近傍）
        if debug:
            H = html or ""
            T = text or ""
            print("[AMZ] len(html)=", len(H))
            print("[AMZ] markers:", {
                "apex":       bool(re.search(r'id=["\']apex_desktop["\']', H, re.I)),
                "corePrice":  bool(re.search(r'id=["\']corePrice_feature_div["\']', H, re.I)),
                "corePriceDisp": bool(re.search(r'id=["\']corePriceDisplay_desktop_feature_div["\']', H, re.I)),
                "priceToPay_id":    bool(re.search(r'id=["\']priceToPay["\']', H, re.I)),
                "priceToPay_class": bool(re.search(r'class=["\'][^"\']*\bpriceToPay\b', H, re.I)),
                "aOffscreen": bool(re.search(r'class=["\']a-offscreen["\']', H, re.I)),
                "buyNow":     bool(re.search(r"(今すぐ買う|Buy Now)", T)),
                "addCart":    bool(re.search(r"(カートに入れる|Add to Cart)", T)),
                "unavail":    bool(re.search(r"(現在お取り扱いできません|Currently unavailable)", T, re.I)),
                "robot":      bool(re.search(r"(Robot Check|captcha|ロボットによる|自動アクセス|enable cookies)", H, re.I)),
            })

        s = stock_from_amazon_jp(html, text)
        if s:
            stock = s
        price = price_from_amazon_jp(html, text)

        # 再取得するかの判定（DP化やロボットページ時など）
        need_follow = False
        if price is None and stock in ("UNKNOWN", "", None):
            need_follow = True
        if not re.search(r"/(dp|gp/product)/", url):
            need_follow = True
        if len(html or "") < 3000:
            need_follow = True
        if re.search(r"(Robot Check|captcha|ロボットによる|自動アクセス|enable cookies)", html or "", re.I):
            need_follow = True
        if CI_MODE:
            need_follow = False
        if price is None and PLAYWRIGHT_ENABLED:
            try:
                p3 = amazon_price_via_playwright_sync(dp_url or url, headless=True)
            except Exception:
                p3 = None
            if isinstance(p3, int):
                price = p3

        # 2nd pass: HTMLから /dp/… を推定して一度だけ再取得（正規URLへ寄せる）
        dp_url = None
        if need_follow:
            try:
                dp_url = _amz_guess_dp_url(host, url, html)
            except Exception:
                dp_url = None

            if dp_url:
                try:
                    amz_html2 = fetch_html(dp_url)
                    amz_text2 = strip_tags(amz_html2).replace("\u3000", " ").replace("\u00A0", " ")
                    s2 = stock_from_amazon_jp(amz_html2, amz_text2)
                    if s2:
                        stock = s2
                    p2 = price_from_amazon_jp(amz_html2, amz_text2)
                    if p2 is not None:
                        price = p2
                    if debug:
                        print("[AMZ] follow dp:", dp_url, " len=", len(amz_html2), " price=", p2, " stock=", s2)
                except Exception:
                    pass

        # 3rd pass（任意の保険）：Playwrightで価格箱だけ直読（未定義なら無視）
        if price is None:
            try:
                p3 = amazon_price_via_playwright_sync(dp_url or url, headless=True)
            except NameError:
                p3 = None
            except Exception:
                p3 = None
            if isinstance(p3, int):
                price = p3
                if debug:
                    print("[AMZ] playwright price:", p3)




    # Mercari（最初から Playwright）
    elif ("mercari" in host) or ("jp.mercari.com" in host):
        try:
            res = mercari_via_playwright_sync(url, timeout_ms=90_000, headless=False)
        except Exception:
            res = None

        if isinstance(res, dict):
            if res.get("stock"): stock = res["stock"]
            if isinstance(res.get("price"), int): price = res["price"]

        # 取れなかったときだけ旧ロジックに保険
        if price is None:
            s = stock_from_mercari(html, text)
            if s: stock = s
            p2 = price_from_mercari(html, text)
            if p2 is not None:
                price = p2



        
    elif ("item.rakuten.co.jp" in host) or (host.endswith(".rakuten.co.jp")) or ("rakuten.co.jp" in host):
        # 価格
        price = _price_from_rakuten(html, text)
        if "_stock_from_rakuten_combined" in globals():
           try:
               s, q = _stock_from_rakuten_combined(html, text, price)
               if s: stock = s
               if q: qty = q
           except Exception:
                pass
    


    if price is None and not ("amazon.co.jp" in host or host.endswith(".amazon.co.jp")):
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

            # 数値へ変換（厳密 → ゆるめ）
            v = None
            n = parse_yen_strict(h)
            if n == n and 0 < n < 10_000_000:
                 v = int(n)
            else:
                vv = to_int_yen_fuzzy(h)
                if vv is not None:
                    v = vv
            if v is None:
                continue


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
        # プラグイン補完（既存ロジックの結果を壊さない）

    out = {"stock": stock, "qty": qty, "price": price}
    if debug:
        out["_debug"] = {"host": host, "text_snippet": text[:200]}
    return out


# ========== キャッシュ付き ==========
@functools.lru_cache(maxsize=256)
def fetch_and_extract(url: str) -> Dict[str, Any]:
    return extract_supplier_info(url, fetch_html(url))

def _strong_get_html(url: str) -> str:
    try:
        if "amazon.co.jp" not in url:
            return ""  # 他サイトは触らない

        import requests, re
        sess = requests.Session()

        from requests.adapters import HTTPAdapter
        sess.mount("https://", HTTPAdapter(max_retries=1))
        sess.mount("http://",  HTTPAdapter(max_retries=1))
        
        UA_PC = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36"
        UA_MB = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
        H_PC = {"User-Agent": UA_PC, "Accept-Language": "ja,en;q=0.8", "Referer": "https://www.google.com/"}
        H_MB = {"User-Agent": UA_MB, "Accept-Language": "ja,en;q=0.8", "Referer": "https://www.google.com/"}

        def _get(u, headers):
            try:
                r = sess.get(u, headers=headers, timeout=20, allow_redirects=True)
                return r.status_code, (r.text or "")
            except Exception:
                return 0, ""
        if CI_MODE:
            for hdr in (H_PC, H_MB):
                sc, h = _get(url, hdr)
                if sc == 200 and h:
                    return h
            return ""
        htmls: list[str] = []
        sc1, h1 = _get(url, H_PC);  sc2, h2 = _get(url, H_MB)
        if sc1 == 200 and h1: htmls.append(h1)
        if sc2 == 200 and h2: htmls.append(h2)

        m = re.search(r"(https?://[^/]*amazon\.co\.jp)/(?:.*?)(/(?:dp|gp/product)/[A-Z0-9]{10})", url, re.I)
        if m:
            canon = m.group(1).rstrip("/") + m.group(2)
            sc3, h3 = _get(canon, H_PC);  sc4, h4 = _get(canon + "?psc=1", H_PC)
            if sc3 == 200 and h3: htmls.append(h3)
            if sc4 == 200 and h4: htmls.append(h4)
      
        def _is_robot(h: str) -> bool:
            return bool(re.search(r"(Robot Check|captcha|自動アクセス|ロボットによる|enable cookies)", h, re.I))
        pool = [h for h in htmls if h and not _is_robot(h)] or htmls
        return max(pool, key=len) if pool else ""
    except Exception:
        return ""

def _amz_guess_dp_url(host: str, base_url: str, html: str) -> str | None:
    """
    取得HTMLから /dp/ または /gp/product/ の商品詳細URLを推定して返す。
    1) <link rel="canonical" href=".../dp/ASIN"> を最優先
    2) ページ内の href="/dp/ASIN" / "/gp/product/ASIN" を拾う
    """
    H = str(html or "")

    # 1) canonical
    m = re.search(
        r'rel=["\']canonical["\'][^>]*href=["\']([^"\']+/(?:dp|gp/product)/[A-Z0-9]{10})',
        H, re.I
    )
    if m:
        u = m.group(1)
        return u if u.startswith("http") else f"https://{host.rstrip('/')}/{u.lstrip('/')}"

    # 2) ページ内リンク
    m = re.search(r'href=["\'](/?(?:dp|gp/product)/[A-Z0-9]{10})', H, re.I)
    if m:
        path = m.group(1)
        return path if path.startswith("http") else f"https://{host.rstrip('/')}/{path.lstrip('/')}"

    # 3) og:url に dp/gp/product が入っているケース
    m = re.search(r'property=["\']og:url["\'][^>]*content=["\']([^"\']+/(?:dp|gp/product)/[A-Z0-9]{10})', H, re.I)
    if m:
        u = m.group(1)
        return u if u.startswith("http") else f"https://{host.rstrip('/')}/{u.lstrip('/')}"

    # 4) 埋め込みASINから推定（data-asin や "ASIN":"XXXXXXXXXX"）
    m = re.search(r'(["\']ASIN["\']\s*:\s*["\']|data-asin=["\'])([A-Z0-9]{10})', H, re.I)
    if m:
        asin = m.group(2)
        return f"https://{host.rstrip('/')}/dp/{asin}"
    return None

