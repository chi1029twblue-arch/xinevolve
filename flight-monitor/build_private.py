#!/usr/bin/env python3
"""把儀表板和最新票價資料打包成單一 HTML（不需另外讀 data/history.json），用來發布成私人頁面。

用法：python3 flight-monitor/build_private.py 輸出路徑.html
較舊的快照只保留畫走勢需要的兩筆（全窗口最低、連假出發最低），讓檔案保持精簡。
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def compact(history: dict) -> dict:
    hol = history["config"]["holiday"]
    snaps = history["snapshots"]
    out = []
    for i, s in enumerate(snaps):
        offers = s.get("offers", [])
        if i < len(snaps) - 1 and offers:
            keep = [min(offers, key=lambda o: o["price"])]
            hol_offers = [o for o in offers if hol["start"] <= o["depart"] <= hol["end"]]
            if hol_offers:
                keep.append(min(hol_offers, key=lambda o: o["price"]))
            s = {**s, "offers": keep}
        out.append(s)
    return {**history, "snapshots": out}


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 1
    history = compact(json.loads((ROOT / "data" / "history.json").read_text(encoding="utf-8")))
    data = json.dumps(history, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    page = (ROOT / "index.html").read_text(encoding="utf-8")
    marker = '<script type="application/json" id="embedded-data"></script>'
    assert marker in page, "index.html 缺少 embedded-data 區塊"
    page = page.replace(marker, f'<script type="application/json" id="embedded-data">{data}</script>')
    # 私人頁面平台會自己包上 <html>/<head>/<body>，這裡只留 title、樣式與內容
    head = page.split("<head>", 1)[1].split("</head>", 1)[0]
    head = "\n".join(l for l in head.splitlines() if "<meta" not in l)
    body = page.split("<body>", 1)[1].rsplit("</body>", 1)[0]
    page = head.strip() + "\n" + body.strip() + "\n"
    Path(sys.argv[1]).write_text(page, encoding="utf-8")
    print(f"已輸出 {sys.argv[1]}（{len(page) // 1024} KB，{len(history['snapshots'])} 次觀測）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
