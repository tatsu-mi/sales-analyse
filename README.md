# さくらメール分析ツール

IMAPで取得した営業メールをGeminiで3分類し、条件を検証してSES・受託それぞれ上位10通までを抽出します。Python 3.11以上が必要です。外部パッケージは不要です。

## 実行

一般設定はローカル・GitHub Actions共通の `config.toml`、認証情報・IMAP接続先は同じフォルダの `.env` に分けます。`config.toml` はリポジトリで管理し、初回は `.env` のサンプルをコピーして編集します。

```powershell
Copy-Item .env.example .env
notepad config.toml
notepad .env
python mail_analyzer.py
```

期間を指定しない通常実行は、前回正常完了した実行の取得範囲の終了時刻から、今回の実行開始時刻までを対象にします。初回（記録がない場合）は直近24時間を対象にします。サーバー受信日時 `INTERNALDATE` で秒単位に絞り込み、開始時刻以上・終了時刻未満を取得します。日本時間を使用します。古い設定に `start_date` / `end_date` が残っていても、通常実行ではこの差分取得を優先します。

前回実施時刻は `output/.last_runs/` 以下にメールボックス（接続先・ポート・ユーザー名・フォルダ）別で原子的に保存します。正常完了時だけ更新し、取得・分析に失敗した場合は前回の記録を維持します。対象0件の場合や `--fetch-only` の取得成功時も更新します。保存するのは実行開始時刻なので、処理中に届いたメールは次回取得されます。

`--start` / `--end` による期間指定、または `--emails-file` による再分析では、この記録を参照・更新しません。記録が不正な場合は通常実行を停止しますが、明示期間の実行は記録の状態に影響されません。同じ秒に再実行し、新しい取得範囲がない場合は取得せず終了します。

```toml
[analysis]
lookback_hours = 24
priority_rules_file = "priority_rules.toml"
```

`priority_rules_file` と `output_dir` の相対パスは `config.toml` の場所が基準です。判定ファイルは起動時に読み込み・検証します。

取得だけ確認する場合や、過去の日付範囲を明示して分析する場合:

```powershell
python mail_analyzer.py --fetch-only
python mail_analyzer.py --start 2026-10-01 --end 2026-10-15
python mail_analyzer.py --config path/to/config.toml
```

日付指定時は開始日・終了日の両方を含みます。片方だけ指定する場合、もう片方は設定の `start_date` / `end_date` が必要です。

## 分類と優先度

本文分析の前に、**件名の機械除外 → 件名だけのAI選別 → 残ったメールの本文分析** を実施します。設定は `priority_rules.toml` の `[prefilter]` です。GHA・ローカル実行の両方に適用され、既存の取得・Teams投稿フローはそのまま利用できます。

機械除外は `exclude_patterns` の正規表現で判定します。初期値は社員・人材・要員紹介、挨拶、自社製品・サービス紹介、セミナー案内、テスト専任・運用専任などです。件名をUnicode NFKCで正規化し、大文字・小文字を区別せず照合します。`keep_patterns` にも該当する場合は機械除外を見送り、AI選別に回します。

AI選別に送信するメール情報はUIDと件名だけです。SES・受託の条件を参考に、件名から明確に対象外と判断できるものを除外します。要員の紹介と募集を区別し、条件不明・件名なし・案件混在の可能性があれば本文分析に残すよう指示します。技術名や金額等だけから対象外と推測せず、最終的な条件判定は本文で実施します。件名だけの選別には誤除外の可能性があるため、必要に応じて除外文言を調整してください。

`enabled = false` で事前選別をすべて無効化、`ai_enabled = false` で機械除外だけにできます。`batch_size`（1〜100）で件名のAI選別をまとめる件数を設定します。トークン上限を超える場合はさらに分割します。AI応答のUID欠落・重複・形式不正を検出した場合は、再分割して検証し、解消できなければ停止します。

除外メール一覧や理由のファイルは作成せず、機械除外・AI除外・本文分析対象の件数だけをログ、レポート、集計に表示します。件数は完全一致の再送をまとめた後の通数です。原文の取得・保存は従来どおり行い、事前選別は本文をAIに送信する量を減らします。`--fetch-only` は選別を行わず取得のみ実施します。

各メールを次の3カテゴリに分類します。

- SES派遣の要員募集 (`ses`)
- 受託案件としてアプローチできそうな案件 (`contract`)
- その他 (`other`): 人材紹介・協業先募集・広告等を含み、優先対象から除外

