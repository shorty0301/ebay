# test_extractors.py
# -*- coding: utf-8 -*-
"""
supplier_extractors.py の抽出精度をURL単位で確認するワンショットテスター。
"""

import os, sys, json, time
from datetime import datetime
from urllib.parse import urlparse

# 詳細ログON
os.environ["EXTRACTOR_DEBUG"] = os.environ.get("EXTRACTOR_DEBUG", "1")

from supplier_extractors import fetch_html, extract_supplier_info  # ← host_from_url は使わない

TEST_URLS = [
    # ここに任意のURLを入れてもOK。Actions の inputs で渡せば空でも可。
]

def host_from(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower()
    except Exception:
        return ""

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
            info = extract_supplier_info(url, html)
            ms = int((time.time()-t0)*1000)
            print(json.dumps({
                "host": host_from(url),
                "stock": info.get("stock"),
                "qty": info.get("qty"),
                "price": info.get("price"),
                "debug": info.get("_debug", {})
            }, ensure_ascii=False, indent=2))
            print(f"→ {ms} ms\n")
            ok += 1
        except Exception as e:
            print(f"⚠️ 失敗: {e}\n")

    print(f"Done. success={ok}/{len(urls)}")

if __name__ == "__main__":
    urls = sys.argv[1:] if len(sys.argv) > 1 else TEST_URLS
    main(urls)
