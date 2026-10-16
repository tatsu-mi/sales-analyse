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

本文の分析対象は、重複の集約と件名による除外を行った後の最大1000件です。1000件を超える場合は受信日時の古いメールから除外し、新しい1000件を分析します。上限超過による除外件数はレポートと集計に記録します。取得時の安全上限 `analysis.max_emails` は別に適用されます。

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

期間指定の実行は、通常実行の前回時刻を更新しません。Teamsのテスト投稿後は投稿先チャネルで表示を確認してください。

`--test-report`はUTF-8のMarkdownファイル単体で実行できます。認証情報は`TEAMS_WEBHOOK_URL`のみ必要で、指定した設定ファイルと同じフォルダの`.env`から読み込みます。メール取得・Geminiでの分析・処理済み時刻の更新は行いません。通常の投稿と同じ分割処理で複数案件をまとめ、各投稿に「【テスト投稿】」と通し番号を付けます。レポート内のローカルファイルへのリンクはTeamsから開けません。

## GitHub Actions（GHA）の定期実行

日本時間の月〜金曜7:00を予定時刻とし、起動時の日本の日付が土日・祝日・振替休日、または毎年12月29日〜1月4日（両端を含む）ならメール分析とTeams通知をスキップします。祝日は[内閣府の祝日CSV](https://www8.cao.go.jp/chosei/shukujitsu/gaiyou.html)で判定します。データを取得できない場合や当年のデータがない場合は、分析・通知を行わず失敗として記録します。GHAのスケジュールには遅延が発生することがあります。

休みにスキップしても処理済み時刻は更新せず、次の通常実行で前回正常配信以降のメールを対象にします。手動実行は休日判定を省略し、休みの日でも実行できます。

## GitHub Actions（GHA）での手動実行

1. GitHubの **Settings → Secrets and variables → Actions → Repository secrets** に「シークレット」の5項目を登録します。
2. **Actions → Mail analysis and Teams report → Run workflow** を開きます。
3. デフォルトブランチを選び、**Run workflow** をクリックします。
4. 実行結果とTeamsの通知を確認します。

Teamsには、SES・受託それぞれの上位10件（`top_n`設定に従う）の案件タイトル一覧とレポート全文を、通し番号付きで分割投稿します。上限に収まる複数の案件は1投稿にまとめ、次の案件が収まらない場合はその案件の手前で区切ります。1案件だけで投稿上限を超える場合に限り案件内でも分割します。日本語やカードのJSONを含む送信サイズを24,000バイト以内に収め、投稿間隔は3秒空けます。候補がないカテゴリは「該当候補はありません。」と表示します。保存済みレポートのダウンロード用リンクも掲載します。

すべての分割投稿が受理されてから処理済み時刻を更新します。途中で失敗した場合、再実行時に送信済みの部分が重複することがあります。

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
