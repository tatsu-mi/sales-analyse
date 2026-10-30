# 営業メール分析ツール

## セットアップ

Python 3.11以上。プロジェクトのルートでPowerShellから実行します。

```powershell
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

`config.toml` と同じフォルダの `.env` を編集します。

| `.env` の項目 | 設定値 |
| --- | --- |
| `SAKURA_MAIL_HOST` | IMAPホスト名 |
| `SAKURA_MAIL_USERNAME` | メールアドレス全体 |
| `SAKURA_MAIL_PASSWORD` | IMAPパスワード |
| `GEMINI_API_KEY` | Gemini APIキー |
| `TEAMS_WEBHOOK_URL` | Teams通知時のみ必要 |
| `DRIVE_ID` | GHAの `SHAREPOINT_DRIVE_ID` と同じ値 |
| `FOLDER_ID` | GHAの `SHAREPOINT_FOLDER_ID` と同じ値 |

SharePointから `engineers.md`・`priority_rules.md` をダウンロードし、`config.toml` の `analysis.engineers_file`・`analysis.priority_rules_file` の場所へ配置します。

- エンジニア一覧：`## 名前` ごとにプロフィールを記載。
- 判定条件：`priority_rules.example.md` を参考に編集。
- ログ：`config.toml` の `[logging] level` で設定。
- `.env` と実データはGitにコミットしないでください。

## ローカル実行

```powershell
python -m sales_analyze.validate_config
python -m sales_analyze.mail_analyzer

# 期間指定
python -m sales_analyze.mail_analyzer --start 2026-10-01 --end 2026-10-15

# メール取得のみ
python -m sales_analyze.mail_analyzer --fetch-only

# Teams接続テスト
python -m sales_analyze.scheduled_run --test-notification
```

結果：`output/report.html` をブラウザーで開きます。別設定は `--config`、ログ保存は `--log-file` で指定します。

## 再マッチング

SharePointから取得する場合は、Azure CLIをインストールしてログインします。

```powershell
az login --tenant "テナントID" --allow-no-subscriptions

# 一時ファイルIDを指定。最新のプロフィール・ルールをSharePointから取得
python -m sales_analyze.rematch --run-id 123456789-1

# ローカルの分析結果を使用（SharePoint接続不要）
python -m sales_analyze.rematch --source-dir output/元の分析結果フォルダ

# 保存済みメールの本文抽出から再開
python -m sales_analyze.rematch --source-dir output/元の分析結果フォルダ --start-from extraction
```

再開地点は `--start-from` で指定します。

| 指定値 | 再開地点 |
| --- | --- |
| `screening` | 件名選別 |
| `extraction` | メール本文からの案件抽出 |
| `aggregation` | 案件集計 |
| `profiles`（既定） | プロフィール構造化 |
| `matching` | 案件キーワード照合 |
| `scoring` | 採点・レポート作成 |

- 必須設定：`GEMINI_API_KEY`。SharePoint利用時は `DRIVE_ID`・`FOLDER_ID` も設定。
- 結果：`output/rematch_日時_識別子/result/report.html` をブラウザーで開きます。
- SharePoint保存・Teams通知：`--publish` を追加。
- ID指定時はローカルのプロフィール・ルール設定先を最新データで上書き。
- SES共通条件は元の分析と同じ内容・順序にしてください。

[再マッチングの仕様](docs/rematching.md)

## GitHub Actions

[SharePoint接続設定](docs/sharepoint-setup.md) を済ませ、デフォルトブランチで **Run workflow** を実行します。

| ワークフロー | 入力 |
| --- | --- |
| Mail analysis and Teams report | なし |
| Resume mail analysis | 失敗した実行の `run_id`（例：`123456789-1`） |
| Rematch saved mail analysis | 元の一時ファイルの `run_id`（完了済みも可）、再開地点の `start_from`（上表参照） |
| SharePoint upload and Teams notification test | なし |
| Update engineers from skill sheets | `mode`: `append`（追記） / `overwrite`（上書き）。[準備・実行手順](docs/skill-sheets.md) |

エラーログ：`temporary-runs/<run_id>/error-*.log`。再マッチングは `temporary-runs/rematch-<GitHub実行ID>-<試行回数>/`。

再利用する実行の `temporary-runs/<run_id>/` は残してください。`run-state/checkpoint.json` は通常編集しません。
