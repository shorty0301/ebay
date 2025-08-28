# -*- coding: utf-8 -*-
"""
在庫巡回クローラ(完全版)
- Listings_Input から SKU / 仕入れ元URL（SourceURL）等を読み込み
- 各URLの在庫/価格を抽出（supplier_extractors.py を使用）
- 在庫管理シート（既定: 在庫管理）へ反映：
    C列: 在庫ラベル（日本語）
    D列: 価格（最新）
    E列: 前回価格
    G列: 取得日時（JST, yyyy-mm-dd HH:MM）
    H列: NOTE（通知方針/エラー等）
- 前回値との比較で Slack 通知（価格変動／在庫切れなど）
環境変数:
  SHEET_ID, SHEET_INPUT(=Listings_Input), SHEET_INV(=在庫管理), GOOGLE_SERVICE_ACCOUNT_JSON, SLACK_WEBHOOK_URL
  MIN_PRICE_DIFF (default 100), NOTIFY_ON_STOCK (csv: OUT_OF_STOCK,LAST_ONE など), SKIP_FIRST_TIME (1/0)
"""
import os, json, time, math, re
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Tuple

import gspread
from google.oauth2.service_account import Credentials
import requests

from supplier_extractors import fetch_and_extract, extract_supplier_info

# ================= 設定 =================
SHEET_ID    = os.environ.get("SHEET_ID", "").strip()
SHEET_INPUT = os.environ.get("SHEET_INPUT", "Listings_Input")
SHEET_INV   = os.environ.get("SHEET_INV", "在庫管理")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
STATE_DIR  = Path("state"); STATE_DIR.mkdir(exist_ok=True)
STATE_FILE = STATE_DIR / "inventory_state.json"

MIN_PRICE_DIFF    = int(os.environ.get("MIN_PRICE_DIFF", "100"))
NOTIFY_ON_STOCK   = set([s.strip().upper() for s in os.environ.get("NOTIFY_ON_STOCK", "OUT_OF_STOCK").split(",") if s.strip()])
SKIP_FIRST_TIME   = os.environ.get("SKIP_FIRST_TIME", "1") not in ("0","false","False")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL","").strip()

JST = timezone(timedelta(hours=9))

