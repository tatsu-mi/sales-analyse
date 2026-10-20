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

## エンジニア一覧の設定

`engineers.md` を編集して、エンジニアごとに `## 名前` の見出しとプロフィールを記載してください。共通注意事項は最初のエンジニアの見出しより前に記載します。

ファイルを変更する場合は、`config.toml` の `analysis.engineers_file` に設定してください。相対パスは設定ファイルの場所が基準です。GitHub Actionsでも同じ設定先にUTF-8のファイルを配置してください。

各人の `### マッチングフィードバック` 欄に、案件名・判定（適合／不適合／要確認）・理由を追記できます。以下は記入例です。実際に確認した内容に置き換えてください。

```markdown
### マッチングフィードバック

- 案件名：○○システム開発（案件IDや受信日も分かれば記載）
  - 判定：不適合
  - 理由：必須のバックエンド実務3年以上を確認できない。研修期間を実務に含めない。
```

## 判定結果の評価

実行フォルダの `engineer_assessments.json` を確認し、人が確認した正解を `matching_labels.json` に記載してください。対象レコードの `engineer`・`uid`・`project_key` をそのままコピーし、`expected` に `match`（適合）・`mismatch`（不適合）・`unknown`（情報不足）のいずれかを指定します。

```json
[
  {"engineer": "EK", "uid": "対象UID", "project_key": "対象レコードのキー", "expected": "mismatch"}
]
```

```powershell
python evaluate_matching.py output/対象実行フォルダ/engineer_assessments.json matching_labels.json
```

正解を記載した案件に対する集計と判定の相違が表示されます。評価結果を比較するときは、同じ案件・エンジニアの組み合わせに正解を付けてください。
