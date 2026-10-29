"""Restart saved mail analysis from a selected stage without fetching emails again."""
import argparse
import copy
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from uuid import uuid4

import mail_analyzer as app
import sharepoint_run
import scheduled_run
import run_progress


START_STEPS = {
    'screening': (),
    'extraction': (),
    'aggregation': (),
    'profiles': (),
    'matching': ('career_facts',),
    'scoring': ('career_facts', 'shared_projects'),
}


def copy_previous_steps(source, work, steps):
    """Import only validated-by-identity record caches needed before the start point."""
    cache = source.parent / '.analysis_cache' / 'token-files-v1'
    for step in steps:
        for path in (cache / step).glob('*.json'):
            if re.fullmatch(r'[a-f0-9]{64}\.json', path.name):
                destination = work / '.analysis_cache' / 'token-files-v1' / step / path.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, destination)


def restore_run(store, run_id, work):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', run_id):
        raise app.ToolError('一時ファイルIDの形式が不正です。')
    remote = 'temporary-runs/' + run_id
    manifest = store.get_json(remote + '/progress.json')
    archive = manifest.get('archive', '')
    if not re.fullmatch(r'snapshot-[a-f0-9]{32}\.zip', archive):
        raise app.ToolError('保存済みスナップショット名が不正です。')
    relative = manifest.get('output')
    if not isinstance(relative, str) or not relative:
        raise app.ToolError('再マッチングに使えるメール分析結果がありません。')
    source = (work / relative).resolve()
    if not source.is_relative_to(work.resolve()):
        raise app.ToolError('保存済み出力先が不正です。')
    run_progress.report('restoring')
    sharepoint_run.restore_archive(store.request('GET', remote + '/' + archive), work)
    return source


def download_settings(store, config_path):
    config = app.load_config(config_path, load_priority_rules=False)
    for key, name in [('engineers_file', 'engineers.md'), ('priority_rules_file', 'priority_rules.md')]:
        if key not in config['analysis']:
            raise app.ToolError('再マッチングには analysis.' + key + ' が必要です。')
        destination = Path(config['analysis'][key])
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(store.request('GET', name))


def rematch(config_path, source, *, source_id=None, log_file=None, start_from='profiles'):
    if start_from not in START_STEPS:
        raise app.ToolError('再開地点は ' + ' / '.join(START_STEPS) + ' から選択してください。')
    config_path, source = Path(config_path).resolve(), Path(source).resolve()
    config = app.load_config(config_path)
    app.configure_logging(config, log_file)
    if 'engineers_file' not in config['analysis']:
        raise app.ToolError('再マッチングには analysis.engineers_file が必要です。')
    metadata = app.read_metadata(source / 'metadata.json')
    app.saved_period(metadata)
    if start_from not in ('screening', 'extraction'):
        saved_rules = app.read_metadata(source / 'priority_rules.json')
        if saved_rules.get('ses', {}).get('criteria') != config['priority_rules']['ses']['criteria']:
            raise app.ToolError('SES共通条件が元の分析と異なります。元と同じ条件を設定してください。条件の再判定には --start-from extraction を指定してください。')
    if metadata.get('count'):
        if start_from in ('screening', 'extraction'):
            required = ('emails.jsonl',)
        else:
            complete = app.read_metadata(source / 'extraction_complete.json')
            if (complete.get('pipeline_version') != app.analysis_pipeline.VERSION
                    or metadata.get('pipeline_version') != app.analysis_pipeline.VERSION):
                raise app.ToolError('保存済みメール分析のバージョンが異なります。')
            required = ('emails.jsonl', 'extracted_emails.jsonl', 'extraction_complete.json')
            if start_from != 'aggregation':
                required += ('opportunities.jsonl', 'statistics.json')
        if start_from in ('extraction', 'aggregation'):
            required += ('title_selection.json',)
    else:
        if metadata.get('status') != 'complete' or metadata.get('count') != 0:
            raise app.ToolError('完了したメール分析データがありません。')
        required = ('emails.jsonl',)
    if any(not (source / name).is_file() for name in required):
        raise app.ToolError('再マッチングに必要な分析済みファイルが不足しています。')
    if (start_from in ('profiles', 'matching', 'scoring')
            and (source / 'title_selection.json').is_file()):
        required += ('title_selection.json',)
    key = app.secret(config['gemini'], 'api_key')
    root = Path(config['analysis']['output_dir'])
    if not root.is_absolute():
        root = config_path.parent / root
    # Each re-match owns its cache; only steps before the chosen start are imported.
    work = root / ('rematch_' + app.datetime.now(app.JST).strftime('%Y%m%d_%H%M%S') + '_' + uuid4().hex[:8])
    output = work / 'result'
    output.mkdir(parents=True, exist_ok=False)
    sharepoint_run.snapshot_settings(config_path, work)
    config = app.load_config(work / 'config.toml')
    copy_previous_steps(source, work, START_STEPS[start_from])
    for name in required:
        shutil.copyfile(source / name, output / name)
    metadata = copy.deepcopy(metadata)
    metadata.update(status='rematching', incremental_run=False, rematch_source=source_id or str(source),
                    rematched_at=app.datetime.now(app.JST).isoformat(), model=config['gemini']['model'],
                    rematch_start_from=start_from)
    metadata.pop('integrated_matching', None)
    app.write_json(output / 'metadata.json', metadata)
    try:
        if metadata['count']:
            original = copy.deepcopy(metadata)
            pipeline = app.analysis_pipeline.Pipeline(app, config, key, output / 'emails.jsonl', output, metadata)
            pipeline.required_cached_steps = set(START_STEPS[start_from])
            # Constructor initializes extraction counters; preserve the saved analysis counts.
            for name in ('extracted_unique_count', 'skipped_unique_count', 'skipped_emails', 'extraction_chunks'):
                if name in original:
                    metadata[name] = original[name]
            if start_from in ('screening', 'extraction', 'aggregation'):
                summary = pipeline.run()
            else:
                stats = app.read_metadata(output / 'statistics.json')
                summary = pipeline.report(output / 'opportunities.jsonl', stats)
            app.write_report(output, metadata, summary)
        else:
            from types import SimpleNamespace
            app.finish_analysis(config, key, output / 'emails.jsonl', output, metadata, None,
                                app.datetime.fromisoformat(metadata['stop_at']), SimpleNamespace(fetch_only=False))
    except Exception:
        metadata['status'] = 'rematch_failed'
        app.write_json(output / 'metadata.json', metadata)
        raise
    return output


