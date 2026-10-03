# ✈️ 台北 ⇄ 東京 春節前後機票觀測工具

**以價格為優先**：在 2027 春節前後約 6 週的寬鬆窗口內，每天自動抓所有「出發日 × 停留天數」組合的來回最低價，
先告訴你哪個價錢最便宜，再讓你挑配合得上的日期。

- 2027 春節連假：**2/4（四）小年夜 ～ 2/10（三）**，共 7 天（人事行政總處 116 年行事曆）
- 觀測窗口預設：出發 **1/16 ～ 2/28**，停留 **3 ～ 10 天**，TPE → 東京（成田 NRT ＋ 羽田 HND）

## 運作方式

```
GitHub Actions（每天 08:17 / 20:17 台灣時間）
   └─ monitor.py
        ├─ Travelpayouts API：抓窗口內每組日期最低價（免費）
        ├─ SerpApi Google 航班：複查「目前最便宜的 3 組日期」的即時價與價格水準（選用）
        ├─ 寫入 data/history.json（累積歷史 → 才看得出趨勢）
        └─ 新低價 / 低於目標價 → Telegram 或 Discord 通知（選用）
index.html：讀 history.json 畫出
   ① 最便宜 10 組日期（價格排序）
   ② 出發日 × 天數 價格地圖（一眼看出哪幾天便宜）
   ③ 最低價走勢（全窗口 vs. 只限連假出發）
   ④ Google 航班價格水準（偏低／一般／偏高）
   ＋ 「該不該買」建議訊號
```

## 設定步驟（約 10 分鐘）

1. **取得 Travelpayouts token（必要，免費）**
   到 <https://www.travelpayouts.com> 註冊 → 個人設定（Profile）→ API token。
2. **（選用）SerpApi key**：<https://serpapi.com> 免費方案的額度足夠每天查 3 組日期。
   有這個才會有「Google 判定偏低／偏高」的價格水準，買點判斷更準。
3. **（選用）通知管道**，二擇一或都設：
   - Telegram：找 `@BotFather` 建 bot 取得 token；傳一則訊息給 bot 後，
     開 `https://api.telegram.org/bot<TOKEN>/getUpdates` 找到 `chat.id`
   - Discord：頻道設定 → 整合 → Webhook → 複製網址
4. 到 GitHub repo → **Settings → Secrets and variables → Actions** 新增：

   | Secret | 必填 |
   |---|---|
   | `TRAVELPAYOUTS_TOKEN` | ✅ |
   | `SERPAPI_KEY` | |
   | `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID` | |
   | `DISCORD_WEBHOOK_URL` | |

5. 把這個分支合併到 **預設分支**（GitHub 的排程只在預設分支上執行），
   然後到 **Actions → 東京春節機票觀測 → Run workflow** 手動跑第一次。
6. 打開儀表板：網站有部署的話是 `/flight-monitor/`；或在本機
   `python3 -m http.server` 後開 <http://localhost:8000/flight-monitor/>。
   還沒資料時可加 `?demo=1` 預覽版面（示範資料，非真實票價）。

## 調整條件 — `config.json`

| 欄位 | 說明 |
|---|---|
| `depart_from` / `depart_to` | 出發日期範圍 |
| `min_trip_days` / `max_trip_days` | 停留天數範圍 |
| `direct_only` | `true` 只看直飛 |
| `target_price` | 來回低於這個價錢就通知（台幣） |
| `outbound_time` / `return_time` | 去程 / 回程的起飛時段（當地時間），預設避開太早、太晚的航班 |
| `lcc_bag_fee_roundtrip` | 廉價航空每人來回的託運行李估算費用；所有價格都以「含行李」比較 |
| `serpapi_top_n` | 每次用 Google 航班複查幾組最便宜日期 |
| `origin` | 改 `KHH` 可看高雄出發 |

本機測試：`TRAVELPAYOUTS_TOKEN=xxx python3 flight-monitor/monitor.py --dry-run`

## 怎麼解讀、什麼時候買

儀表板的建議訊號是簡單的經驗法則：

- ✅ **好價，建議下手**：低於目標價
- ↓ **可以考慮**：在觀測最低價 3% 以內，或 Google 判定「偏低」
- ! **別等太久**：比 7 天前貴 5% 以上，或離出發剩不到 45 天
- … **持續觀察**：其他情況

一般春節機票的經驗（僅供參考，實際以觀測資料為準）：

- 春節是台灣出國旺季，**越接近出發通常越貴**，大多在出發前 2～4 個月（也就是現在到 12 月）價格較好。
- 台灣出發的尖峰是**連假前一兩天出發、收假前一兩天回台**；避開這兩端（例如除夕／初一當天出發，
  或連假結束後才回來）常常明顯便宜。價格地圖可以直接看出差多少。
- **連假前或連假後幾週**（1 月下旬、2 月中下旬）往往是整個窗口最便宜的時段；搭配請假，可能比卡連假划算。
- 廉航（台灣虎航、樂桃、捷星等）不定期促銷，表上的價格**不一定含行李與選位**，下單前請確認。

## 限制

- Travelpayouts 的價格是 Aviasales 使用者近 48 小時搜尋過的快取，冷門日期可能暫時沒資料，也可能與航空公司官網略有差距。
  看到好價請以訂位頁面實際價格為準。
- `data/history.json` 每次執行由 Actions 自動 commit，約每天兩筆，春節前檔案大小約數 MB。
