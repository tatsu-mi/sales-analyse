"""Public progress contains fixed messages and numeric counts only."""

stream = None

MESSAGES = {
    'starting': '実行条件と保存先を準備しています。',
    'restoring': 'SharePointから前回の処理状況を復元しています。',
    'fetching': 'メール取得中です。',
    'mail_search': '取得対象のメールを検索しています。',
    'mail_scan': 'メール確認中：{done:,} / {total:,}件（取得済み {fetched:,}通）',
    'fetched_count': 'メール取得中：{count:,}通を保存しました。',
    'fetch_complete': 'メール取得完了：{count:,}通。',
    'mails_reused': '保存済みメールを使用します。',
    'titles_start': 'タイトル絞り込み中です。',
    'titles_reused': '保存済みのタイトル絞り込み結果を使用します。',
    'titles_progress': 'タイトル絞り込み：{done:,} / {total:,}通確認済み。',
    'titles_complete': 'タイトル絞り込み完了：本文分析の対象は{count:,}通です。',
    'extracting': 'メール分析：{done:,} / {total:,}通完了（スキップ {skipped:,}通）。',
    'extract_start': 'メール分析を開始します：対象 {total:,}通。',
    'extract_preparing': 'メール分析：{done:,} / {total:,}通完了。今回の{count:,}通の送信を準備しています。',
    'extract_request': 'メール分析：{done:,} / {total:,}通完了。今回の{count:,}通をAIへ送信（入力 {tokens:,}トークン、試行 {attempt:,}回目）。',
    'extract_cached': 'メール分析：{done:,} / {total:,}通完了。今回の{count:,}通は保存済みの分析結果を使用します。',
    'extract_saved': 'メール分析：{done:,} / {total:,}通完了（スキップ {skipped:,}通）。今回の{count:,}通の分析結果を保存しました。',
    'extract_split_output': 'メール分析：{done:,} / {total:,}通完了。入出力のトークン上限に達したため、今回の{count:,}通を分割して再分析します。',
    'extract_split_invalid': 'メール分析：{done:,} / {total:,}通完了。構造化結果の形式・根拠を検証できないため、今回の{count:,}通を分割して再分析します。',
    'extract_skip_output': 'メール分析：{done:,} / {total:,}通完了（スキップ {skipped:,}通）。1通でもトークン上限を超えたためスキップし、残りを続行します。',
    'extract_skip_invalid': 'メール分析：{done:,} / {total:,}通完了（スキップ {skipped:,}通）。1通の構造化結果を検証できないためスキップし、残りを続行します。',
    'extract_diagnostic_saved': '抽出失敗の診断情報を途中保存しました。',
    'skills_start': '全員分のスキル情報を整理しています（{count:,}人）。',
    'shared_matching': '統合キーワードへ案件を照合しています（{count:,}案件）。',
    'shared_matching_progress': '統合キーワードへの案件照合：{done:,} / {total:,}案件処理済み（照合失敗による保留 {failed:,}案件）。',
    'engineer_start': '個人別の条件確認・得点計算：{index:,} / {total:,}人目（{count:,}案件）。',
    'engineer_reused': '人物ごとの案件照合：{index:,} / {total:,}人目は保存済みです。',
    'engineer_complete': '個人別の条件確認・得点計算：{index:,} / {total:,}人完了。',
    'reporting': '照合結果をまとめてレポートを作成しています。',
    'saving': 'SharePointへ処理状況を一時保存しています。',
    'saved': 'SharePointへの一時保存が完了しました。',
    'publishing': 'SharePointへレポートを保存しています。',
    'notifying': 'Teamsへレポートのリンクを通知しています。',
    'committing': 'SharePointの前回実行時間を更新しています。',
    'waiting': 'APIの利用制限に合わせて約{seconds:,}秒待機します。',
    'complete': 'レポートの保存・通知と前回実行時間の更新が完了しました。',
    'error_saved': '処理に失敗しました。詳細エラーログをSharePointの実行用一時フォルダへ保存しました。',
    'error_save_failed': '処理に失敗しました。SharePointへのエラーログ保存にも失敗しました。',
    'checkpoint_save_failed': '最新の分析途中結果をSharePointへ保存できませんでした。前回保存分から再開します。',
}


def report(event, **counts):
    if stream is None:
        return
    if any(type(value) is not int or value < 0 for value in counts.values()):
        raise ValueError('Progress accepts nonnegative integer counts only')
    print('[進捗] ' + MESSAGES[event].format(**counts), file=stream, flush=True)
