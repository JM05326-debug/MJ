# CPBL / NPB 賽事預測 — 雲端 MLOps 管線

中華職棒（CPBL）與日本職棒（NPB）賽事勝率預測系統。全部在 GitHub Actions 雲端排程執行，本機電腦不需要開機。

**Dashboard（手機可看）**：`https://<your-github-username>.github.io/<repo-name>/`（需先在 repo Settings → Pages 設定來源為 `main` branch 的 `/docs` 資料夾）

## 系統如何運作

```
每天 3 次（台北 09:30 / 14:30 / 16:45）
  scripts/fetch_*.py          抓 CPBL/NPB 賽程、球員數據、玩運彩賠率
  pipeline/lock_predictions.py 對 36 小時內即將開打、尚未鎖定的比賽產生預測並「鎖定」
                                （鎖定後永不覆寫——這是防止 data leakage 的核心機制）
  scripts/generate_site.py     產生完整版預測網頁 site/index.html
  pipeline/build_dashboard.py  產生手機 Dashboard docs/index.html

每天 2 次（台北 00:00 / 06:00）
  pipeline/collect_results.py  比對已鎖定的預測，抓到比賽結果就記錄下來（絕不竄改原本的預測）

每週一（台北凌晨 02:00）
  pipeline/build_dataset.py    合併「歷史回填資料」+「賽前預測⋈賽後結果」成訓練集
  pipeline/train_model.py      用訓練集訓練一個新的 challenger 模型（stacked LogisticRegression）
  pipeline/validate_promote.py 用同一份驗證集比較 challenger 與目前 production 模型的 Log Loss，
                                只有 challenger 明顯更好才會升級成新的 production 模型
```

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

- NPB 目前沒有賠率來源（`fetch_cpbl_odds.py` 只有 CPBL 玩運彩版本），ROI 指標只能算 CPBL 有賠率的場次
- NPB 的牛棚/先發數據只能抓到近 25 天的滾動視窗（官網沒有球員逐場成績 API），CPBL 則有完整球季資料
- 先發投手常常賽前幾小時才公布，鎖定時若還沒公布會顯示「先發未公布」，模型退化成純 Elo+Poisson（不會因此不預測）
- 歷史回填資料沒有先發投手/牛棚/對戰左右投特徵（技術上無法回溯），只有正向蒐集的資料才有完整特徵

## 舊版：本機手動網頁（仍保留）

`update_data.bat`（雙擊執行）+ `scripts/generate_site.py` 產生的 `site/index.html` 是原本的本機手動更新版本，功能更完整（先發投手詳細數據卡片、EV 計算機、任意對戰試算），適合想要更豐富介面時使用；雲端 Dashboard 則是精簡的監控/追蹤介面。兩者互不影響。
