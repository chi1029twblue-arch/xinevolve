#!/usr/bin/env python3
"""台北 → 東京 春節機票觀測器。

每次執行：
  1. 從 Travelpayouts（Aviasales Data API，免費）抓取出發窗口內所有來回組合的最低價
  2. （選用）Google 航班（SerpApi）：在每月免費額度內輪流查窗口內的日期，取得即時價與「價格水準」
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


def party_size(cfg: dict) -> int:
    t = cfg["travelers"]
    return t["adults"] + t["children"]


def family_total(cfg: dict, adult_fare: int, bags_included: bool) -> tuple[int, int]:
    """由一張成人票價換算全家總價。回傳 (總價含行李, 行李費小計)。

    傳統航空：兒童票約為成人票的 child_fare_ratio，行李已含。
    廉價航空：兒童與成人同價，每人另加託運行李費。
    """
    t = cfg["travelers"]
    ratio = 1.0 if not bags_included else cfg["child_fare_ratio"]
    fares = adult_fare * t["adults"] + round(adult_fare * ratio) * t["children"]
    bags = 0 if bags_included else cfg["lcc_bag_fee_roundtrip"] * party_size(cfg)
    return fares + bags, bags


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
                stops = max(row.get("transfers", 0), row.get("return_transfers", 0))
                if cfg.get("direct_only") and stops:
                    continue
                airline = row.get("airline", "")
                bags_included = airline not in LCC_AIRLINES
                total, bag_fee = family_total(cfg, int(row["price"]), bags_included)
                offer = {
                    "source": "aviasales",
                    "depart": depart,
                    "return": ret,
                    "dep_time": out_t.strftime("%H:%M"),
                    "ret_time": back_t.strftime("%H:%M"),
                    "fare": int(row["price"]),
                    "fare_basis": "adult",
                    "bag_fee": bag_fee,
                    "bags_included": bags_included,
                    "price": total,
                    "per_person": round(total / party_size(cfg)),
                    "airline": row.get("airline", ""),
                    "flight": f'{row.get("airline", "")}{row.get("flight_number", "")}',
                    "from": row.get("origin_airport", cfg["origin"]),
                    "to": row.get("destination_airport", ""),
                    "stops": stops,
                    "link": "https://www.aviasales.com" + row["link"] if row.get("link") else "",
                }
                key = (depart, ret)
                if key not in best or offer["price"] < best[key]["price"]:
                    best[key] = offer
    return sorted(best.values(), key=lambda o: (o["depart"], o["return"]))


def google_candidates(cfg: dict, offers: list[dict], history: dict, today: date) -> list[tuple[str, str]]:
    """決定這次要問 Google 航班哪幾組日期（受每月免費額度限制）。

    1. 目前 Aviasales 最便宜的那組（若近期沒查過），用來交叉確認好價
    2. 其餘在窗口內輪流：停留 trip_days 天的每個出發日，最久沒查的優先
    """
    g = cfg["google"]
    checked = history.get("google_checked", {})
    keep = timedelta(days=g["keep_days"])

    def fresh(key: str) -> bool:
        return key in checked and today - date.fromisoformat(checked[key]) < keep

    picks: list[tuple[str, str]] = []
    for o in sorted(offers, key=lambda o: o["price"])[:1]:
        if not fresh(f'{o["depart"]}|{o["return"]}'):
            picks.append((o["depart"], o["return"]))

    pool = []
    d, end = date.fromisoformat(cfg["depart_from"]), date.fromisoformat(cfg["depart_to"])
    while d <= end:
        for t in g["trip_days"]:
            key = f"{d}|{d + timedelta(days=t)}"
            pool.append((checked.get(key, "0000-00-00"), d.isoformat(), (d + timedelta(days=t)).isoformat()))
        d += timedelta(days=1)
    for _, dep, ret in sorted(pool):
        if len(picks) >= g["queries_per_run"]:
            break
        if (dep, ret) not in picks:
            picks.append((dep, ret))
    return picks[: g["queries_per_run"]]


def fetch_google(cfg: dict, key: str, pairs: list[tuple[str, str]], today: date) -> tuple[list[dict], list[dict]]:
    """用 SerpApi 查 Google 航班。回傳 (符合條件的最便宜報價, 價格水準資訊)。

    以設定的大人＋兒童人數查詢，Google 會套用各航空的兒童票規則，回傳的是全家來回總價。
    來回搜尋第一頁只列去程航班（回程由 Google 依 return_times 篩選），所以回程起飛時間記為空字串。
    """
    hours = lambda w: f"{int(w[0][:2])},{int(w[1][:2])}"
    offers, insights = [], []
    for dep, ret in pairs:
        params = {
            "engine": "google_flights",
            "departure_id": cfg["origin"],
            "arrival_id": TOKYO_AIRPORTS,
            "outbound_date": dep,
            "return_date": ret,
            "type": 1,
            "adults": cfg["travelers"]["adults"],
            "children": cfg["travelers"]["children"],
            "currency": cfg["currency"],
            "hl": "zh-TW",
            "gl": "tw",
            "sort_by": 2,
            "stops": 1 if cfg.get("direct_only") else 0,
            "outbound_times": hours(cfg["outbound_time"]),
            "return_times": hours(cfg["return_time"]),
            "api_key": key,
        }
        try:
            body = http_get_json(SERPAPI_URL, params)
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            print(f"  SerpApi {dep}→{ret} 失敗：{e}", file=sys.stderr)
            continue
        link = body.get("search_metadata", {}).get("google_flights_url", "")
        best = None
        for it in body.get("best_flights", []) + body.get("other_flights", []):
            legs = it.get("flights") or []
            if not legs or "price" not in it:
                continue
            stops = len(legs) - 1
            if cfg.get("direct_only") and stops:
                continue
            dep_time = legs[0].get("departure_airport", {}).get("time", "")[-5:]
            if not dep_time or not (cfg["outbound_time"][0] <= dep_time <= cfg["outbound_time"][1]):
                continue
            number = legs[0].get("flight_number", "")
            code = number.split()[0] if number else ""
            bags_included = code not in LCC_AIRLINES
            bag_fee = 0 if bags_included else cfg["lcc_bag_fee_roundtrip"] * party_size(cfg)
            total = int(it["price"]) + bag_fee
            offer = {
                "source": "google",
                "depart": dep,
                "return": ret,
                "dep_time": dep_time,
                "ret_time": "",
                "fare": int(it["price"]),
                "fare_basis": "party",
                "bag_fee": bag_fee,
                "bags_included": bags_included,
                "price": total,
                "per_person": round(total / party_size(cfg)),
                "airline": code,
                "flight": number.replace(" ", ""),
                "airline_name": legs[0].get("airline", ""),
                "from": legs[0].get("departure_airport", {}).get("id", cfg["origin"]),
                "to": legs[-1].get("arrival_airport", {}).get("id", ""),
                "stops": stops,
                "link": link,
                "seen_at": today.isoformat(),
            }
            if best is None or offer["price"] < best["price"]:
                best = offer
        if best:
            offers.append(best)
        pi = body.get("price_insights") or {}
        insights.append({
            "depart": dep,
            "return": ret,
            "lowest": best["price"] if best else pi.get("lowest_price"),
            "level": pi.get("price_level"),
            "typical": pi.get("typical_price_range"),
            "airline": (best or {}).get("airline_name", ""),
            "matched": best is not None,
            "link": link,
            "seen_at": today.isoformat(),
        })
    return offers, insights


def carry_over(prev: list[dict], fresh_keys: set[str], today: date, keep_days: int) -> list[dict]:
    """保留前一次快照裡、近 keep_days 天查過且這次沒重查的 Google 結果，讓價格地圖不會每天只剩幾格。"""
    out = []
    for x in prev:
        if x.get("source", "google") != "google" and "level" not in x:
            continue
        key = f'{x["depart"]}|{x["return"]}'
        seen = x.get("seen_at")
        if key in fresh_keys or not seen:
            continue
        if (today - date.fromisoformat(seen)).days < keep_days:
            out.append(x)
    return out


def merge_offers(*groups: list[dict]) -> list[dict]:
    best: dict[tuple[str, str], dict] = {}
    for o in [o for g in groups for o in g]:
        key = (o["depart"], o["return"])
        if key not in best or o["price"] < best[key]["price"]:
            best[key] = o
    return sorted(best.values(), key=lambda o: (o["depart"], o["return"]))


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
        f' 全家含行李 NT${o["price"]:,}（平均每人 NT${o["per_person"]:,}）{"" if o.get("bags_included", True) else "［廉航，行李為估算］"} {o["flight"]} {o["from"]}-{o["to"]}'
        f' {"直飛" if o["stops"] == 0 else "轉機 %d 次" % o["stops"]}'
        f'{"［Google 航班］" if o.get("source") == "google" else ""}'
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
    t = cfg["travelers"]
    print(f"[{now:%Y-%m-%d %H:%M}] 查詢 {cfg['origin']} ⇄ {cfg['destination']}，"
          f"出發 {cfg['depart_from']} ~ {cfg['depart_to']}，停留 {cfg['min_trip_days']}-{cfg['max_trip_days']} 天，"
          f"{t['adults']} 大人 {t['children']} 兒童")

    history = load_history()
    today = now.date()
    prev = history["snapshots"][-1] if history["snapshots"] else {}

    tp_offers = fetch_travelpayouts(cfg, token)
    g_offers, g_insights, pairs = [], [], []
    if os.getenv("SERPAPI_KEY"):
        pairs = google_candidates(cfg, tp_offers, history, today)
        g_offers, g_insights = fetch_google(cfg, os.environ["SERPAPI_KEY"], pairs, today)
    fresh = {f"{d}|{r}" for d, r in pairs}
    keep = cfg["google"]["keep_days"]
    offers = merge_offers(tp_offers, g_offers, carry_over(prev.get("offers", []), fresh, today, keep))
    insights = sorted(g_insights + carry_over(prev.get("insights", []), fresh, today, keep),
                      key=lambda i: (i.get("lowest") is None, i.get("lowest") or 0))

    cheapest = sorted(offers, key=lambda o: o["price"])[:5]
    print(f"Aviasales {len(tp_offers)} 組、Google 新查 {len(pairs)} 組（{len(g_offers)} 組有符合條件的航班），合計 {len(offers)} 組日期")
    for o in cheapest:
        print("  " + format_offer(o))
    for i in g_insights:
        print(f'  Google 航班 {i["depart"]}→{i["return"]}：NT${i["lowest"]} 價格水準={i["level"]} 一般區間={i["typical"]}')

    if args.dry_run:
        return 0

    prev_low = all_time_low(history)
    history.setdefault("google_checked", {}).update({k: today.isoformat() for k in fresh})
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
        reasons.append(f'全家總價低於你的目標 NT${cfg["target_price"]:,}')
    low_levels = [i for i in g_insights if i.get("level") == "low" and i.get("matched")]
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
