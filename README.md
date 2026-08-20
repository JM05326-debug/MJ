# CPBL / NPB 賽事預測 — 雲端 MLOps 管線

中華職棒（CPBL）與日本職棒（NPB）賽事勝率預測系統。資料抓取、預測鎖定、結果回收、模型訓練、Dashboard 全部在 GitHub Actions 雲端排程執行，不依賴本機電腦。CPBL 官網 `www.cpbl.com.tw` 會封鎖 GitHub Actions 公開的 IP 範圍（實測每個端點都回傳 404，換一般 IP 就正常），所以 CPBL 資料改抓第三方球隊數據網站「野球革命」(rebas.tw) 的公開 JSON API——不同網域，不在那份封鎖名單上。本機 Windows 工作排程器仍保留一份備援（見下方「CPBL 本機備援排程」），預設停用，只在 rebas.tw 也連不上時手動切回。

**Repo**：https://github.com/JM05326-debug/MJ
**Dashboard（手機可看）**：https://jm05326-debug.github.io/MJ/

## 系統如何運作

```
              雲端 GitHub Actions（每天 4 次 09:15/16:15/18:15/21:15 台北）
        │                                 │
        ▼                                 ▼
     CPBL 資料                         NPB 資料
(賽程/球員數據/玩運彩賠率           (賽程/球員數據)
 抓自野球革命 rebas.tw——
 官網 cpbl.com.tw 擋 GitHub
 Actions 的 IP，rebas.tw 不擋)
        │                                 │
        └────────────────┬────────────────┘
                          ▼
                 Feature Engineering
           （Elo + Poisson + 先發/牛棚/左右對戰因子，
             pipeline/feature_spec.py）
                          ▼
                ML Model Prediction
        （目前 production：stacked LogisticRegression；
          models/registry.json 記錄版本）
                          ▼
              今日預測結果（追加寫入
        predictions/{league}_predictions_log.jsonl）
                          ▼
                   比賽開始前 🔒 鎖定
        （45 分鐘安全緩衝內尚未鎖定則跳過，絕不事後補）
                          ▼
                      比賽開始
                          ▼
        雲端 GitHub Actions（每天 2 次 台北 00:00/06:00）
                自動取得真實結果
              pipeline/collect_results.py
                          ▼
              Prediction vs Result 比對
        （只 append 結果，絕不竄改原本鎖定的預測）
                          ▼
                  累積 Training Data
                          ▼
        雲端 GitHub Actions（每週一 台北凌晨 02:00）
              pipeline/build_dataset.py
              pipeline/train_model.py
                          ▼
              Challenger vs Production
        （用同一份驗證集比較 Log Loss，challenger 需
          低至少 0.005、驗證集≥30場才有資格比較——
          pipeline/validate_promote.py）
                          ▼
                新模型勝出 → 上線成新 production
        （否則維持現有 production，不受資料量不足
          或訓練失敗影響）
```

## CPBL 本機備援排程（預設停用）

CPBL 資料現在由雲端抓（見上），本機 Windows 工作排程器的四個任務（`CPBL_Update_1/2/3/4`，各對應 09:00/16:00/18:00/21:00，執行 `scripts/update_cpbl_and_push.bat`）保留下來但**停用**，只當 rebas.tw 有一天也連不上 GitHub Actions 時的備援手段。啟用後這部分一樣需要電腦在排程時間點開機且已登入，跳過的那一輪不會補跑。

管理指令（PowerShell）：
```powershell
Get-ScheduledTask -TaskName "CPBL_Update_*"          # 查看狀態（Ready=停用中可手動觸發, Disabled=已停用）
Get-ScheduledTaskInfo -TaskName "CPBL_Update_1"       # 查看下次執行時間、上次結果
Enable-ScheduledTask -TaskName "CPBL_Update_*"        # 需要切回本機備援時重新啟用全部四個
Start-ScheduledTask -TaskName "CPBL_Update_1"         # 手動立即觸發一次（不需先啟用排程本身）
Disable-ScheduledTask -TaskName "CPBL_Update_*"       # 重新停用
```

