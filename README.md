# さくらメール分析ツール

## ローカルでの実行

Python 3.11以上を使用します。外部パッケージのインストールは不要です。リポジトリのフォルダで以下を実行します（PowerShellの例）。

```powershell
Copy-Item .env.example .env
notepad .env
notepad config.toml
python validate_config.py
python mail_analyzer.py
```

`.env` に設定する値は「シークレット」を参照してください。通常実行は前回正常完了した取得範囲の終了時刻以降、初回は `analysis.lookback_hours`（既定24時間）を対象にします。結果は `output/report.md` に保存され、実行ごとの履歴も `output/` に残ります。

必要に応じて次のコマンドを使います。

```powershell
# メール取得のみ（Gemini APIキー不要）
python mail_analyzer.py --fetch-only

# 期間を指定して分析（開始日・終了日を含む、日本時間）
python mail_analyzer.py --start 2026-10-01 --end 2026-10-15

# 別の設定ファイルで実行
python mail_analyzer.py --config path/to/config.toml

# Teamsへのテスト投稿（TEAMS_WEBHOOK_URLのみ必要）
python scheduled_run.py --test-notification
```

期間指定の実行は、通常実行の前回時刻を更新しません。Teamsのテスト投稿後は投稿先チャネルで表示を確認してください。

## GitHub Actions（GHA）での手動実行

1. GitHubの **Settings → Secrets and variables → Actions → Repository secrets** に「シークレット」の5項目を登録します。
2. **Actions → Mail analysis and Teams report → Run workflow** を開きます。
3. デフォルトブランチを選び、**Run workflow** をクリックします。
4. 実行結果とTeamsの通知を確認します。

## シークレット

ローカルでは `.env`、GHAでは同名のRepository secretsに設定します。

| 名前 | 内容 |
| --- | --- |
| `SAKURA_MAIL_HOST` | IMAPサーバーのホスト名 |
| `SAKURA_MAIL_USERNAME` | IMAPログイン名（メールアドレス全体） |
| `SAKURA_MAIL_PASSWORD` | IMAPパスワード |
| `GEMINI_API_KEY` | Gemini APIキー |
| `TEAMS_WEBHOOK_URL` | Teams WorkflowsのWebhook URL。GHAの通知で使用 |

```dotenv
SAKURA_MAIL_HOST="example.sakura.ne.jp"
SAKURA_MAIL_USERNAME="sales@example.com"
SAKURA_MAIL_PASSWORD="メールパスワード"
GEMINI_API_KEY="GeminiのAPIキー"
TEAMS_WEBHOOK_URL="Teams WorkflowsのURL"
```

`.env` は指定した `config.toml` と同じフォルダから自動で読み込みます。既存の環境変数が優先されるため、GHAでは `.env` の作成は不要です。`.env` はGit管理対象外です。

これらの値をTOMLには記載しないでください。`imap.host`・`imap.username`・`imap.password`・`gemini.api_key`・`teams.webhook_url` がTOMLにあると、空欄でも設定エラーになります。
