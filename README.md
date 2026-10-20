# 営業メール分析ツール

## ローカルでの実行

Python 3.11以上を使用します。リポジトリのフォルダで以下を実行します（PowerShellの例）。

```powershell
Copy-Item .env.example .env
python -m pip install -r requirements.txt
notepad .env
notepad config.toml
python validate_config.py
python mail_analyzer.py
```

`.env` に設定する値は「シークレット」を参照してください。実行後は `output/report.md` を確認してください。

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

# ローカルのレポート全文をTeamsへ分割テスト投稿
python scheduled_run.py --test-report output/report.md

# 任意のレポートファイル・設定ファイルを指定
python scheduled_run.py --config config.toml --test-report "path/to/report.md"
```

Teamsのテスト投稿後は投稿先チャネルで表示を確認してください。`--test-report` にはUTF-8のMarkdownファイルを指定してください。

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

`.env` は指定した `config.toml` と同じフォルダに配置してください。GHAでは `.env` の作成は不要です。シークレットをTOMLに記載したり、Gitにコミットしたりしないでください。

## スキルシートの配置

`スキルシート/` にエンジニアごとのPDFを置いてください。配置先を変更する場合は、`config.toml` の `analysis.skill_sheets_dir` に設定してください。相対パスは設定ファイルの場所を基準に指定します。

画像のみのページはOCR済みPDFに差し替えてください。
GitHub Actionsで実行する場合も、実行環境の同じ設定先にPDFを配置する必要があります。
