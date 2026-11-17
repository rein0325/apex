# 台股智慧量化投資決策系統

個人使用的台股量化研究與投資決策輔助系統。系統提供分析與建議，**不自動下單，不保證獲利**。

## 文件

| 文件 | 內容 |
|---|---|
| [產品企劃書](docs/台股智慧量化投資系統_產品企劃書.md) | 產品目標、功能、路線圖 |
| [系統規格書](docs/台股智慧量化投資系統_系統規格書.md) | 每日流程、驗證標準、開發原則 |
| [資料來源評估](docs/資料來源評估.md) | 資料來源比較與實測結果 |
| [技術選型](docs/技術選型.md) | 技術決策與個人資料原則 |
| [資料層設計](docs/資料層設計.md) | Phase 1 資料流程、資料表、品質檢查 |

## 目錄

```
docs/      規格與決策文件
src/apex/  主程式
tests/     測試
probes/    資料來源探測腳本（研究用）
data/      本機資料（不進版本控制）
personal/  持股與交易紀錄（只留本機，不進版本控制）
```

## 開發環境

需要 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync
uv run python -m pytest
uv run python -m ruff check .
```

在這台 Windows 上，`pytest.exe` 會被系統的應用程式控制原則擋下，所以統一用 `python -m` 執行。若 `uv` 不在 PATH，可改用 `python -m uv`。

資料擷取（詳見資料層設計 §6）：

```bash
uv run python -m apex update     # 補齊近一個月的交易日
uv run python -m apex status     # 資料狀態
```

資料來源探測：

```bash
python probes/data_source_probe.py
```

## 目前進度

- [x] 資料來源調查與實測
- [x] 技術選型（第 1～4 階段）
- [ ] Phase 1：資料與回測基礎
  - [x] 交易日曆、上市櫃日行情、證券主檔、資料品質檢查
  - [ ] 10 年歷史回補
  - [ ] 除權息與還原股價、下市櫃清單、產業分類
  - [ ] 三大法人、融資融券、月營收、財報
  - [ ] 交易成本與回測框架