「その他を捨てる」は分析上の除外です。サーバー上のメール削除・移動は行いません。取得原文は従来どおり保存し、本文分析に進んだメールだけ分類・条件判定を保存します。

各案件について、`priority_rules.toml` の条件を設定順に「適合・不適合・不明」で評価し、根拠と確認事項を記録します。メールのカテゴリ判定と条件判定は分けて扱います。要員募集を月額単価だけで受託案件と推測しません。

初期条件は以下のとおりです。細かな判定指示を含む原本は `priority_rules.toml` です。

| カテゴリ | 条件 |
| --- | --- |
| SES | 主要技術がJava・C#・VB.NET・Python・C・C++、主業務がテスト・運用以外、月額45万〜90万円、対象地域、1ヶ月以内の参画、期間1ヶ月以上、週5日 |
| 受託 | 対象地域、案件総額300万円以下、フルスクラッチ・既存改修等でSaaS提案不要、HP制作・デザインを主目的としないシステム開発 |

対象地域は札幌、仙台、東京、千葉、埼玉、神奈川、名古屋、京都、大阪、神戸、広島、福岡です。SES単価の両端と受託300万円は含みます。月額・人月単価を案件総額とみなさず、地域・期間・稼働日数等が書かれていない場合は不明とします。1ヶ月以内の参画は分析期間の終了日時を基準に暦月で判定します。

一つでも明確な不適合がある案件、募集終了・期限切れ案件は候補から除外します。不明条件のある案件は「要確認」として残し、全条件適合より下位にします。件数を埋めるために不適合を混ぜません。

順位はプログラムで、適合条件数、募集中の明記、受信日時の新しさ、UIDの順に確定します。同じメールに複数案件がある場合、カテゴリごとに最も適合する案件がそのメールを代表します。混在メールは両カテゴリに入る場合があります。カテゴリごとに最大10通、該当数が少なければその件数だけ出します。`top_n` は1〜10に設定できます。

同じ送信元・件名・本文の完全一致再送は最新メールにまとめ、関連UIDを残します。省略本文だけの一致はまとめません。同じ送信元の明記された案件IDは条件履歴をまとめ、最新条件を判定に使います。似ているだけの案件は統合しません。

AIは原文から分類と条件評価を行います。順位付けとレポート生成はプログラムで行うため、要約による候補の脱落はありません。全ての条件判定がそろっていない応答やUIDの欠落・重複があれば、再分割して検証し、完了できない場合は停止します。根拠は原文と照合してください。

## 認証情報と設定

IMAP接続先・認証情報とAPIキー・Teams Webhook URLは `.env` または環境変数で設定します。TOMLには `imap.host` / `imap.username` / `imap.password` / `gemini.api_key` / `teams.webhook_url` を書かず、空欄の項目も削除してください。残っている場合は設定エラーとして停止します。

```dotenv
SAKURA_MAIL_HOST="example.sakura.ne.jp"
SAKURA_MAIL_USERNAME="sales@example.com"
SAKURA_MAIL_PASSWORD="メールパスワード"
GEMINI_API_KEY="GeminiのAPIキー"
TEAMS_WEBHOOK_URL="Teams WorkflowsのURL"
```

`.env` は指定したTOMLと同じフォルダから自動で読み込みます。既存の環境変数が優先されるため、GHAはこれまでどおりGitHub Secretsを環境変数へ渡せます。GHA上で `.env` ファイルを生成する必要はありません。`.env` はGit管理対象外、`.env.example` だけをリポジトリに配置します。

形式は1行ごとの `KEY=value` です。単一引用符はそのままの文字列、二重引用符はJSONと同じエスケープ規則（例：バックスラッシュは `\\`）で読みます。`#` で始まる行はコメントです。変数展開やコマンド実行は行いません。パスワード前後の空白が必要な場合は引用符で囲みます。

`--fetch-only` ではGeminiのAPIキーは不要です。保存済みメールの分析にはIMAP認証情報は不要です。Teams URLは `scheduled_run.py` でTeams投稿するときだけ必要です。

設定項目の例とコメントは `config.example.toml` を参照してください。主な上限は `max_emails`（10,000通）、`max_body_chars`（8,000文字/通）、`max_message_bytes`（添付を含め10MB/通）です。件数・サイズ上限超過時は停止します。添付内容は分析しません。

Geminiは入力トークン数でファイル分割し、モデル上限とローカルの分あたり利用制限に従って処理します。HTTP 429・5xx・通信障害は設定範囲内で再試行します。日次上限や割当量0、指定待ち時間が上限超過の場合は停止します。`logging.enabled` でログを制御し、`--log-file path` でUTF-8ログを保存できます。

