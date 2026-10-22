"""Persist scheduled runs and delivery boundaries in SharePoint using Azure CLI login."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib
import traceback
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from uuid import uuid4
import zipfile

import mail_analyzer as app
import scheduled_run
import sharepoint_reports
import run_progress


class Store:
    def __init__(self):
        drive, folder = os.environ['DRIVE_ID'], os.environ['FOLDER_ID']
        if not drive.strip() or not folder.strip():
            raise ValueError('Missing SharePoint settings')
        self.base = ('https://graph.microsoft.com/v1.0/drives/' + quote(drive, safe='')
                     + '/items/' + quote(folder, safe=''))

    def request(self, method, path, data=None, missing=False):
        token = subprocess.run([
            'az', 'account', 'get-access-token', '--resource', 'https://graph.microsoft.com/',
            '--query', 'accessToken', '--output', 'tsv'], capture_output=True, text=True, check=True).stdout.strip()
        request = Request(self.base + ':/' + quote(path, safe='/') + ':/content',
                          data=data, method=method, headers={
                              'Authorization': 'Bearer ' + token,
                              'Content-Type': 'application/octet-stream'})
        try:
            with urlopen(request, timeout=180) as response:
                return response.read()
        except HTTPError as error:
            if missing and error.code == 404:
                return None
            raise app.ToolError(f'SharePoint state transfer failed (HTTP {error.code}, {method}).') from error

    def folder(self, parent, name):
        base = self.base if not parent else self.base + ':/' + quote(parent, safe='/')
        try:
            path = '/'.join(part for part in (parent, name) if part)
            item = sharepoint_reports.graph('GET', self.base + ':/' + quote(path, safe='/'))
        except subprocess.CalledProcessError:
            children = self.base + '/children' if not parent else base + ':/children'
            item = sharepoint_reports.graph('POST', children, {
                'name': name, 'folder': {}, '@microsoft.graph.conflictBehavior': 'fail'})
        if not isinstance(item.get('folder'), dict):
            raise ValueError('SharePoint state destination is not a folder')

    def put(self, path, data):
        if len(data) > 250_000_000:
            raise app.ToolError('SharePoint snapshot exceeds the 250 MB upload limit.')
        return json.loads(self.request('PUT', path, data))

    def get_json(self, path, missing=False):
        raw = self.request('GET', path, missing=missing)
        return None if raw is None else json.loads(raw)


def snapshot_settings(config_path, work):
    config = app.load_config(config_path)
    settings = tomllib.loads(config_path.read_text(encoding='utf-8'))
    for key, name in [('engineers_file', 'engineers.md'), ('priority_rules_file', 'priority_rules.md')]:
        if key in config['analysis']:
            shutil.copyfile(config['analysis'][key], work / name)
            settings['analysis'][key] = name
    settings['analysis']['output_dir'] = 'output'
    # Configuration validation permits scalar strings, integers and booleans only.
    # JSON scalar literals also represent these values in TOML.
    text = '\n\n'.join('[' + section + ']\n' + '\n'.join(
        key + ' = ' + json.dumps(value, ensure_ascii=False) for key, value in values.items())
        for section, values in settings.items()) + '\n'
    (work / 'config.toml').write_text(text, encoding='utf-8')
    app.load_config(work / 'config.toml')


def restore_archive(raw, work):
    from io import BytesIO
    with zipfile.ZipFile(BytesIO(raw)) as archive:
        for item in archive.infolist():
            target = (work / item.filename).resolve()
            if (not target.is_relative_to(work.resolve()) or '\\' in item.filename
                    or item.filename.startswith('/')):
                raise ValueError('Invalid snapshot path')
        archive.extractall(work)


def restore_matching_cache(raw, work):
    """Import only content-addressed matching records from the previous run."""
    from io import BytesIO
    cache = work / 'output' / '.analysis_cache' / 'token-files-v1'
    with zipfile.ZipFile(BytesIO(raw)) as archive:
        entries = archive.infolist()
        if sum(e.file_size for e in entries) > 250_000_000:
            raise ValueError('Matching cache exceeds the size limit')
        for entry in entries:
            if not re.fullmatch(r'(?:career_facts|shared_projects)/[a-f0-9]{64}\.json', entry.filename):
                raise ValueError('Invalid matching cache path')
        for entry in entries:
            target = cache / entry.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(entry))


def save_matching_cache(store, work):
    """Carry validated facts/plans across fresh scheduled runners, not just resume."""
    from io import BytesIO
    cache = work / 'output' / '.analysis_cache' / 'token-files-v1'
    paths = [path for folder in ('career_facts', 'shared_projects')
             for path in (cache / folder).glob('*.json')
             if re.fullmatch(r'[a-f0-9]{64}\.json', path.name)]
    if not paths:
        return
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(paths):
            archive.write(path, path.relative_to(cache).as_posix())
    store.put('run-state/matching-cache.zip', buffer.getvalue())


def redact_credentials(text):
    secrets = [value for name, value in os.environ.items() if value and (
        name in {'SAKURA_MAIL_USERNAME', 'TEAMS_WEBHOOK_URL'}
        or re.search(r'(?:PASSWORD|API_KEY|TOKEN|SECRET)$', name, re.IGNORECASE))]
    for secret in sorted(secrets, key=len, reverse=True):
        for value in (secret, repr(secret)[1:-1], json.dumps(secret, ensure_ascii=False)[1:-1]):
            text = text.replace(value, '[REDACTED]')
    text = re.sub(r'(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*', 'Bearer [REDACTED]', text)
    text = re.sub(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b', '[REDACTED JWT]', text)

    def redact_url(match):
        try:
            url = urlsplit(match[0])
            return urlunsplit((url.scheme, url.netloc.rsplit('@', 1)[-1], url.path,
                               '[REDACTED]' if url.query else '', ''))
        except ValueError:
            return '[REDACTED URL]'

    return re.sub(r'https?://[^\s<>"\']+', redact_url, text)


def save_failure(store, run_id, error, log_file=None):
    """Save diagnostics separately, leaving the last resumable snapshot untouched."""
    sys.stdout.flush()
    sys.stderr.flush()
    timestamp = app.datetime.now(app.JST).strftime('%Y%m%d_%H%M%S')
    filename = 'error-' + timestamp + '-' + uuid4().hex[:8] + '.log'
    details = ''
    if log_file is not None:
        try:
            details = log_file.read_text(encoding='utf-8', errors='replace')
        except OSError:
            details = 'The execution log could not be read.\n'
    details += '\nFailed at: ' + app.datetime.now(app.JST).isoformat() + '\n'
    details += ''.join(traceback.format_exception(error))
    current, seen = error, set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, subprocess.CalledProcessError) and current.stderr:
            stderr = current.stderr
            details += '\nSubprocess stderr:\n' + (
                stderr.decode('utf-8', errors='replace') if isinstance(stderr, bytes) else stderr)
        current = current.__cause__ or current.__context__
    payload = redact_credentials(details).encode('utf-8')
    destination = (log_file.parent if log_file is not None else Path('.gha-state')) / filename
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)
    if store is None or not run_id or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', run_id):
        raise ValueError('SharePoint is unavailable or the run ID is invalid')
    store.folder('', 'temporary-runs')
    store.folder('temporary-runs', run_id)
    store.put('temporary-runs/' + run_id + '/' + filename, payload)


def execute(store, work, run_id, resume=False):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', run_id):
        raise ValueError('Invalid run ID')
    remote = 'temporary-runs/' + run_id
    run_progress.report('restoring' if resume else 'starting')
    checkpoint = store.get_json('run-state/checkpoint.json', missing=True)
    if resume:
        manifest = store.get_json(remote + '/progress.json')
        if manifest.get('pipeline_version') != app.analysis_pipeline.VERSION:
            raise app.ToolError('Saved pipeline version differs from the current version.')
        if manifest['status'] == 'complete':
            raise app.ToolError('This run has already completed.')
        archive_name = manifest['archive']
        if not re.fullmatch(r'snapshot-[a-f0-9]{32}\.zip', archive_name):
            raise ValueError('Invalid snapshot name')
        restore_archive(store.request('GET', remote + '/' + archive_name), work)
        own_checkpoint = work / 'state/checkpoint.json'
        committed = (manifest['status'] == 'notified' and own_checkpoint.exists()
                     and app.read_metadata(own_checkpoint) == checkpoint)
        if manifest['previous_checkpoint'] != checkpoint and not committed:
            raise app.ToolError('A newer delivery boundary exists; this run cannot be resumed.')
    else:
        store.folder('', 'temporary-runs')
        store.folder('temporary-runs', run_id)
        snapshot_settings(Path('config.toml').resolve(), work)
        previous_cache = store.request('GET', 'run-state/matching-cache.zip', missing=True)
        if previous_cache is not None:
            restore_matching_cache(previous_cache, work)
        manifest = {'status': 'created', 'previous_checkpoint': checkpoint, 'output': None,
                    'pipeline_version': app.analysis_pipeline.VERSION}
        # Keep original conditions directly readable beside the snapshots.
        for name in ('config.toml', 'engineers.md', 'priority_rules.md'):
            if (work / name).exists():
                store.put(remote + '/' + name, (work / name).read_bytes())
    state_dir, publish = work / 'state', work / 'publish'
    if checkpoint is not None and manifest['status'] != 'notified':
        app.analysis_pipeline.atomic_json(state_dir / 'checkpoint.json', checkpoint)

    def save(output, stage):
        run_progress.report('saving')
        if output is not None:
            manifest['output'] = output.relative_to(work).as_posix()
            metadata = app.read_metadata(output / 'metadata.json')
            manifest['period'] = {key: metadata[key] for key in ('start_at', 'stop_at')}
        manifest['status'] = stage
        archive_name = 'snapshot-' + uuid4().hex + '.zip'
        archive_path = work.parent / archive_name
        try:
            with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED) as archive:
                for path in work.rglob('*'):
                    if path.is_file() and path.suffix != '.sqlite3' and path.suffix != '.log':
                        archive.write(path, path.relative_to(work).as_posix())
            store.put(remote + '/' + archive_name, archive_path.read_bytes())
            manifest['archive'] = archive_name
            store.put(remote + '/progress.json', app.analysis_pipeline.encoded(manifest).encode('utf-8'))
            run_progress.report('saved')
        finally:
            archive_path.unlink(missing_ok=True)

    if not resume:
        config = app.load_config(work / 'config.toml')
        start, stop = app.incremental_period(config['analysis'], state_dir / 'checkpoint.json',
                                             app.datetime.now(app.JST).replace(microsecond=0))
        manifest['period'] = {'start_at': start.isoformat(), 'stop_at': stop.isoformat()}
        save(None, 'created')
    config = work / 'config.toml'
    output = (work / manifest['output']).resolve() if manifest['output'] else None
    if output is not None and not output.is_relative_to(work.resolve()):
        raise ValueError('Invalid output path')
    if manifest['status'] not in ('prepared', 'report_uploaded', 'notified'):
        run_period = tuple(app.datetime.fromisoformat(manifest['period'][key])
                           for key in ('start_at', 'stop_at'))
        try:
            scheduled_run.prepare(config, state_dir, publish, resume_dir=output, progress=save,
                                  run_period=run_period)
        except Exception:
            # Each validated AI response is already cached locally. Upload those
            # responses on failure so resume can reuse them without new AI calls.
            try:
                save(None, manifest['status'])
            except Exception:
                run_progress.report('checkpoint_save_failed')
            raise
        save(output, 'prepared')
    if manifest['status'] == 'prepared':
        run_progress.report('publishing')
        sharepoint_reports.ensure_reports_folder(os.environ['DRIVE_ID'], os.environ['FOLDER_ID'])
        # Use the same authenticated binary transfer for reports.
        item = store.put('reports/report-' + run_id + '.md', (publish / 'report.md').read_bytes())
        manifest['report_url'] = item['webUrl']
        scheduled_run.validate_report_url(manifest['report_url'])
        save(output, 'report_uploaded')
    if manifest['status'] == 'report_uploaded':
        run_progress.report('notifying')
        scheduled_run.notify(config, state_dir, publish, manifest['report_url'], link_only=True)
        save(output, 'notified')
    run_progress.report('committing')
    store.folder('', 'run-state')
    save_matching_cache(store, work)
    store.put('run-state/checkpoint.json', (state_dir / 'checkpoint.json').read_bytes())
    save(output, 'complete')
    run_progress.report('complete')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--resume', metavar='RUN_ID')
    parser.add_argument('--progress-fd', type=int, help='File descriptor for public numeric progress')
    parser.add_argument('--log-file', type=Path, help='Detailed execution log to preserve on failure')
    args = parser.parse_args()
    store, run_id = None, None
    try:
        if args.progress_fd is not None:
            run_progress.stream = os.fdopen(os.dup(args.progress_fd), 'w', encoding='utf-8')
        run_id = args.resume or (os.environ['GITHUB_RUN_ID'] + '-' + os.environ['GITHUB_RUN_ATTEMPT'])
        store = Store()
        work = Path('.gha-state/run').resolve()
        work.mkdir(parents=True, exist_ok=False)
        execute(store, work, run_id, resume=bool(args.resume))
        return 0
    except Exception as error:
        try:
            save_failure(store, run_id, error, args.log_file)
            run_progress.report('error_saved')
        except Exception:
            # A secondary upload error must never replace the original failure.
            run_progress.report('error_save_failed')
        print('SharePoint run failed. Resume using the saved run ID after checking settings and services.', file=sys.stderr)
        return 1
    finally:
        if args.progress_fd is not None and run_progress.stream is not None:
            run_progress.stream.close()
            run_progress.stream = None


if __name__ == '__main__':
    sys.exit(main())
