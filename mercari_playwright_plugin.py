# mercari_playwright_plugin.py
# -*- coding: utf-8 -*-
"""
Mercari（jp.mercari.com）価格抽出を Playwright で“補完”するプラグイン。
- 既存の supplier_extractors の結果を上書きしない（price が None のときのみ採用）
- Playwright 未導入・起動失敗時は静かにスキップ（None返し）
- import されるだけで自動登録されます
"""

import re, json
from typing import Optional

# supplier_extractors.py のプラグインAPIを利用
from supplier_extractors import register_simple_plugin, to_int_yen

# -------- Playwright / nest_asyncio の有無を安全に判定 --------
try:
    import asyncio
except Exception:
    asyncio = None  # 無い環境でも動作継続

try:
    import nest_asyncio
    if asyncio is not None:
        try:
            # 既存ループが走っていても二重実行できるようパッチ
            nest_asyncio.apply()
        except Exception:
            pass
except Exception:
    pass  # nest_asyncio 無くても問題なし（必要時のみ使う）

try:
    from bs4 import BeautifulSoup
except Exception:
    BeautifulSoup = None  # ない場合は後段の保険ロジックが一部無効になるだけ

try:
    from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError
except Exception:
    async_playwright = None
    class PWTimeoutError(Exception):
        pass

# --------- ここからユーザー提供ロジックをベースに移植/安全化 ---------

YEN_RE = re.compile(r"(?:￥|¥)\s*([0-9０-９][0-9０-９,，]{2,})")
LABEL_WORDS = ("税込", "送料込", "送料込み")

def _to_int_digits(s: str) -> Optional[int]:
    s = (s or "").replace("，", ",")
    s = re.sub(r"[^\d]", "", s)
    return int(s) if s.isdigit() else None

async def _fetch_price_playwright(url: str, headless: bool = True, timeout_ms: int = 60000, retries: int = 2):
    """
    非同期：PlaywrightでDOM/JSON-LDから価格候補を抽出
    返り値例: {"price": 12345, "source": "dom:near_buy"} / {"status":"price_not_found"} など
    """
    if async_playwright is None:
        return {"status": "playwright_not_available"}

    async with async_playwright() as pw:
        for attempt in range(1, retries + 1):
            browser = await pw.chromium.launch(headless=headless)
            ctx = await browser.new_context(
                locale="ja-JP",
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0 Safari/537.36"),
                extra_http_headers={"Accept-Language": "ja,en;q=0.8"}
            )
            page = await ctx.new_page()
            try:
                # networkidle だと待ちすぎるケースがあるので domcontentloaded
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                # 軽く待つ＋「購入手続き」ボタンの出現を待機
                await page.wait_for_timeout(700)
                await page.wait_for_selector(
                    "xpath=(//button[contains(., '購入手続き')] | //a[contains(., '購入手続き')])[1]",
                    timeout=8000
                )
            except PWTimeoutError:
                await browser.close()
                if attempt < retries:
                    continue
                return {"status": "timeout_goto"}

            # 1) 購入ボタン近傍の “¥…” テキスト（ラベル付き優先）
            btn = page.locator("xpath=(//button[contains(., '購入手続き')] | //a[contains(., '購入手続き')])[1]")
            if await btn.count() > 0:
                lab = page.locator(
                    "xpath=(//*[contains(., '¥') and (contains(., '税込') or contains(., '送料込') or contains(., '送料込み')) "
                    " and preceding::*[(self::button or self::a) and contains(., '購入手続き')]][last()])"
                )
                if await lab.count() > 0:
                    txt = await lab.inner_text()
                    m = YEN_RE.search(txt or "")
                    if m:
                        n = _to_int_digits(m.group(1))
                        if n:
                            await browser.close()
                            return {"price": n, "source": "dom:near_buy+labeled"}

                near = page.locator(
                    "xpath=(//*[contains(., '¥') and preceding::*[(self::button or self::a) and contains(., '購入手続き')]][last()])"
                )
                if await near.count() > 0:
                    txt = await near.inner_text()
                    m = YEN_RE.search(txt or "")
                    if m:
                        n = _to_int_digits(m.group(1))
                        if n:
                            await browser.close()
                            return {"price": n, "source": "dom:near_buy"}

            # 2) JSON-LD / meta の保険
            html = await page.content()
            soup = BeautifulSoup(html, "lxml") if BeautifulSoup else None
            if soup:
                for tag in soup.find_all("script", {"type": "application/ld+json"}):
                    try:
                        data = json.loads(tag.string or "")
                    except Exception:
                        continue
                    stack = [data]
                    while stack:
                        node = stack.pop()
                        if isinstance(node, dict):
                            t = str(node.get("@type", "")).lower()
                            if t in ("offer", "aggregateoffer"):
                                if "price" in node and _to_int_digits(str(node["price"])) is not None:
                                    await browser.close()
                                    return {"price": _to_int_digits(str(node["price"])), "source": "jsonld:price"}
                                if "lowPrice" in node and _to_int_digits(str(node["lowPrice"])) is not None:
                                    await browser.close()
                                    return {"price": _to_int_digits(str(node["lowPrice"])), "source": "jsonld:lowPrice"}
                            stack.extend(node.values())
                        elif isinstance(node, list):
                            stack.extend(node)

                tag = soup.find("meta", attrs={"name": "product:price:amount"}) if soup else None
                if tag and tag.get("content"):
                    n = _to_int_digits(tag["content"])
                    if n:
                        await browser.close()
                        return {"price": n, "source": "meta:product:price:amount"}

                # 3) 可視テキスト走査（最後の保険）
                visible = soup.get_text(" ", strip=True)
                best = None
                for m in YEN_RE.finditer(visible):
                    seg = visible[max(0, m.start() - 20): m.end() + 20]
                    if any(w in seg for w in LABEL_WORDS):
                        best = _to_int_digits(m.group(1))
                        if best:
                            break
                if best is None:
                    nums = [_to_int_digits(m.group(1)) for m in YEN_RE.finditer(visible)]
                    nums = [n for n in nums if n and 100 <= n <= 3_000_000]
                    if nums:
                        from collections import Counter
                        best = Counter(nums).most_common(1)[0][0]

                await browser.close()
                return {"price": best, "source": "visible_text"} if best else {"status": "price_not_found"}

            # BeautifulSoup が無い場合はここまで
            await browser.close()
            return {"status": "price_not_found"}