## 出力と再実行

実行ごとに `output/実行日時_ランダムID/` を作成します。

レポートは各履歴フォルダの `report.md` に保存し、出力先直下の `output/report.md` も最新の内容で上書き更新します。対象0件の実行も更新対象です。取得のみ・分析失敗時は前回のレポートを維持します。出力先直下のレポート内リンクは対応する履歴フォルダを参照します。

| ファイル | 内容 |
| --- | --- |
| `report.md` | カテゴリ別の順位、案件情報、問い合わせ先、参照元メール（件名・送信元・宛先・本文を転記）、次の対応。条件ごとの判定表・根拠はJSONに保持 |
| `ranked_emails.json` | 各カテゴリ上位10通までの機械可読データ。source_emailに参照元メールを保持。UID・関連UIDは内部照合用に保持 |
| `priority_rules.json` | この実行で実際に使用した判定条件のスナップショット |
| `emails.jsonl` | 取得原文。その他メールも含む |
| `extracted_emails.jsonl` | 事前選別を通過したメールの分類・案件・条件判定・関連UID |
| `opportunities.jsonl` | 最新条件・過去条件・期限判定を含む案件一覧。不適合案件も監査用に保持 |
| `statistics.json` | 取得件数・事前除外件数・本文分析対象件数・分析後の分類件数・案件群等の集計。分類件数は事前除外分を含まない。優先対象の件数は `ranked_emails.json` で確認 |
| `metadata.json` | 対象日時（`start_at` 以上・`stop_at` 未満）、件数、進捗、状態 |
| `analysis/`・`analysis.sqlite3` | 分割送信ファイル・成功応答・集計用データ |

`--fetch-only` は原文・メタデータのみ保存します。取得中断時は `emails.partial.jsonl` を残し、未完了データを成功扱いにはしません。分析失敗時も原文と成功済み抽出結果を残します。

```powershell
python mail_analyzer.py --emails-file output/取得済みフォルダ/emails.jsonl
python mail_analyzer.py --refresh
```

`--emails-file` は保存時の正確な日時範囲を使って再分析します（直近24時間への再絞り込みはしません）。旧 `emails.json` と日付だけのメタデータも読めます。隣の `metadata.json` が必要です。

自動再利用はメールボックス・正確な対象日時・本文上限等が一致した取得済みデータだけです。通常実行は前回時刻を開始境界とする新しい範囲を取得します。分析だけを再試行する場合は `--emails-file` で明示してください。判定条件の変更は抽出キャッシュにも反映されます。`--refresh` は同じ取得範囲をIMAPから再取得します。前回実施時刻をリセットする指定ではありません。

IMAPフォルダは読み取り専用で開き、`BODY.PEEK[]` で取得します。既読状態の変更・送信は行いません。本文はGeminiへ送信され、ローカル出力にも含まれます。`.env` と `output/` は `.gitignore` の対象です。認証情報を含まない `config.toml` はGit管理対象です。

## テスト

```powershell
python -m unittest discover -s tests -v
```

外部サービスをモックしたテストです。実サーバー・実APIへの接続は行いません。

## TOML変更時の検証

`.github/workflows/validate-config.yml` はTOML変更のpush・Pull Request、および手動実行で、`config.toml` / `config.example.toml` / `priority_rules.toml` を検証します。構文、未対応の設定名、型、正の整数、IMAPポートの範囲、再試行待ち時間の整合性、日付、参照先の判定条件、除外用正規表現などを確認します。TOMLへの認証項目の混入も検出します。

検証は `.env` やGitHub Secretsを読み込まず、IMAP・Gemini・Teamsに接続しません。モデルIDの実在や認証情報の有効性は検証対象外です。ローカルでも同じ検証を実行できます。

```powershell
python validate_config.py
python validate_config.py config.toml priority_rules.toml
```

通常の定期実行ワークフローでも、メール取得前に同じ検証を実行します。

## GitHub Actionsでの定期実行とTeams投稿

`.github/workflows/mail-report.yml` は毎日 **9:15（日本時間）** に、メール取得 → Gemini分析 → レポートArtifact保存 → Teamsへリンク通知 → 次回用の時刻保存を実行します。Actions画面の **Run workflow** から手動実行もできます。定期実行・手動実行ともデフォルトブランチを使用します。外部Pythonパッケージは不要です。

