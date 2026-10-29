# 保存済みメール分析の再マッチング

`rematch.py` は保存済みメールや案件群に対し、選択した処理段階から分析とレポート作成を実行する。既定では現在のエンジニア情報を使った再マッチングを行う。IMAP取得は実行しない。

入力はSharePointの `temporary-runs/<run_id>/progress.json` が指す最新スナップショット、またはローカルの分析結果フォルダ。完了状態や通常処理の配信チェックポイントによる再開制限は適用しない。選んだ段階より前の処理結果が必要。メール0件で完了した実行も空レポートとして処理できる。

## 再開地点

`--start-from` で開始する段階を選ぶ。GitHub Actionsにも同名の選択欄がある。

| 値 | 開始する処理 | 必要な保存結果（前段階まで） |
| --- | --- | --- |
| `screening` | 件名選別 | `metadata.json`、`emails.jsonl` |
| `extraction` | メール本文からの案件抽出 | 上記と `title_selection.json` |
| `aggregation` | 案件集計 | 上記と `priority_rules.json`、`extracted_emails.jsonl`、`extraction_complete.json` |
| `profiles`（既定） | プロフィール構造化 | メール・抽出結果・ルールと `opportunities.jsonl`、`statistics.json`（件名選別ファイルは不要） |
| `matching` | 案件キーワード照合 | `profiles` と同じ入力と、プロフィール構造化のキャッシュ |
| `scoring` | 採点・レポート作成 | `matching` と同じ入力と、案件照合のキャッシュ |

```powershell
python rematch.py --source-dir output/元の分析結果フォルダ --start-from screening
python rematch.py --run-id 123456789-1 --start-from extraction
python rematch.py --source-dir output/前回の実行/result --start-from scoring
```

指定した段階以降は再実行する。件名選別の設定変更時は `screening`、SES共通条件やメール抽出の変更時は `extraction` を選ぶ。`aggregation` 以降では現在の抽出形式と元と同じSES共通条件が必要。

`matching`・`scoring` は入力フォルダの親にある `.analysis_cache/token-files-v1/` から前段階のレコードを新しい実行先へコピーし、入力・モデル・プロンプト・形式の一致と内容を検証する。ローカルへ移す際は親フォルダのキャッシュも保持する。スナップショットにも同じ配置のキャッシュが必要。結果が不足・破損している場合や現在の設定と一致しない場合は停止し、やり直すべき `--start-from` を案内する。プロフィール変更時は `profiles`、照合のやり直しは `matching`、採点の重みや表示件数だけの変更は `scoring` を選ぶ。`scoring` もAPIキーとモデル上限の照会は必要だが、生成APIは呼ばない。

`--run-id` 指定時は、SharePoint対象フォルダ直下の最新 `engineers.md`・`priority_rules.md` を毎回取得して使う。取得に失敗した場合は停止する。ローカルの設定先ファイルは取得した内容で上書きする。`--source-dir` 指定時はローカルの設定を使い、`--sharepoint-settings` を併用した場合はSharePointから取得する。

モデルとマッチング得点の重みは現在の設定ファイル、表示件数は現在の判定ルールを使う。実行ごとに独立したキャッシュを作り、選んだ段階より前の結果だけを再利用する。対象期間と基準日は元の分析期間のままとし、実行日へ変更しない。

実行ごとに `rematch_日時_識別子/` を作り、使用設定とプロフィールを保存し、`result/` へ入力データのコピーと新しい結果を保存する。メタデータには元のIDまたは入力パスと再マッチング日時を記録する。元の入力、実行状態、前回メール取得時刻を更新しない。

統合キーワード照合には案件数による固定上限を設けず、入力トークン予算と要件数・出力予算で送信分を分割する。失敗時はその送信分をさらに分割して再試行する。処理実績による件数調整は行わない。

入力トークン予算も確認する。要件数は1回最大64項目を目安とし、最大出力トークン数を128で割った値が小さければそちらを使う。1案件だけで目安を超える場合は案件を分断せず送信し、出力上限や応答の検証失敗時は再分割・要確認への記録を行う。ログの「統合キーワードへの案件照合」は今回送信する件数を示し、対象案件数の減少を意味しない。

照合用の案件番号とキーワード番号は実行内の整数連番を使う。入力は `{"i":0,"u":[{"t":"Python実務","k":["python"]}]}`、辞書は `[{"i":0,"n":"python"}]`、応答は `{"r":[{"i":0,"m":[[0]]}]}` の形式。`m` は入力の要件順で、対応するキーワード番号の配列を返す。一致なしは空配列とし、要件を省略しない。

`keyword_matches.json` は短い形式のレコード配列を保存し、そのまま採点に使う。長い案件ID・キーワードIDや `requirement`・`keywords` キーへ戻す処理は行わない。`keyword_projects.json` に案件番号と元メール・案件の対応、`integrated_skills.json` にキーワード番号・名称・キャリア情報の対応を保存する。要件数・案件番号の網羅性・辞書番号の範囲を検証する。新形式では旧形式の照合キャッシュを再利用せず、プロフィール抽出と元のメール分析は再利用できる。

`--publish` を指定すると、独立した名前のHTMLレポートをSharePointの `reports/` へ保存し、Teamsへリンクを通知する。GHA用ワークフローはこのオプションを有効にする。通常実行のチェックポイントと共有マッチングキャッシュは更新しない。通知に失敗した場合は、保存されたレポートがSharePointに残ることがある。再実行では別のレポートを作成する。

ローカル用のAzure CLI認証には、対象フォルダを読み取れるMicrosoft Graphの権限が必要。公開時は書き込み権限も必要。GHAでは既存ワークフローと同じOIDC接続を使用する。ローカルは既定ではレポート公開・Teams通知を実行しない。

## Azure CLIを起動できない場合

Windowsで `[WinError 2]` が発生し、SharePointへのエラーログ保存も失敗する場合は、Azure CLIが実行元のPATHにあるか確認する。インストールやPATH変更後は、ターミナルや実行元アプリを再起動する。

```powershell
Get-Command az
az --version
az login --tenant "テナントID" --allow-no-subscriptions
```

`Get-Command az` で見つからない場合はAzure CLIをインストールし、再起動後に確認する。Windowsの `az.cmd` も自動的に検索して使用する。Azure CLIが起動できない間はSharePointへのログ保存もできないため、ローカルの `.gha-state/error-*.log`（`--log-file` 指定時はそのログと同じフォルダ）を確認する。