# ================ Slack =================
def slack_notify(message: str):
    url = SLACK_WEBHOOK_URL
    if not url:
        print("⚠️ SLACK_WEBHOOK_URL 未設定のため通知スキップ")
        return
    payload = {
        "text": message[:2000],
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": "在庫巡回レポート"}},
            {"type": "section","text":{"type":"mrkdwn","text":message[:2800]}}
        ]
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code != 200:
            print(f"⚠️ Slack通知失敗: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"⚠️ Slack通知エラー: {e}")

# ============== Google Sheets ==============
def _gspread_client():
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON が未設定です。")
    info = json.loads(raw)
    cred = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(cred)

def _open_ws(sheet_id: str, name: str):
    gc = _gspread_client()
    sh = gc.open_by_key(sheet_id).worksheet(name)
    return sh

def _headers_map(ws) -> Dict[str, int]:
    """ヘッダ名(小文字) -> 1-based col index"""
    row = ws.row_values(1)
    m = {}
    for i, h in enumerate(row, start=1):
        k = (h or "").strip().lower()
        if k: m[k]=i
    return m

def _col_letter(n: int) -> str:
    s = ""
    while n>0:
        n, r = divmod(n-1, 26)
        s = chr(65+r) + s
    return s

# ============== データ取得 ==============
def load_input_rows() -> List[Dict[str,str]]:
    ws = _open_ws(SHEET_ID, SHEET_INPUT)
    rows = ws.get_all_values()
    if not rows: return []
    headers = [h.strip().lower() for h in rows[0]]
    def idx(*cands):
        for c in cands:
            if c in headers: return headers.index(c)
        return -1
    i_sku    = idx("sku")
    i_srcurl = idx("sourceurl","srcurl","url","商品url","仕入れ元url")
    i_ebay   = idx("ebayid","ebay_id")
    out=[]
    for r in rows[1:]:
        sku = (r[i_sku] if i_sku>=0 and i_sku<len(r) else "").strip()
        url = (r[i_srcurl] if i_srcurl>=0 and i_srcurl<len(r) else "").strip()
        ebay= (r[i_ebay] if i_ebay>=0 and i_ebay<len(r) else "").strip()
        if not sku: continue
        listing = f"https://www.ebay.com/itm/{ebay}" if ebay else ""
        out.append({"sku":sku, "url":url, "listing":listing})
    return out

# ============== 在庫ラベル決定（サイト別ポリシー） ==============
def host_of(url: str) -> str:
    m = re.match(r"^[a-z]+://([^/?#]+)", str(url or ""), re.I)
    return m.group(1).lower() if m else ""

TWO_LABEL_HOSTS = re.compile(
    r"(netmall\.hardoff\.co\.jp|auctions\.yahoo\.co\.jp|paypayfleamarket\.yahoo\.co\.jp|"
    r"(?:fril\.jp|rakuma\.rakuten\.co\.jp)|geo-online\.co\.jp|mercari|suruga-ya\.jp|treasure-f\.com)"
)

def stock_label_for_site(url: str, stock: str, qty: str) -> str:
    stock = (stock or "UNKNOWN").upper()
    h = host_of(url)
    is_two = bool(TWO_LABEL_HOSTS.search(h))
    if is_two:
        return "在庫あり" if stock != "OUT_OF_STOCK" else "在庫なし"
    # 4表記（デフォルト）
    if stock == "OUT_OF_STOCK":
        return "在庫切れ"
    if stock == "LAST_ONE":
        return "残り1点"
    if stock == "IN_STOCK":
        try:
            n = int(qty) if qty and str(qty).isdigit() else None
            if n is not None and n>1:
                return f"残り{n}点"
        except: pass
        return "在庫あり"
    return "不明"

# ============== 状態保存 ==============
STATE_DIR = Path("state"); STATE_DIR.mkdir(exist_ok=True)
STATE_FILE = STATE_FILE

def load_state() -> Dict[str, Any]:
    if STATE_FILE.exists():
        try: return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except: return {}
    return {}

def save_state(state: Dict[str,Any]):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

# ============== 在庫管理シートの列位置 ==============
def resolve_inventory_columns(ws) -> Dict[str,int]:
    """
    既定: C=Stock, D=Price, E=LastPrice, G=CheckedAt, H=Note
    可能ならヘッダ名でも解決（優先）: supplierstock, supplierprice, lastsupplierprice, lastcheckedat, note, sourceurl, listingurl, sku
    """
    hm = _headers_map(ws)
    def get_by_header(*names):
        for n in names:
            i = hm.get(n)
            if i: return i
        return None
    col = {}
    col["sku"]          = get_by_header("sku") or 1
    col["sourceurl"]    = get_by_header("sourceurl","srcurl","url","仕入れ元url") or 2
    col["listingurl"]   = get_by_header("listingurl") or 0
    col["stock"]        = get_by_header("supplierstock","stock") or 3
    col["price"]        = get_by_header("supplierprice","price") or 4
    col["last_price"]   = get_by_header("lastsupplierprice","lastprice") or 5
    col["checked_at"]   = get_by_header("lastcheckedat","checked_at") or 7
    col["note"]         = get_by_header("note") or 8
    return col

# ============== Inventory 行の索引（SKU->row） ==============
def build_row_index(ws, col_sku: int) -> Dict[str,int]:
    vals = ws.col_values(col_sku)[1:]  # 2行目以降
    return { (v or "").strip(): i+2 for i,v in enumerate(vals) if (v or "").strip() }

# ============== メイン処理 ==============
def main():
    if not SHEET_ID: raise RuntimeError("SHEET_ID が未設定です。")
    ws_inv = _open_ws(SHEET_ID, SHEET_INV)

    input_rows = load_input_rows()
    if not input_rows:
        print("入力行が空です。終了")
        return

    inv_cols = resolve_inventory_columns(ws_inv)
    row_map = build_row_index(ws_inv, inv_cols["sku"])

    # 既存行がないSKUは追記
    append_batch = []
    for r in input_rows:
        sku = r["sku"]
        if sku not in row_map:
            row = [""] * max(ws_inv.col_count, 10)
            row[inv_cols["sku"]-1]       = sku
            if inv_cols.get("sourceurl"):  row[inv_cols["sourceurl"]-1] = r.get("url","" )
            if inv_cols.get("listingurl") and inv_cols["listingurl"]>0:
                row[inv_cols["listingurl"]-1] = r.get("listing","" )
            append_batch.append(row)
    if append_batch:
        start_row = ws_inv.row_count + 1
        ws_inv.add_rows(len(append_batch))
        ws_inv.update(f"A{start_row}:{_col_letter(len(append_batch[0]))}{start_row+len(append_batch)-1}", append_batch)
        row_map = build_row_index(ws_inv, inv_cols["sku"])

    state = load_state()
    changes = []

    for r in input_rows:
        sku = r["sku"]; url = r.get("url","" ); listing = r.get("listing","" )
        if not sku: continue
        row_no = row_map.get(sku)
        if not row_no: continue

        note_msgs = []
        try:
            info = fetch_and_extract(url) if url else {"stock":"UNKNOWN","qty":"","price":None}
            stock = info.get("stock","UNKNOWN")
            qty   = info.get("qty","") or ""
            price = info.get("price", None)
        except Exception as e:
            stock, qty, price = "UNKNOWN", "", None
            note_msgs.append(f"取得失敗: {e}")

        label = stock_label_for_site(url, stock, qty)

        prev = state.get(sku, {})
        prev_stock = prev.get("stock")
        prev_price = prev.get("price")

        # シートE(前回価格)が数字ならそれを prev に採用
        try:
            if inv_cols.get("last_price"):
                last_p_cell = ws_inv.cell(row_no, inv_cols["last_price"]).value
                if last_p_cell and last_p_cell.strip().isdigit():
                    prev_price = int(last_p_cell.strip())
        except: pass

        if not prev and SKIP_FIRST_TIME:
            pass
        else:
            if stock and prev_stock and stock != prev_stock and stock.upper() in NOTIFY_ON_STOCK:
                changes.append(f"*{sku}* 在庫: {prev_stock} → *{stock}*\n{url or listing}")
            if (price is not None) and (prev_price is not None):
                if abs(int(price) - int(prev_price)) >= MIN_PRICE_DIFF:
                    diff = int(price) - int(prev_price)
                    changes.append(f"*{sku}* 価格: {int(prev_price):,} → *{int(price):,}*（{diff:+,}）\n{url or listing}")

        # シート更新
        try:
            nowj = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
            # E(前回価格) ← D(最新) をコピー
            if inv_cols.get("last_price") and inv_cols.get("price"):
                cur_d = ws_inv.cell(row_no, inv_cols["price"]).value
                if cur_d and cur_d.strip().isdigit():
                    ws_inv.update_cell(row_no, inv_cols["last_price"], int(cur_d))

            updates = []
            if inv_cols.get("stock"):
                updates.append({"range": gspread.utils.rowcol_to_a1(row_no, inv_cols["stock"]), "values":[[label]]})
            if inv_cols.get("price"):
                updates.append({"range": gspread.utils.rowcol_to_a1(row_no, inv_cols["price"]), "values":[[("" if price is None else int(price))]]})
            if inv_cols.get("checked_at"):
                updates.append({"range": gspread.utils.rowcol_to_a1(row_no, inv_cols["checked_at"]), "values":[[nowj]]})
            if inv_cols.get("note"):
                if not note_msgs:
                    note_msgs.append("在庫切れ/LAST1をSlackで通知。価格は±{}円以上で通知。".format(MIN_PRICE_DIFF))
                updates.append({"range": gspread.utils.rowcol_to_a1(row_no, inv_cols["note"]), "values":[[" / ".join(note_msgs)]]})
            if updates:
                ws_inv.batch_update([{"range": u["range"], "values": u["values"]} for u in updates])
        except Exception as e:
            print(f"⚠️ シート更新エラー {sku}: {e}")

        state[sku] = {
            "stock": stock,
            "price": (None if price is None else int(price)),
            "url": url,
            "checked_at": int(time.time())
        }
        time.sleep(0.2)

    save_state(state)
    if changes:
        slack_notify("在庫巡回レポート\n\n" + "\n\n".join(changes))

if __name__ == "__main__":
    main()
