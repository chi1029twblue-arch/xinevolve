#!/usr/bin/env python3
"""台北 → 東京 春節機票觀測器。

每次執行：
  1. 從 Travelpayouts（Aviasales Data API，免費）抓取出發窗口內所有來回組合的最低價
  2. （選用）把目前最便宜的前幾組日期丟到 Google 航班（SerpApi）複查即時價與「價格水準」
  3. 把結果附加到 data/history.json，供 index.html 儀表板畫圖
  4. 若出現新低價或低於目標價，透過 Telegram / Discord 通知

只使用 Python 標準函式庫，不需 pip install。

環境變數：
  TRAVELPAYOUTS_TOKEN   必填，https://www.travelpayouts.com 註冊後於「API token」取得
  SERPAPI_KEY           選填，https://serpapi.com（免費方案每月 250 次查詢）
  TELEGRAM_BOT_TOKEN    選填，搭配 TELEGRAM_CHAT_ID 傳送通知
  TELEGRAM_CHAT_ID
  DISCORD_WEBHOOK_URL   選填
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
HISTORY_PATH = ROOT / "data" / "history.json"
TAIPEI = timezone(timedelta(hours=8))

TRAVELPAYOUTS_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
SERPAPI_URL = "https://serpapi.com/search.json"
TOKYO_AIRPORTS = "NRT,HND"
TOKYO = timezone(timedelta(hours=9))

# 廉價航空：票價不含託運行李，需另加行李費
LCC_AIRLINES = {
    "IT", "MM", "GK", "JQ", "3K", "VZ", "FD", "AK", "D7", "TR", "UO", "5J", "Z2", "IJ", "ZG",
    "7C", "TW", "LJ", "BX", "ZE", "RS", "9C", "HB", "DD", "SL", "OD",
}


def local_time(iso: str, tz: timezone) -> datetime:
    """把 API 的時間轉成當地時間；沒有時區資訊時視為已是當地時間。"""
    t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return t.astimezone(tz) if t.tzinfo else t.replace(tzinfo=tz)


def time_ok(t: datetime, window: list[str]) -> bool:
    return window[0] <= t.strftime("%H:%M") <= window[1]


def http_get_json(url: str, params: dict, headers: dict | None = None) -> dict:
    full = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(full, headers={"Accept": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def http_post_json(url: str, payload: dict) -> None:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30):
        pass


def months_between(start: date, end: date) -> list[str]:
    months, cur = [], start.replace(day=1)
    while cur <= end:
        months.append(cur.strftime("%Y-%m"))
        cur = (cur + timedelta(days=32)).replace(day=1)
    return months


def in_window(cfg: dict, depart: str, ret: str) -> bool:
    d, r = date.fromisoformat(depart), date.fromisoformat(ret)
    trip = (r - d).days
    return (
        date.fromisoformat(cfg["depart_from"]) <= d <= date.fromisoformat(cfg["depart_to"])
        and cfg["min_trip_days"] <= trip <= cfg["max_trip_days"]
    )


def fetch_travelpayouts(cfg: dict, token: str) -> list[dict]:
    """回傳窗口內每組（出發日, 回程日）最便宜的一筆報價。

    Travelpayouts 的資料是 Aviasales 使用者近 48 小時內實際搜尋到的價格快取，
    冷門日期可能暫時沒有資料，屬正常現象。
    """
    dep_from, dep_to = date.fromisoformat(cfg["depart_from"]), date.fromisoformat(cfg["depart_to"])
    dep_months = months_between(dep_from, dep_to)
    ret_months = months_between(
        dep_from + timedelta(days=cfg["min_trip_days"]),
        dep_to + timedelta(days=cfg["max_trip_days"]),
    )

    best: dict[tuple[str, str], dict] = {}
    for dm in dep_months:
        for rm in ret_months:
            if rm < dm:
                continue
            params = {
                "origin": cfg["origin"],
                "destination": cfg["destination"],
                "departure_at": dm,
                "return_at": rm,
                "currency": cfg["currency"].lower(),
                "sorting": "price",
                "direct": str(cfg.get("direct_only", False)).lower(),
                "unique": "false",
                "limit": 1000,
                "one_way": "false",
            }
            body = http_get_json(TRAVELPAYOUTS_URL, params, {"X-Access-Token": token})
            if not body.get("success", False):
                raise RuntimeError(f"Travelpayouts 回傳錯誤：{body.get('error') or body}")
            for row in body.get("data", []):
                if not row.get("return_at"):
                    continue
                out_t = local_time(row["departure_at"], TAIPEI)
                back_t = local_time(row["return_at"], TOKYO)
                depart, ret = out_t.date().isoformat(), back_t.date().isoformat()
                if not in_window(cfg, depart, ret):
                    continue
                if not (time_ok(out_t, cfg["outbound_time"]) and time_ok(back_t, cfg["return_time"])):
                    continue
                airline = row.get("airline", "")
                bags_included = airline not in LCC_AIRLINES
                bag_fee = 0 if bags_included else cfg["lcc_bag_fee_roundtrip"]
                offer = {
                    "depart": depart,
                    "return": ret,
                    "dep_time": out_t.strftime("%H:%M"),
                    "ret_time": back_t.strftime("%H:%M"),
                    "fare": int(row["price"]),
                    "bag_fee": bag_fee,
                    "bags_included": bags_included,
                    "price": int(row["price"]) + bag_fee,
                    "airline": row.get("airline", ""),
                    "flight": f'{row.get("airline", "")}{row.get("flight_number", "")}',
                    "from": row.get("origin_airport", cfg["origin"]),
                    "to": row.get("destination_airport", ""),
                    "stops": max(row.get("transfers", 0), row.get("return_transfers", 0)),
                    "link": "https://www.aviasales.com" + row["link"] if row.get("link") else "",
                }
                key = (depart, ret)
                if key not in best or offer["price"] < best[key]["price"]:
                    best[key] = offer
    return sorted(best.values(), key=lambda o: (o["depart"], o["return"]))


def fetch_serpapi_insights(cfg: dict, key: str, offers: list[dict]) -> list[dict]:
    """價格優先：拿目前最便宜的前 N 組日期去 Google 航班複查，取得即時價與價格水準（low / typical / high）。"""
    out = []
    top = sorted(offers, key=lambda o: o["price"])[: cfg.get("serpapi_top_n", 3)]
    for pair in top:
        params = {
            "engine": "google_flights",
            "departure_id": cfg["origin"],
            "arrival_id": TOKYO_AIRPORTS,
            "outbound_date": pair["depart"],
            "return_date": pair["return"],
            "type": 1,
            "currency": cfg["currency"],
            "hl": "zh-TW",
            "gl": "tw",
            "api_key": key,
        }
        try:
            body = http_get_json(SERPAPI_URL, params)
        except urllib.error.HTTPError as e:
            print(f"  SerpApi {pair} 失敗：{e}", file=sys.stderr)
            continue
        insight = body.get("price_insights", {})
        flights = body.get("best_flights", []) + body.get("other_flights", [])
        cheapest = min(flights, key=lambda f: f.get("price", 10**9), default=None)
        out.append({
            "depart": pair["depart"],
            "return": pair["return"],
            "lowest": insight.get("lowest_price") or (cheapest or {}).get("price"),
            "level": insight.get("price_level"),
            "typical": insight.get("typical_price_range"),
            "airline": ((cheapest or {}).get("flights") or [{}])[0].get("airline", ""),
            "link": body.get("search_metadata", {}).get("google_flights_url", ""),
        })
    return out


def load_history() -> dict:
    if HISTORY_PATH.exists():
        return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    return {"snapshots": []}


def all_time_low(history: dict) -> int | None:
    prices = [o["price"] for s in history["snapshots"] for o in s.get("offers", [])]
    return min(prices) if prices else None


def format_offer(o: dict) -> str:
    return (
        f'{o["depart"]} {o.get("dep_time", "")} → {o["return"]} {o.get("ret_time", "")}（{(date.fromisoformat(o["return"]) - date.fromisoformat(o["depart"])).days} 天）'
        f' 含行李 NT${o["price"]:,}{"" if o.get("bags_included", True) else "（廉航，含行李估算）"} {o["flight"]} {o["from"]}-{o["to"]}'
        f' {"直飛" if o["stops"] == 0 else "轉機 %d 次" % o["stops"]}'
    )


def notify(text: str) -> None:
    tg_token, tg_chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    discord = os.getenv("DISCORD_WEBHOOK_URL")
    if tg_token and tg_chat:
        http_post_json(
            f"https://api.telegram.org/bot{tg_token}/sendMessage",
            {"chat_id": tg_chat, "text": text, "disable_web_page_preview": True},
        )
    if discord:
        http_post_json(discord, {"content": text[:1900]})
    if not (tg_token and tg_chat) and not discord:
        print("（未設定通知管道，只輸出到終端機）")


def write_step_summary(lines: list[str]) -> None:
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="只抓價格並印出，不寫檔、不通知")
    args = parser.parse_args()

    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    token = os.getenv("TRAVELPAYOUTS_TOKEN")
    if not token:
        print("缺少 TRAVELPAYOUTS_TOKEN，請參考 flight-monitor/README.md 設定。", file=sys.stderr)
        return 1

    now = datetime.now(TAIPEI)
    print(f"[{now:%Y-%m-%d %H:%M}] 查詢 {cfg['origin']} ⇄ {cfg['destination']}，"
          f"出發 {cfg['depart_from']} ~ {cfg['depart_to']}，停留 {cfg['min_trip_days']}-{cfg['max_trip_days']} 天")

    offers = fetch_travelpayouts(cfg, token)
    insights = fetch_serpapi_insights(cfg, os.environ["SERPAPI_KEY"], offers) if os.getenv("SERPAPI_KEY") else []

    cheapest = sorted(offers, key=lambda o: o["price"])[:5]
    print(f"取得 {len(offers)} 組日期報價")
    for o in cheapest:
        print("  " + format_offer(o))
    for i in insights:
        print(f'  Google 航班 {i["depart"]}→{i["return"]}：NT${i["lowest"]} 價格水準={i["level"]} 一般區間={i["typical"]}')

    if args.dry_run:
        return 0

    history = load_history()
    prev_low = all_time_low(history)
    history["snapshots"].append({
        "checked_at": now.isoformat(timespec="minutes"),
        "offers": offers,
        "insights": insights,
    })
    history["config"] = {k: v for k, v in cfg.items() if not k.startswith("_")}
    history["updated_at"] = now.isoformat(timespec="minutes")
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(json.dumps(history, ensure_ascii=False, indent=1), encoding="utf-8")

    summary = [f"## 東京春節機票觀測 {now:%Y-%m-%d %H:%M}", f"共 {len(offers)} 組日期有報價", ""]
    summary += [f"- {format_offer(o)}" for o in cheapest]
    write_step_summary(summary)

    if not cheapest:
        return 0
    best = cheapest[0]
    reasons = []
    if prev_low is None or best["price"] < prev_low:
        reasons.append("觀測以來新低價" + (f"（先前最低 NT${prev_low:,}）" if prev_low else ""))
    if best["price"] <= cfg.get("target_price", 0):
        reasons.append(f'低於你的目標價 NT${cfg["target_price"]:,}')
    low_levels = [i for i in insights if i.get("level") == "low"]
    if low_levels:
        reasons.append("Google 航班判定為「偏低」價格：" + "、".join(f'{i["depart"]}出發' for i in low_levels))

    if reasons:
        msg = "✈️ 東京春節機票降價提醒\n" + "\n".join(f"• {r}" for r in reasons) + "\n\n目前最便宜：\n"
        msg += "\n".join(format_offer(o) for o in cheapest[:3])
        if best["link"]:
            msg += f"\n\n訂票連結：{best['link']}"
        print(msg)
        notify(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
