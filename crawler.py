import os, re, json, time, requests
from pathlib import Path
from bs4 import BeautifulSoup
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime

# =====================================================
# 設定値（GitHub Secrets で設定）
# =====================================================
SHEET_ID   = os.environ.get("SHEET_ID", "")              # GoogleスプレッドシートのID
SHEET_NAME = os.environ.get("SHEET_NAME", "Listings_Input")
SCOPES     = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

STATE_DIR  = Path("state"); STATE_DIR.mkdir(exist_ok=True)
STATE_FILE = STATE_DIR / "inventory_state.json"

# ===== 通知ポリシー =====
MIN_PRICE_DIFF = 100                 # 価格が100円以上動いたら通知
NOTIFY_ON_STOCK = {"OUT_OF_STOCK"}   # 在庫ありは無視。品切れになった時だけ通知
SKIP_FIRST_TIME = True               # 初回（前回値がないSKU）は通知しない

# =====================================================
# SLACK通知
# =====================================================
def slack_notify(message: str):
    """
    Slack Incoming Webhook に通知する。
    - Webhook URL は環境変数 SLACK_WEBHOOK_URL から読む
    - Blocks形式で見やすく送信（フォールバックとしてtextも付与）
    """
    import os, requests
    url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not url:
        print("⚠️ SLACK_WEBHOOK_URL が未設定です")
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
# Google Sheets から SKU / URL を取得
# =====================================================
def _gspread_client():
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not raw:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON が未設定です。")
    info = json.loads(raw)
    cred = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(cred)

def load_suppliers_from_sheet(sheet_id=SHEET_ID, worksheet_name=SHEET_NAME):
    if not sheet_id:
        raise RuntimeError("SHEET_ID が未設定です。")

    gc = _gspread_client()
    ws = gc.open_by_key(sheet_id).worksheet(worksheet_name)
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
# 巡回メイン処理
# =====================================================
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

def main():
    # どちらか片方だけ使う想定。シートがマスターならこれでOK
    suppliers = load_suppliers_from_sheet()   # ← スプレッドシートからSKU/URL取得
    # suppliers = load_suppliers("suppliers.csv")  # CSVを使う場合

    state = load_state()
    changes = []

    for row in suppliers:
        sku, url = row["sku"], row["url"]

        try:
            html = fetch_html(url)
            stock, price = parse_stock_and_price(html)
        except Exception as e:
            # 通知はしないが、エラーメモは残す
            changes.append(f"⚠️ 取得失敗 {sku}\n{url}\n{e}")
            continue

        prev = state.get(sku, {})
        prev_stock = prev.get("stock")
        prev_price = prev.get("price")

        # ===== 通知条件 =====
        # 初回は通知しない
        if not prev and SKIP_FIRST_TIME:
            pass
        else:
            # 1) 在庫：在庫ありは無視。OUT_OF_STOCK になったら通知
            if stock and stock != prev_stock and stock in NOTIFY_ON_STOCK:
                changes.append(f"*{sku}* 在庫: {prev_stock} → *{stock}*\n{url}")

            # 2) 価格：100円以上変動したら通知
            if (price is not None) and (prev_price is not None):
                if abs(price - prev_price) >= MIN_PRICE_DIFF:
                    diff = price - prev_price
                    changes.append(
                        f"*{sku}* 価格: {prev_price:,} → *{price:,}*（{diff:+,}）\n{url}"
                    )

        # 状態を更新（通知有無に関わらず）
        import time
        state[sku] = {
            "stock": stock,
            "price": price,
            "url": url,
            "checked_at": int(time.time()),
        }

        time.sleep(0.3)  # サイトに優しく


    save_state(state)
    if changes:
       slack_notify("在庫巡回レポート\n\n" + "\n\n".join(changes))



