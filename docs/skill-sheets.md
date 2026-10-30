# スキルシートからエンジニア一覧を更新

## 準備・実行

[SharePoint接続設定](sharepoint-setup.md)と同じアプリ・GitHub Secrets / Variablesを使用します。メール・Teamsの認証情報は不要です。

`SHAREPOINT_FOLDER_ID` のフォルダ直下に `対象者スキルシート/` を作り、一人につき一つのPDFを配置します。サブフォルダは探索しません。出力先は同じ親フォルダの `engineers.md` です。PDFは1件50MB以下とし、モデルの入力上限内に収めてください。

GitHub Actionsの **Update engineers from skill sheets → Run workflow** で更新方式を選択します。

| `mode` | 動作 |
| --- | --- |
| `append`（既定） | `## 名前｜資料名.pdf` の資料名が未登録のPDFだけを解析し、末尾へ追記。既存本文・フィードバックを保持 |
| `overwrite` | 全PDFを解析して全員のセクションを再生成。既存フィードバックも削除。先頭の共通説明は保持 |

同じ資料名のPDFを差し替えた場合は `overwrite` を選びます。同じ対象者の別名PDFは自動統合しません。氏名の重複は更新前検証でエラーになるため、古いPDFを除いてください。

`config.toml` の `[gemini]` にPDF解析専用のモデルを指定できます。メール分析のモデルには影響しません。PDF入力・構造化出力に対応し、APIキーで利用可能なGeminiモデルIDを指定してください。

| 設定 | 用途 |
| --- | --- |
| `gemini.skill_sheets_model` | PDF解析専用モデル（任意） |
| `gemini.model` | 通常のメール分析モデル。`skill_sheets_model` 省略時はPDF解析にも使用 |

`config.toml` の既存の `[gemini]` 内で `skill_sheets_model` のコメントを外してモデルIDを設定します。Actions・ローカルともこの設定に従います。モデルの入出力上限は選択したモデルから取得します。RPM・TPM・再試行・最大出力トークン数は共通の `[gemini]` 設定を使います。

ローカル実行は `.env` の `GEMINI_API_KEY`・`DRIVE_ID`・`FOLDER_ID` を設定し、Azure CLIでログインします。

```powershell
az login --tenant "テナントID" --allow-no-subscriptions
python -m sales_analyze.skill_sheets --mode append
python -m sales_analyze.skill_sheets --mode overwrite
```

## 抽出・更新の仕様

PDFを一つずつGeminiへ送り、`integrated_matching.PROFILE_PROMPT` と共通スキーマに合わせて整理します。画像PDFもモデルで解析します。

| 観点 | 整理内容 |
| --- | --- |
| 特徴 | 技術、担当工程、業務知識、役割、ヒューマンスキル |
| 経験 | 実務／研修・独学・資格／不明、経験あり／明示的な未経験 |
| 期間 | 総経験と案件期間、経験月数、開始・終了月、業務識別子。並行期間を合算しない |
| 条件 | 稼働開始日、希望最低月額、居住国、契約種別。明記された項目のみ |
| 通勤 | **最寄り駅は必須**。記載がある通勤先・片道時間・経路も保持。推測しない |

出力は `## 名前｜資料名.pdf` ごとに、最寄り駅、経験や条件を保持するJSON形式の特徴、フィードバック欄を記載します。通常のマッチング処理が本文から共通の構造化データへ抽出します。

最寄り駅・氏名の欠落、不完全な抽出、不正なPDF、重複プロフィール、通信失敗があれば全体を中止し、途中までの結果は公開しません。最寄り駅がファイル名にしかない場合も、PDF本文を補完して再実行してください。

全件の解析・既存ローダーでの検証が成功した後に一度だけアップロードします。更新前の内容は `engineers-backups/<識別子>.md` に退避します。必要に応じて元の `engineers.md` として復元してください。解析中の変更も再取得して検知しますが、外部編集との完全な排他制御ではないため、実行中の手動編集は避けてください。

GHAはメール分析と同じconcurrencyグループで直列実行します。エラー詳細は `temporary-runs/engineers-<実行ID>-<試行回数>/error-*.log` に保存します。`対象者スキルシート/`・`engineers-backups/`・一時ログの閲覧権限は運用担当者に限定してください。

PDF送信形式は[Geminiのドキュメント処理API](https://ai.google.dev/gemini-api/docs/document-processing)を使用します。
