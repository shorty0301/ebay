# crawler.py
# -*- coding: utf-8 -*-
"""
在庫巡回 → 解析 → Slack通知 → Inventoryシート更新 までを一括で実行します。

【必要な環境変数（GitHub Secrets想定）】
- GOOGLE_SERVICE_ACCOUNT_JSON : サービスアカウントJSONの中身（文字列）
- SHEET_ID                    : GoogleスプレッドシートID
- SHEET_NAME                  : 読み取り元のタブ名（既定: Listings_Input）
- INV_SHEET                   : 書き込み先のタブ名（既定: Inventory）
- SLACK_WEBHOOK_URL           : Slack Incoming Webhook（任意）

【シート列】
Inventory のヘッダは足りなければ自動追加します。
SKU / Listing URL / SupplierStock / SupplierPrice / LastSupplierPrice / SourceURL / LastCheckedAt / Note
C列=在庫、D列=現在価格、E列=前回価格、G列=取得時刻、H列=NOTE

【通知ポリシー】
- 価格が MIN_PRICE_DIFF（既定100円）以上動いたら通知
- 在庫は OUT_OF_STOCK になったとき通知（初回は通知スキップ可能）
"""

import os, re, json, time, requests
from pathlib import Path
from bs4 import BeautifulSoup
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime

# =====================================================
# 設定値
# =====================================================
SHEET_ID   = os.environ.get("SHEET_ID", "")                  # スプレッドシートID
SHEET_NAME = os.environ.get("SHEET_NAME", "Listings_Input")  # 読み取り元
INV_SHEET  = os.environ.get("INV_SHEET",  "Inventory")       # 書き込み先
SCOPES     = ["https://www.googleapis.com/auth/spreadsheets"]# 書き込み可
JST_FMT    = "%Y/%m/%d %H:%M:%S"

STATE_DIR  = Path("state"); STATE_DIR.mkdir(exist_ok=True)
STATE_FILE = STATE_DIR / "inventory_state.json"

# ===== 通知ポリシー =====
MIN_PRICE_DIFF   = int(os.environ.get("MIN_PRICE_DIFF", "100"))  # 価格差しきい値
NOTIFY_ON_STOCK  = {"OUT_OF_STOCK"}                              # 在庫ありは通知対象外
SKIP_FIRST_TIME  = os.environ.get("SKIP_FIRST_TIME", "true").lower() == "true"

# ===== 2表記（在庫あり/在庫切れ）サイト =====
TWO_STATE_HOST_KEYS = [
    "offmall",                        # オフモール
    "auctions.yahoo",                 # ヤフオク
    "paypayfleamarket", "paypayfleamarket.yahoo",  # PayPayフリマ
    "rakuma", "fril",                 # ラクマ
    "geo-online", "geo-online.co", "geo-online.jp",# GEO
    "mercari",                        # メルカリ
    "suruga-ya", "surugaya",          # 駿河屋
    "treasure-f", "trefac"            # トレファク
]

INV_HEADERS = [
    "SKU","Listing URL","SupplierStock","SupplierPrice",
    "LastSupplierPrice","SourceURL","LastCheckedAt","Note"
]

