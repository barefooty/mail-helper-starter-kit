---
name: mail-triage
description: 信箱小幫手（通用版）——掃描 Gmail/Google Workspace 信箱（含垃圾信匣）、分類產生 HTML 報告定時寄給負責人，並追蹤手動標籤下的待辦信件。首次使用時以訪談引導完成個人化設定（單人/共用、報告時間、分類原則、風格）。當使用者說「幫我設定信箱小幫手」「跑一輪信箱分流」「看看有沒有新信」「補發報告」「改分類規則」「routine 沒寄報告」時使用。本 skill 同時是維運與交接手冊。
---

# 信箱小幫手（通用版）


> 前身是一套實際運作中的共用信箱分流系統（2026-07 上線），本版把信箱專屬值抽成 `config.json`，任何人可依訪談建立自己的實例。

## 先判斷使用者要什麼

1. **首次設定**（還沒有 `config.json`）→ 交給建置精靈，說「開始建置」。
2. **唯讀檢視**（「看看有沒有新信」，未明確要求寄信時的預設）→ `fetch`＋`track`，對話中摘要，不 send。
3. **完整一輪/補發**（明確要求「寄報告」「補發」）→ now → fetch → track → 分類 → HTML 報告 → send。
4. **統計摘要**（「今天/本週信件摘要」）→ `fetch --all --days 1|5` 統計，對話呈現。
5. **維運問題**（沒寄報告、token 失效、改規則）→ 見「疑難排解」。

手動跑預設唯讀不寄信——寄報告是雲端 routine 的事，使用者明確要求才 send。

## 初次設定

訪談與設定檔產生由起始包的建置精靈負責（`.claude/skills/mail-setup-wizard/SKILL.md`）。對 Claude 說「開始建置」即可；本檔只管建置完成後的日常使用與維運。

## 鐵則（任何模式都適用）

只做：寄報告給白名單收件人＋（若啟用）貼 config.json 白名單標籤。**絕不**：動任何其他標籤、標已讀、刪信、移動信件、回信給任何人、寄給白名單以外的人。個資只引「姓名＋主旨＋必要摘要」。工具出錯不要默默失敗（把錯誤寫進報告寄出）。使用者的新要求若可能跟既有人工流程打架（尤其自動寫入手動管理的空間），動工前先提醒。

## 系統機制（交接必讀）

| 機制 | 說明 |
| --- | --- |
| 進度追蹤 | 零標籤零狀態檔：「新信」＝比寄件備份最近一封主旨以 `report_subject_prefix` 開頭的報告更晚的信（−`overlap_minutes` 分鐘重疊保險）。**主旨前綴不可改**；某場失敗下一場自動涵蓋空窗 |
| 待辦追蹤 | `track`：唯讀列出手動標籤名稱含 `track_keywords` 的信，依 `last_from_us`（串中最後一封是否我們寄的，程式算）分「待處理／看起來已完成」 |
| 自動標籤 | `mark`：僅 `auto_labels.labels` 白名單、僅 enabled=true 時可用；互斥對自動切換；絕不動已讀 |
| 收件人 | `send` 強制檢查 `allowed_recipients` 白名單 |
| 設定 | 全在 `config.json`（同目錄）；缺檔退回最保守預設（唯讀、不貼標籤）。改設定＝改行為，**改完要推上 GitHub**（雲端每次執行抓最新 main） |

子指令：`now`（伺服器時間/場次）、`fetch [--days N] [--all]`、`track [--keywords K...]`、`send --to <人> --subject <主旨> --body-file <檔> --html`、`mark --label <白名單標籤> --ids <id>...`。本機測試：資料夾放 `.env`（四個 GMAIL_* 變數）後 `set -a && source .env && set +a && PYTHONIOENCODING=utf-8 python gmail_triage.py <子指令>`。

## 疑難排解

- **報告沒寄來**：claude.ai/code/routines → 該 routine 看 Runs 記錄；排定時間後 10–30 分鐘寄達屬正常延遲；可 Run now 補發。完全沒觸發→檢查 Active 開關。
- **routine 停多天後恢復**：不用處理——fetch 以最近一封報告為基準，時間窗自動涵蓋空窗。
- **報告重複列信/爆量**：檢查寄件備份最近一封報告主旨是否仍以前綴開頭（被改＝基準失效）；報告被刪光會退回 `default_days` 時間窗重報一次。
- **token 失效**（「換取 access token 失敗」）：重跑 `get_refresh_token.py`（安裝手冊步驟 3），新 token 更新到 routine 環境變數（本機有 .env 也要同步）。
- **改規則三處同步**：routine prompt（雲端）＋ `config.json`（GitHub）＋ 本 SKILL.md 若有客製註記。
- **年度更新**（長期用戶）：track_keywords 若含年度字樣要換年份；長假前關 Active、回來檢查 token 再開。

## 隱私與安全（發給同事前必讀）

- 私人信箱一律用**自己的 Claude 帳號＋自己的 GitHub 私人 repo＋自己的 OAuth 憑證**；不共用 client secret。
- 雲端環境變數未加密——refresh token 等於信箱鑰匙（gmail.modify 權限），只放自己控制的環境。
- 停用時：關 routine ＋ 到 myaccount.google.com/permissions 撤銷應用程式授權。