def publish(store, output, result_id):
    """Publish a separate report; never advance the delivery checkpoint."""
    config_path = output.parent / 'config.toml'
    config = app.load_config(config_path)
    webhook = os.environ.get('TEAMS_WEBHOOK_URL', '').strip()
    scheduled_run.validate_report_url(webhook)
    run_progress.report('publishing')
    sharepoint_run.sharepoint_reports.ensure_reports_folder(os.environ['DRIVE_ID'], os.environ['FOLDER_ID'])
    item = store.put('reports/rematch-' + result_id + '.html', (output / 'report.html').read_bytes())
    scheduled_run.validate_report_url(item['webUrl'])
    run_progress.report('notifying')
    scheduled_run.post_report(item['webUrl'], webhook, config['teams']['timeout_seconds'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--run-id', help='SharePoint temporary-runs のID。プロフィール・ルールはSharePointの最新を取得（完了済みも可）')
    source.add_argument('--source-dir', type=Path, help='ローカルの分析結果フォルダ')
    parser.add_argument('--config', type=Path, default=Path('config.toml'))
    parser.add_argument('--start-from', choices=START_STEPS, default='profiles',
                        help='再開地点: screening=件名選別、extraction=メール抽出、aggregation=案件集計、'
                             'profiles=プロフィール構造化（既定）、matching=案件照合、scoring=採点・レポート')
    parser.add_argument('--sharepoint-settings', action='store_true', help='最新のプロフィール・ルールをSharePointから取得')
    parser.add_argument('--publish', action='store_true', help='別レポートをSharePointへ保存しTeamsへ通知')
    parser.add_argument('--progress-fd', type=int)
    parser.add_argument('--log-file', type=Path)
    args = parser.parse_args()
    app.configure_logging({}, args.log_file)
    store, result_id = None, uuid4().hex
    if os.environ.get('GITHUB_RUN_ID') and os.environ.get('GITHUB_RUN_ATTEMPT'):
        result_id = os.environ['GITHUB_RUN_ID'] + '-' + os.environ['GITHUB_RUN_ATTEMPT']
    try:
        if args.progress_fd is not None:
            run_progress.stream = os.fdopen(os.dup(args.progress_fd), 'w', encoding='utf-8')
        # Load .env before constructing the SharePoint store, without requiring local profiles.
        app.load_config(args.config.resolve(), load_priority_rules=False)
        if args.run_id or args.sharepoint_settings or args.publish:
            store = sharepoint_run.Store()
        if args.run_id or args.sharepoint_settings:
            download_settings(store, args.config.resolve())
        with tempfile.TemporaryDirectory(prefix='mail-rematch-') as temporary:
            source_dir = (restore_run(store, args.run_id, Path(temporary))
                          if args.run_id else args.source_dir)
            output = rematch(args.config, source_dir, source_id=args.run_id, log_file=args.log_file,
                             start_from=args.start_from)
        if args.publish:
            publish(store, output, result_id)
        app.LOGGER.info('再マッチング完了: %s', output)
        run_progress.report('rematch_complete')
        return 0
    except Exception as error:
        if store is not None:
            try:
                sharepoint_run.save_failure(store, 'rematch-' + result_id, error, args.log_file)
                run_progress.report('error_saved')
            except Exception:
                run_progress.report('error_save_failed')
        app.LOGGER.error('再マッチングに失敗しました: %s', sharepoint_run.redact_credentials(str(error)))
        return 1
    finally:
        if args.progress_fd is not None and run_progress.stream is not None:
            run_progress.stream.close()
            run_progress.stream = None


if __name__ == '__main__':
    sys.exit(main())