# =====================================================
# Slack通知
# =====================================================
def slack_notify(message: str):
    url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not url:
        print("⚠️ SLACK_WEBHOOK_URL が未設定です（通知はスキップ）")
        return

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "在庫巡回レポート"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": message[:2800]}},
    ]
    payload = {"text": message[:2000], "blocks": blocks}

    try:
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code != 200:
            print(f"⚠️ Slack通知失敗: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"⚠️ Slack通知エラー: {e}")

# =====================================================
# Google Sheets クライアント
# =====================================================
def _gspread_client():
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not raw:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON が未設定です。")
    info = json.loads(raw)
    cred = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(cred)

def _open_ws(sheet_id: str, tab: str):
    gc = _gspread_client()
    return gc.open_by_key(sheet_id).worksheet(tab)

def _header_map(ws):
    headers = ws.row_values(1)
    return { (h or "").strip().lower(): i+1 for i,h in enumerate(headers) if (h or "").strip() }

def ensure_inventory_headers(ws):
    m = _header_map(ws)
    to_add = [h for h in INV_HEADERS if h.lower() not in m]
    if to_add:
        start = len(m) + 1
        ws.update(f"R1C{start}:R1C{start+len(to_add)-1}", [to_add])
        m = _header_map(ws)
    return m

def load_inventory_existing(ws, hmap):
    values = ws.get_all_values()
    out = {}
    for r in range(2, len(values)+1):
        row = values[r-1]
        def gv(name):
            c = hmap.get(name.lower())
            return row[c-1] if (c and c-1 < len(row)) else ""
        sku = (gv("sku") or "").strip()
        if not sku: 
            continue
        out[sku] = {
            "row": r,
            "SupplierStock": (gv("supplierstock") or "").strip(),
            "SupplierPrice": (gv("supplierprice") or "").replace(",",""),
            "LastSupplierPrice": (gv("lastsupplierprice") or "").replace(",",""),
        }
    return out

# =====================================================
# 読み取り元（Listings_Input）から SKU/URL を取得
# =====================================================
def load_suppliers_from_sheet(sheet_id=SHEET_ID, worksheet_name=SHEET_NAME):
    if not sheet_id:
        raise RuntimeError("SHEET_ID が未設定です。")
    ws = _open_ws(sheet_id, worksheet_name)
    rows = ws.get_all_values()
    if not rows:
        return []

    headers = [h.strip().lower() for h in rows[0]]

    def col_idx(cands):
        for c in cands:
            if c in headers:
                return headers.index(c)
        return None

    idx_sku = col_idx(["sku"])
    idx_url = col_idx(["sourceurl", "srcurl", "url", "商品url", "仕入れ元url"])

    if idx_sku is None or idx_url is None:
        raise RuntimeError(f"必要な列が見つかりません。headers={headers}")

    out = []
    for r in rows[1:]:
        try:
            sku = (r[idx_sku] or "").strip()
            url = (r[idx_url] or "").strip()
        except IndexError:
            continue
        if sku and url:
            out.append({"sku": sku, "url": url})
    return out

# =====================================================
# HTML取得 & 価格・在庫解析
# =====================================================
def fetch_html(url: str) -> str:
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36"
    r = requests.get(url, headers={"User-Agent": ua}, timeout=30)
    r.raise_for_status()
    r.encoding = r.apparent_encoding
    return r.text

def yen_to_int(text: str):
    t = text.replace("，", ",").replace("．", ".")
    t = re.sub(r"[^\d,]", "", t).replace(",", "")
    return int(t) if t.isdigit() else None

def parse_stock_and_price(html: str):
    """
    stock: IN_STOCK / OUT_OF_STOCK / LAST_ONE / UNKNOWN
    price: int or None
    """
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)

    stock = "UNKNOWN"
    if re.search(r"(売り切れ|在庫切れ|SOLD\s*OUT|販売終了|取扱い終了)", text, re.I):
        stock = "OUT_OF_STOCK"
    elif re.search(r"(在庫あり|即日|カートに入れる|購入手続き|今すぐ購入|ご注文手続き)", text, re.I):
        stock = "IN_STOCK"
    if re.search(r"残り\s*1\s*(点|個|枚|本)", text):
        stock = "LAST_ONE"

    stop = re.compile(r"(ポイント|付与|獲得|送料|手数料|実質|クーポン|割引|値引|上限)", re.I)
    hits = re.findall(r"[¥￥]?\s?\d{1,3}(?:[,，]\d{3})+|\b\d{3,7}\b", text)
    prices = []
    for h in hits:
        i = text.find(h)
        ctx = text[max(0, i-15): i+len(h)+15]
        if stop.search(ctx):
            continue
        n = yen_to_int(h)
        if n and 0 < n < 10_000_000:
            prices.append(n)
    price = min(prices) if prices else None

    return stock, price

# =====================================================
# 在庫ラベル判定（2表記/4表記）
# =====================================================
def _host_from_url(url: str) -> str:
    m = re.search(r"https?://([^/]+)/?", url or "", re.I)
    return (m.group(1).lower() if m else "").strip()

def decide_stock_label(url: str, stock_code: str, qty: int|None = None) -> str:
    """
    2表記サイト：IN_STOCK/LAST_ONE→在庫あり、OUT_OF_STOCK→在庫切れ
    4表記サイト：OUT_OF_STOCK→在庫切れ、LAST_ONE→残り1点、qty>=2→残りn点、その他→在庫あり
    """
    host = _host_from_url(url)
    two  = any(k in host for k in TWO_STATE_HOST_KEYS)

    if two:
        return "在庫あり" if stock_code != "OUT_OF_STOCK" else "在庫切れ"

    if stock_code == "OUT_OF_STOCK":
        return "在庫切れ"
    if stock_code == "LAST_ONE":
        return "残り1点"
    if qty and qty >= 2:
        return f"残り{qty}点"
    return "在庫あり"