git push 認證用的是 GitHub CLI（`gh auth login` 時設定），存在 Windows 認證管理員裡，工作排程器執行時會自動使用，不需要每次重新登入。

## 防止 Data Leakage 的具體機制

- 預測一旦寫入 `predictions/{league}_predictions_log.jsonl` 就不會再被修改（只有 append，程式碼裡沒有任何覆寫既有行的路徑）
- 鎖定前會檢查「現在時間是否已經太接近或超過賽程開始時間（45分鐘安全緩衝）」，是的話這場比賽這次跳過，**絕不事後補預測**
- 每筆鎖定的預測都完整保存了當下用來計算的 `feature_vector`（不是之後從當前資料重新推導——`data/*.json` 每次爬蟲都會被覆蓋，賽前當下的快照只有這裡才留得住）
- 歷史回填（`backfill_historical.py`）對每個歷史比賽日期，只用「日期更早」的比賽重算 Elo/Poisson，且先發投手/牛棚/對戰左右投等資訊一律視為未知（因為這些資料源本來就無法回溯到任意過去時間點）

## 模型版本與升級規則

所有版本都記錄在 `models/registry.json`，包含被拒絕的 challenger（方便之後比較）。現有的 Elo+Poisson 規則式模型註冊為 `v0000_elo_poisson_baseline`，是每個新模型都要打敗的基準。

升級規則：challenger 的 Log Loss 要比目前 production 低至少 0.005（有安全 margin，避免雜訊觸發），且驗證集至少要有 30 場比賽才有資格比較。資料量不足或訓練失敗都不會影響現有 production 模型。

## 手動執行 / 除錯

```bash
pip install -r requirements.txt

# 補齊資料
python scripts/fetch_cpbl.py && python scripts/fetch_cpbl_players.py && python scripts/fetch_cpbl_odds.py
python scripts/fetch_npb.py && python scripts/fetch_npb_players.py

# 每日流程
python pipeline/lock_predictions.py
python pipeline/collect_results.py
python pipeline/build_dashboard.py

# 訓練流程（一週一次的邏輯，隨時可手動觸發測試）
python pipeline/build_dataset.py
python pipeline/train_model.py
python pipeline/validate_promote.py

# 一次性：歷史回填（只需要在資料庫改變時重新跑）
python pipeline/backfill_historical.py
```

也可以在 GitHub 上 Actions 分頁手動觸發任一 workflow（`workflow_dispatch`）。

## 已知限制

- **CPBL 資料依賴 rebas.tw 這個第三方網站**：不是 CPBL 官方 API，若它改版、關站，或哪天也開始擋 GitHub Actions 的 IP，CPBL 資料就會斷——本機備援排程（見上）就是為了這種情況保留的
- NPB 目前沒有賠率來源（`fetch_cpbl_odds.py` 只有 CPBL 玩運彩版本），ROI 指標只能算 CPBL 有賠率的場次
- NPB 的牛棚/先發數據只能抓到近 25 天的滾動視窗（官網沒有球員逐場成績 API），CPBL 則有完整球季資料
- 先發投手常常賽前幾小時才公布，鎖定時若還沒公布會顯示「先發未公布」，模型退化成純 Elo+Poisson（不會因此不預測）
- 歷史回填資料沒有先發投手/牛棚/對戰左右投特徵（技術上無法回溯），只有正向蒐集的資料才有完整特徵

## 舊版：本機手動網頁（仍保留）

`update_data.bat`（雙擊執行）+ `scripts/generate_site.py` 產生的 `site/index.html` 是原本的本機手動更新版本，功能更完整（先發投手詳細數據卡片、EV 計算機、任意對戰試算），適合想要更豐富介面時使用；雲端 Dashboard 則是精簡的監控/追蹤介面。兩者互不影響。
