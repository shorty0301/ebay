# test_extractors.py
# -*- coding: utf-8 -*-
"""
supplier_extractors.py の抽出精度をURL単位で確認するワンショットテスター。
- 指定URLごとに: fetch_html → extract_supplier_info → 結果とログを標準出力
- どの抽出関数が採用されたか、候補価格なども表示（DEBUG）
"""

import os, sys, json, time
from datetime import datetime

# ログを詳細出力
os.environ["EXTRACTOR_DEBUG"] = os.environ.get("EXTRACTOR_DEBUG", "1")

from supplier_extractors import fetch_html, extract_supplier_info, host_from_url

# === ここに検証したいURLを並べてください（ドメイン混在OK） ===
TEST_URLS = [
    # オフモール
    # "https://netmall.hardoff.co.jp/product/xxxxxxxxxxxxxx",
    # ヤフオク
    # "https://auctions.yahoo.co.jp/some/item/xxxxxxxx",
    # PayPayフリマ
    # "https://paypayfleamarket.yahoo.co.jp/item/xxxxxxxx",
    # ラクマ
    # "https://fril.jp/item/xxxxxxxx"  or "https://rakuma.rakuten.co.jp/item/xxxxxxxx",
    # 駿河屋
    # "https://www.suruga-ya.jp/product/detail/xxxxxxxx",
    # トレファク
    # "https://ec.treasure-f.com/item/xxxxxxxx",
    # 楽天(books含む)
    # "https://item.rakuten.co.jp/xxxx/xxxx" / "https://books.rakuten.co.jp/rb/xxxxxxx/",
]

def main(urls):
    if not urls:
        print("❌ テストURLが空です。TEST_URLS に追加するか、引数でURLを渡してください。")
        sys.exit(1)

    print("=== Extractor smoke test ===")
    print("Date:", datetime.now().isoformat())
    print("Count:", len(urls))
    print()

    ok = 0
    for i, url in enumerate(urls, 1):
        print(f"[{i}/{len(urls)}] {url}")
        t0 = time.time()
        try:
            html = fetch_html(url)
            info = extract_supplier_info(url, html, debug=True)  # ← debug=True で詳細ログ
            ms = int((time.time()-t0)*1000)
            print(json.dumps({
                "host": host_from_url(url),
                "stock": info.get("stock"),
                "qty": info.get("qty"),
                "price": info.get("price"),
                "debug": info.get("_debug", {})  # どの抽出器が当たったか等
            }, ensure_ascii=False, indent=2))
            print(f"→ {ms} ms\n")
            ok += 1
        except Exception as e:
            print(f"⚠️ 失敗: {e}\n")

    print(f"Done. success={ok}/{len(urls)}")

if __name__ == "__main__":
    # 引数にURLがあればそれを使う: 例) python test_extractors.py https://... https://...
    urls = sys.argv[1:] if len(sys.argv) > 1 else TEST_URLS
    main(urls)
