# さくらメール分析ツール

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

`.env` に設定する値は「シークレット」を参照してください。通常実行は前回正常完了した取得範囲の終了時刻以降、初回は `analysis.lookback_hours`（既定24時間）を対象にします。結果は `output/report.md` に保存され、実行ごとの履歴も `output/` に残ります。

メールはメールボックスへの到着順（IMAP UIDの降順）で新しいものから取得し、`analysis.max_emails`（既定2000件）に達したら取得を終了して分析に進みます。期間外のメールは取得件数に含めません。

本文の分析対象は、重複の集約と件名による除外を行った後の最大1000件です。1000件を超える場合は受信日時の古いメールから除外し、新しい1000件を分析します。上限超過による除外件数はレポートと集計に記録します。

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

Teamsには、SESはスキルシートPDFごと、受託は全体で上位10件（`top_n`設定に従う）の案件タイトル一覧とレポート全文を、通し番号付きで分割投稿します。同じスキルシート内では上限に収まる複数の案件を1投稿にまとめ、次の案件が収まらない場合はその案件の手前で区切ります。スキルシートごとの掲載を終えたら、投稿サイズに余裕があっても必ず区切り、次のスキルシートや受託は別の投稿にします。1案件だけで投稿上限を超える場合に限り案件内でも分割します。日本語やカードのJSONを含む送信サイズを24,000バイト以内に収めます。候補がないカテゴリは「該当候補はありません。」と表示します。保存済みレポートのダウンロード用リンクも掲載します。

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

## エンジニア別のSES案件選定

`スキルシート/` にPDFを置いてください。1ファイルを1名として扱い、サブフォルダも検索します。
フォルダは `analysis.skill_sheets_dir` で変更でき、相対パスは設定ファイルの場所が基準です。
同名ファイルもサブフォルダを含む相対パスで区別します。

SESは共通の案件条件を満たす全候補をPDFごとに照合し、必須スキル・経験・担当工程・勤務条件の適合を確認できた順に最大10通選びます。
`priority_rules.toml` の `[ses].criteria` は、案件地域を含め全エンジニア共通で引き続き適用します。
共通条件で明確に不適合の案件は全員の候補から除外し、その後にPDFごとのスキル・通勤条件を追加判定します。
不明な条件は「要確認」として残し、明確な不適合は除外します。同じ案件が複数名の候補になることがあります。
追加の通勤条件として、PDF本文の最寄り駅から就業先まで片道90分以内（90分を含む）、または出社不要のフルリモートを必須とします。フルリモートでも共通条件の不適合を免除しません。
乗換・徒歩・待ち時間を含み、リモート併用や初日・研修時のみ出社する場合も通勤判定が必要です。
経路検索サービスとは連携していないため、資料に経路・所要時間の根拠がない場合は推測せず対象外にします。
最寄り駅や勤務地が不明な場合も対象外ですが、出社不要のフルリモートなら最寄り駅は不要です。
レポートには通勤経路・片道所要時間・根拠を表示します。受託の勤務地条件は `priority_rules.toml` の設定に従います。
候補が10件に満たない場合は補充せず、該当する件数だけ表示します。受託はPDFを使わず、全体で最大10通です。
レポートとTeams通知にPDF別の一覧を表示し、レポートには照合根拠・確認事項も記載します。
`ranked_emails.json` の `ses_by_engineer` にファイル名・PDFのSHA-256・候補を保存します。

PDFの抽出テキストは案件照合のためGeminiに送信します。照合は既存のトークン予算・再試行・キャッシュを使用します。
画像のみのページはOCR済みPDFに差し替えてください。読み取れないPDFや存在しないフォルダはエラーで停止します。
空のフォルダではSESを「スキルシートPDF未登録」と表示し、受託の選定は継続します。
GitHub Actionsで実行する場合も、実行環境の同じ設定先にPDFを配置する必要があります。