# =====================================================
# 状態保存
# =====================================================
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

# =====================================================
# メイン
# =====================================================
def main():
    # 読み取り元
    suppliers = load_suppliers_from_sheet()

    # 書き込み先（Inventory）準備
    inv_ws   = _open_ws(SHEET_ID, INV_SHEET)
    inv_map  = ensure_inventory_headers(inv_ws)
    inv_exist = load_inventory_existing(inv_ws, inv_map)

    # A1ユーティリティ
    def a1(r, c): 
        return gspread.utils.rowcol_to_a1(r, c)

    batch_cells = []   # 既存行の更新
    append_rows = []   # 新規行の追記

    # 変更通知まとめ
    state  = load_state()
    changes = []

    for row in suppliers:
        sku, url = row["sku"], row["url"]

        try:
            html = fetch_html(url)
            stock_code, price = parse_stock_and_price(html)
        except Exception as e:
            changes.append(f"⚠️ 取得失敗 {sku}\n{url}\n{e}")
            continue

        # 通知判定
        prev = state.get(sku, {})
        prev_stock = prev.get("stock")
        prev_price = prev.get("price")

        if not prev and SKIP_FIRST_TIME:
            pass
        else:
            if stock_code and stock_code != prev_stock and stock_code in NOTIFY_ON_STOCK:
                changes.append(f"*{sku}* 在庫: {prev_stock} → *{stock_code}*\n{url}")

            if (price is not None) and (prev_price is not None):
                if abs(price - prev_price) >= MIN_PRICE_DIFF:
                    diff = price - prev_price
                    changes.append(
                        f"*{sku}* 価格: {prev_price:,} → *{price:,}*（{diff:+,}）\n{url}"
                    )

        # state更新
        state[sku] = {
            "stock": stock_code,
            "price": price,
            "url": url,
            "checked_at": int(time.time()),
        }

        # ===== Inventory書き込み =====
        label = decide_stock_label(url, stock_code)    # C列表示
        now   = datetime.now().strftime(JST_FMT)

        prev_inv = inv_exist.get(sku)
        price_now = price

        # 既存価格の数値化
        try:
            prev_price_num = int(prev_inv["SupplierPrice"]) if (prev_inv and prev_inv["SupplierPrice"]) else None
        except:
            prev_price_num = None

        if prev_inv:
            r = prev_inv["row"]

            # 価格が変わったら D→E に退避
            if (prev_price_num is not None) and (price_now is not None) and (prev_price_num != price_now):
                batch_cells.append({"range": a1(r, inv_map["lastsupplierprice"]), "values": [[prev_price_num]]})

            note = "残り1点" if label == "残り1点" else ("在庫切れ" if label == "在庫切れ" else "")

            batch_cells += [
                {"range": a1(r, inv_map["supplierstock"]), "values": [[label]]},                    # C
                {"range": a1(r, inv_map["supplierprice"]), "values": [[("" if price_now is None else price_now)]]},  # D
                {"range": a1(r, inv_map["listing url"]),   "values": [[url]]},                      # B
                {"range": a1(r, inv_map["sourceurl"]),     "values": [[url]]},                      # F
                {"range": a1(r, inv_map["lastcheckedat"]), "values": [[now]]},                      # G
                {"range": a1(r, inv_map["note"]),          "values": [[note]]},                     # H
            ]
        else:
            # 追加行
            rowvals = [""] * len(INV_HEADERS)
            def setv(col_name, val):
                idx = INV_HEADERS.index(col_name)
                rowvals[idx] = val
            setv("SKU", sku)
            setv("Listing URL", url)
            setv("SupplierStock", label)
            setv("SupplierPrice", "" if price_now is None else price_now)
            setv("LastSupplierPrice", "")
            setv("SourceURL", url)
            setv("LastCheckedAt", now)
            setv("Note", "残り1点" if label == "残り1点" else ("在庫切れ" if label == "在庫切れ" else ""))
            append_rows.append(rowvals)

        time.sleep(0.3)  # サイト負荷配慮

    # ===== Inventory反映 =====
    if batch_cells:
        inv_ws.batch_update(batch_cells)
    if append_rows:
        inv_ws.append_rows(append_rows, value_input_option="USER_ENTERED")

    save_state(state)

    # ===== Slack通知 =====
    if changes:
        slack_notify("在庫巡回レポート\n\n" + "\n\n".join(changes))

# -----------------------------------------------------
if __name__ == "__main__":
    main()
