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

1. GitHubの **Settings → Secrets and variables → Actions → Repository secrets** に「シークレット」の値を登録します。
2. 下記の「SharePointへの接続設定」を完了します。
3. **Actions → Mail analysis and Teams report → Run workflow** を開きます。
4. デフォルトブランチを選び、**Run workflow** をクリックします。
5. 実行結果とTeamsに投稿されたリンクを開き、SharePointのレポートを確認します。

途中で失敗した場合は **Actions → Resume mail analysis → Run workflow** を開き、デフォルトブランチを選んで `run_id` に元の実行の `GitHub実行ID-試行回数`（例：`123456789-1`）を入力してください。通常ワークフローの失敗メッセージにもこのIDを表示します。再開が失敗した場合も同じIDを指定します。

エラーの詳細は、SharePointの `temporary-runs/<run_id>/error-日時-識別子.log` を確認してください。失敗するたびに実行ログと例外の詳細を保存します。認証情報は伏せ字になります。SharePointへのログ保存自体が失敗した場合はActionsの進捗欄に表示します。

途中までの分析結果を再利用するには、同じ `run_id` を指定して再開用ワークフローを実行してください。通常ワークフローを新しく起動すると別の実行になります。Actionsに途中結果の保存失敗が表示された場合は、最後にSharePointへ保存できた分から再開します。

保存先の `temporary-runs/<run_id>/` にある `progress.json` とスナップショットを残してください。実行時の `config.toml`、`engineers.md`、`priority_rules.md` も同じフォルダで確認できます。再開には保存された条件を使用するため、条件を変更して実行する場合は通常ワークフローを起動してください。完了済みの実行や、別の実行が先に通知を完了した古い実行は再開できません。

前回実行時間はSharePointの `run-state/checkpoint.json` で管理します。このファイルがない初回は、設定の `analysis.lookback_hours` に従って取得します。チェックポイントの削除・編集は取得期間に影響するため、復旧時以外は行わないでください。

一時保存フォルダにはメール本文やプロフィールが含まれます。対象フォルダの閲覧権限を運用担当者に設定してください。不要になった完了済みの `temporary-runs/<run_id>/` はSharePointから削除できます。

## SharePointへの接続設定

1. Entraのアプリ登録に、Microsoft Graphのアプリケーション権限 `Sites.Selected` を追加し、管理者同意を付与します。
2. 管理者に、保存先SharePointサイトへのアプリの `write` 許可を依頼します。
3. アプリの「証明書とシークレット → フェデレーション資格情報」で、対象GitHubリポジトリのデフォルトブランチを登録します。GitHub Environmentを使用する場合は、ブランチではなくそのEnvironmentに合わせます。
4. Repository secretsに `AZURE_CLIENT_ID` と `AZURE_TENANT_ID`、Repository variablesに `SHAREPOINT_DRIVE_ID`（ライブラリID）と `SHAREPOINT_FOLDER_ID`（対象フォルダID）を登録します。
5. レポートは対象フォルダ内の `reports/` に保存します。フォルダがなければ実行時に作成します。レポートを読む人に `reports/` の閲覧権限を設定してください。
6. 対象フォルダの直下に、UTF-8の `engineers.md` と `priority_rules.md` をアップロードします。

設定済みのIDがSecrets側にある場合は、Variables側に登録してください。クライアントシークレットとAzureサブスクリプションIDの登録は不要です。

接続テストは **Actions → SharePoint upload and Teams notification test → Run workflow** から、OIDCで許可したブランチを選んで実行します。Teamsの「テスト投稿」のリンクを開き、SharePointのテストレポートを確認してください。テスト後のファイルは必要に応じてSharePointから削除してください。

## シークレット

ローカルでは `.env`、GHAでは同名のRepository secretsに設定します。

