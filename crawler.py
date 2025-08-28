# ===== ここを crawler.py の先頭付近（import 群の下あたり）に追記 =====
import os, json
import gspread
from google.oauth2.service_account import Credentials

SHEET_ID   = os.environ.get("SHEET_ID", "")              # GitHub Secrets
SHEET_NAME = os.environ.get("SHEET_NAME", "Listings_Input")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

def _gspread_client():
    # Secrets: GOOGLE_SERVICE_ACCOUNT_JSON に JSON 文字列をそのまま入れている前提
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not raw:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON が未設定です。")
    info = json.loads(raw)
    cred = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(cred)

def load_suppliers_from_sheet(sheet_id=SHEET_ID, worksheet_name=SHEET_NAME):
    """ヘッダ名が SKU / sku、SourceURL / SRCURL / url などでも拾う"""
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

def line_push(msg: str):
    """LINE通知"""
    if not (LINE_TOKEN and LINE_TO):
        print("LINE設定なし。通知スキップ:", msg)
        return
    for to in LINE_TO:
        try:
            requests.post(
                "https://api.line.me/v2/bot/message/push",
                headers={
                    "Authorization": f"Bearer {LINE_TOKEN}",
                    "Content-Type": "application/json"
                },
                json={"to": to, "messages": [{"type": "text", "text": msg[:1000]}]},
                timeout=15,
            )
        except Exception as e:
            print("通知失敗:", e)

if __name__ == "__main__":
    from datetime import datetime
    line_push(f"✅ テスト通知：GitHub Actions から送信 {datetime.now():%Y-%m-%d %H:%M:%S}")