def _run_async_fetch(url: str) -> Optional[int]:
    """
    同期ラッパ：既存フロー（同期）から安全に呼べるようにする。
    - イベントループが無ければ新規作成
    - 既に走っていれば nest_asyncio により再入可能化（可能な範囲）
    """
    if asyncio is None or async_playwright is None:
        return None
    try:
        loop = asyncio.get_event_loop()
    except Exception:
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
        except Exception:
            pass

    try:
        result = loop.run_until_complete(_fetch_price_playwright(url))
    except RuntimeError:
        # 既存ループ稼働中（Jupyter等）の場合は ensure_future 経由で回避
        coro = _fetch_price_playwright(url)
        task = asyncio.ensure_future(coro)
        loop.run_until_complete(task)
        result = task.result()
    except Exception:
        return None

    if isinstance(result, dict) and "price" in result and isinstance(result["price"], int):
        # 最終安全化（上限チェック）
        v = to_int_yen(str(result["price"]))
        return v
    return None

# --------- プラグイン登録 ---------
def _price_fn(url: str, html: str, text: str) -> Optional[int]:
    """
    plugin の price_fn: supplier_extractors 側から呼ばれる。
    - 既存で price が取れている場合は採用されない（override=False）
    - ここでは URL を直接 Playwright で再取得して判定する
    """
    try:
        return _run_async_fetch(url)
    except Exception:
        return None

# Mercari 対象ホストのパターン
_MERCARI_HOSTS = [
    r"\bmercari\.com\b",
    r"\bjp\.mercari\.com\b",
    r"\bmercari\.jp\b",
]

# 既存結果の“補完”のみ（override=False）
register_simple_plugin(
    name="mercari_playwright_price",
    host_regexes=_MERCARI_HOSTS,
    price_fn=_price_fn,
    stock_fn=None,
    override=False,   # 既存で取れていれば上書きしない
    priority=20       # 必要に応じて優先度を高めておく
)
