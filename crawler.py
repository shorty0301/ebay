import os, csv, json, time, re
from pathlib import Path
import requests
from bs4 import BeautifulSoup

LINE_TOKEN = os.environ.get("LINE_CHANNEL_TOKEN", "")
LINE_TO = [s for s in (os.environ.get("LINE_TO", "")).replace(" ", "").split(",") if s]

STATE_DIR = Path("state"); STATE_DIR.mkdir(exist_ok=True)
STATE_FILE = STATE_DIR / "inventory_state.json"

def load_suppliers(csv_path="suppliers.csv"):
    out = []
    with open(csv_path, encoding="utf-8") as f:
        for i, row in enumerate(csv.DictReader(f)):
            sku = (row.get("sku") or "").strip()
            url = (row.get("url") or "").strip()
            if sku and url:
                out.append({"sku": sku, "url": url})
    return out

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

def line_push(msg: str):
    if not (LINE_TOKEN and LINE_TO):
        return
    for to in LINE_TO:
        try:
            requests.post(
                "https://api.line.me/v2/bot/message/push",
                headers={"Authorization": f"Bearer {LINE_TOKEN}", "Content-Type": "application/json"},
                json={"to": to, "messages": [{"type": "text", "text": msg[:1000]}]},
                timeout=15,
            )
        except Exception:
            pass

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

def main():
    suppliers = load_suppliers_from_sheet()
    state = load_state()
    changes = []

    for row in suppliers:
        sku, url = row["sku"], row["url"]
        try:
            html = fetch_html(url)
            stock, price = parse_stock_and_price(html)
        except Exception as e:
            changes.append(f"⚠️ 取得失敗 {sku}\n{url}\n{e}")
            continue

        prev = state.get(sku, {})
        prev_stock, prev_price = prev.get("stock"), prev.get("price")

        # 初回は通知を抑止したい場合は「prevがあるときだけ」変化通知
        if prev:
            if stock and stock != prev_stock:
                changes.append(f"【在庫変化】{sku}\n{prev_stock} → {stock}\n{url}")
            if price is not None and prev_price is not None and price != prev_price:
                diff = price - prev_price
                changes.append(f"【価格変動】{sku}\n{prev_price:,} → {price:,}（差分 {diff:+,}）\n{url}")

        state[sku] = {"stock": stock, "price": price, "url": url, "checked_at": int(time.time())}

        time.sleep(0.3)  # 丁寧に

    save_state(state)
    if changes:
        line_push("在庫巡回レポート\n\n" + "\n\n".join(changes))

if __name__ == "__main__":
    main()

