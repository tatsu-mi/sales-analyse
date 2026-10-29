# SharePoint・GHA接続設定

1. Entraのアプリ登録にMicrosoft Graphのアプリケーション権限 `Sites.Selected` を追加し、管理者同意を付与。
2. 対象SharePointサイトへのアプリの `write` 許可を管理者に依頼。
3. アプリのフェデレーション資格情報に、GitHubリポジトリのデフォルトブランチを登録。GitHub Environmentを使う場合はそのEnvironmentを登録。
4. GitHubの **Settings → Secrets and variables → Actions** に以下を設定。

| 登録先 | 名前 |
| --- | --- |
| Secrets | `SAKURA_MAIL_HOST`、`SAKURA_MAIL_USERNAME`、`SAKURA_MAIL_PASSWORD` |
| Secrets | `GEMINI_API_KEY`、`TEAMS_WEBHOOK_URL` |
| Secrets | `AZURE_CLIENT_ID`、`AZURE_TENANT_ID` |
| Variables | `SHAREPOINT_DRIVE_ID`、`SHAREPOINT_FOLDER_ID` |

5. 対象フォルダ直下にUTF-8の `engineers.md`・`priority_rules.md` を配置。
6. `reports/` にレポート閲覧者の閲覧権限を設定。一時データの `temporary-runs/` は運用担当者に閲覧を限定。
7. **SharePoint upload and Teams notification test** を実行し、Teamsのリンクからレポートを確認。

クライアントシークレット・AzureサブスクリプションIDは不要。GHAでは `.env` を作成しません。

## ダウンロード先で401になる場合

Graphのファイル取得が別ホストへ転送された後に401になる場合、Graphの `contentStream` による直接取得へ自動的に切り替えます。転送先URLに認証情報が含まれない環境での回避策です。Graph自体の401やアップロードには適用しません。

この回避策は [Microsoft Graph beta API](https://learn.microsoft.com/en-us/graph/api/driveitem-get-contentstream?view=graph-rest-beta) に依存します。beta APIは変更される可能性があり、Microsoftは本番利用をサポートしていません。