| 名前 | 内容 |
| --- | --- |
| `SAKURA_MAIL_HOST` | IMAPサーバーのホスト名 |
| `SAKURA_MAIL_USERNAME` | IMAPログイン名（メールアドレス全体） |
| `SAKURA_MAIL_PASSWORD` | IMAPパスワード |
| `GEMINI_API_KEY` | Gemini APIキー |
| `TEAMS_WEBHOOK_URL` | Teams WorkflowsのWebhook URL。GHAの通知で使用 |
| `AZURE_CLIENT_ID` | Entraのアプリケーション（クライアント）ID。GHAのみ |
| `AZURE_TENANT_ID` | Entraのディレクトリ（テナント）ID。GHAのみ |

```dotenv
SAKURA_MAIL_HOST="example.sakura.ne.jp"
SAKURA_MAIL_USERNAME="sales@example.com"
SAKURA_MAIL_PASSWORD="メールパスワード"
GEMINI_API_KEY="GeminiのAPIキー"
TEAMS_WEBHOOK_URL="Teams WorkflowsのURL"
```

`.env` は指定した `config.toml` と同じフォルダに配置してください。GHAでは `.env` の作成は不要です。シークレットをTOMLに記載したり、Gitにコミットしたりしないでください。

ログの出力レベルは `config.toml` の `[logging]` で設定します。通常は `level = "INFO"`、詳細を調べる場合は `level = "DEBUG"` にしてください。`WARNING`・`ERROR`・`CRITICAL` も指定できます。旧設定の `enabled = true/false` は削除して `level` に置き換えてください。ローカルでログを保存する場合は `python mail_analyzer.py --log-file execution.log` を実行します。

## エンジニア一覧の設定

SharePointの対象フォルダ直下にある `engineers.md` を編集して、エンジニアごとに `## 名前` の見出しとプロフィールを記載してください。共通注意事項は最初のエンジニアの見出しより前に記載します。GHAでは実行のたびにこのファイルを取得します。取得や形式の検証に失敗した場合は分析を開始しません。

ローカル実行時はSharePointからダウンロードし、`config.toml` の `analysis.engineers_file` の場所（初期値は `engineers.md`）に配置してください。相対パスは設定ファイルの場所が基準です。GHAでもダウンロードしたファイルをこの設定先に配置します。`engineers.md` と `スキルシート/` はGit管理の対象外です。実データを別名でコミットしないでください。

各人の `### マッチングフィードバック` 欄に、案件名・判定（適合／不適合／要確認）・理由を追記できます。以下は記入例です。実際に確認した内容に置き換えてください。

```markdown
### マッチングフィードバック

- 案件名：○○システム開発（案件IDや受信日も分かれば記載）
  - 判定：不適合
  - 理由：必須のバックエンド実務3年以上を確認できない。研修期間を実務に含めない。
```

## 判定条件の設定

SharePointの対象フォルダ直下に `priority_rules.md` を配置してください。GHAでは毎回取得し、形式を検証してから分析を開始します。取得や検証に失敗すると停止します。

新しく作る場合は `priority_rules.example.md` をコピーし、各見出しの条件を編集してください。`## SES: 表示名` の条件は、1項目ずつ `- ` で始めます。件名パターンはバッククォートで囲んだ正規表現、機械選別・AI選別は `有効` または `無効` で指定します。表示件数は1〜10、一括処理件数は1〜100です。サンプルの見出し名は維持してください。

ローカルではSharePointからダウンロードし、`analysis.priority_rules_file` の場所（`config.toml` の初期値は `priority_rules.md`）に配置してください。実データを含む `priority_rules.md` と旧 `priority_rules.toml` はGit管理対象外です。`config.example.toml` は公開用サンプルを参照します。実際の条件をサンプルへ書かないでください。

```powershell
# ルールだけを検証
python validate_config.py priority_rules.md

# ローカルの設定とルールを検証
python validate_config.py

# SharePoint取得前のCIで設定構造と公開用サンプルを検証
python validate_config.py --structure-only
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