1. `config.toml` と `priority_rules.toml` をリポジトリに配置します。モデル、分析条件、上限、出力先、Teamsの通信タイムアウトなど、認証情報以外はTOMLで管理します。`config.toml` はローカル実行とGHAで共用します。変更は次回の実行から反映されます。
2. GitHubの **Settings → Secrets and variables → Actions → Repository secrets** に以下を登録します。これらの項目はTOMLから削除してあり、実行時はSecretsから環境変数として渡します。

   | Secret名 | 内容 |
   | --- | --- |
   | `SAKURA_MAIL_HOST` | IMAPサーバーのホスト名 |
   | `SAKURA_MAIL_USERNAME` | IMAPログイン名（メールアドレス全体） |
   | `SAKURA_MAIL_PASSWORD` | IMAPパスワード |
   | `GEMINI_API_KEY` | Gemini APIキー |
   | `TEAMS_WEBHOOK_URL` | Teams WorkflowsのWebhook URL |

3. Teamsの投稿先チャネルで **Workflows** を開き、Webhookを受信してチャネルに投稿するテンプレート（「When a Teams webhook request is received」トリガー）を作成します。呼び出し可能なユーザーは **Anyone** にし、投稿先チーム・チャネルを指定して保存し、発行されたURLを `TEAMS_WEBHOOK_URL` に登録します。URL自体が認証情報になるためTOMLには書きません。運用継続のため共同所有者も設定してください。[Microsoftの設定手順](https://learn.microsoft.com/en-us/microsoftteams/platform/webhooks-and-connectors/how-to/add-incoming-webhook)
4. デフォルトブランチへ配置後、Actionsで **Mail analysis and Teams report → Run workflow** を実行し、Teamsの投稿内容とWorkflowsの実行履歴を確認します。WebhookのHTTP成功は受信の受理を表すため、Teams側で後続のチャネル投稿が失敗していないことも確認してください。

Teamsには `mail-report` Artifactへのリンクだけを1件投稿します。レポート本文や参照元メール本文はTeamsに転記しません。リンク先から `report.md` / `ranked_emails.json` / `priority_rules.json` をダウンロードできます。閲覧には対象リポジトリへの読み取り権限とGitHubへのサインインが必要です。保存期間は14日です。上位案件の参照元メール本文はレポートとranked_emails.jsonに含みます。全件の原文ファイルや分析キャッシュはArtifactに含めません。リポジトリ閲覧権限は投稿先に合わせて設定してください。

前回時刻は `mail-checkpoint` Artifact（保存期間90日）に保存し、同じワークフロー・デフォルトブランチの直近成功実行から復元します。初回はTOMLの `analysis.lookback_hours`（現在24時間）を使います。以降は投稿まで成功した前回の取得終了時刻から取得するため、分析・Teams通信失敗時の範囲も次回に残ります。保存済みArtifactが削除・期限切れの場合や、メールボックス設定が変わった場合は停止します。状態を復旧・移行してから再実行してください。[GitHubのArtifact仕様](https://docs.github.com/en/actions/tutorials/store-and-share-data)

復元した時刻はメールボックス・形式・タイムゾーン・未来時刻でないことを確認してから利用します。不正なArtifactで正常なローカル時刻を上書きしません。保存するのは取得範囲の終了時刻で、分析完了時刻ではありません。分析では通知待ちの `pending.json` だけを作成します。レポートArtifactの保存成功後、そのURLをTeamsへ通知し、受理された後に `checkpoint.json` を更新・保存します。Artifact保存失敗や通知失敗では前回時刻を進めません。

TeamsのHTTP 429（投稿制限）は同じリンク通知を最大3回再試行し、`Retry-After` があれば従います。指定待ち時間が60秒を超える場合や制限が解消しない場合は停止し、前回時刻を維持します。空のレポートも成功扱いにしません。[Microsoftの投稿制限の説明](https://learn.microsoft.com/en-us/microsoftteams/platform/webhooks-and-connectors/how-to/connectors-using)

Teams通知の受理後にチェックポイントArtifactの保存で失敗すると、次回に同じ範囲が再分析され、リンクが再通知されることがあります。Webhookには重複排除機能を設けていません。初回成功前の失敗には保存済み時刻がないため、再実行時点から `lookback_hours` を遡ります。長期間失敗した場合は、この値を必要な期間まで増やしてください。

同時実行は抑制しています。実行時刻を変える場合はワークフローの `schedule.cron` をUTCで編集します。9:15 JSTは `15 0 * * *` です。GitHubのスケジュール実行には遅延があり、公開リポジトリでは長期間活動がないと無効化されることがあります。[GitHubのschedule仕様](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
