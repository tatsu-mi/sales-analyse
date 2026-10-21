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
from urllib.error import HTTPError
from urllib.parse import quote
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
            raise app.ToolError('SharePoint state transfer failed.') from None

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
        scheduled_run.prepare(config, state_dir, publish, resume_dir=output, progress=save,
                              run_period=run_period)
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
    store.put('run-state/checkpoint.json', (state_dir / 'checkpoint.json').read_bytes())
    save(output, 'complete')
    run_progress.report('complete')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--resume', metavar='RUN_ID')
    parser.add_argument('--progress-fd', type=int, help='File descriptor for public numeric progress')
    args = parser.parse_args()
    try:
        if args.progress_fd is not None:
            run_progress.stream = os.fdopen(os.dup(args.progress_fd), 'w', encoding='utf-8')
        store = Store()
        work = Path('.gha-state/run').resolve()
        work.mkdir(parents=True, exist_ok=False)
        run_id = args.resume or (os.environ['GITHUB_RUN_ID'] + '-' + os.environ['GITHUB_RUN_ATTEMPT'])
        execute(store, work, run_id, resume=bool(args.resume))
        return 0
    except (app.ToolError, OSError, ValueError, KeyError, subprocess.CalledProcessError, zipfile.BadZipFile):
        print('SharePoint run failed. Resume using the saved run ID after checking settings and services.', file=sys.stderr)
        return 1
    finally:
        if args.progress_fd is not None and run_progress.stream is not None:
            run_progress.stream.close()
            run_progress.stream = None


if __name__ == '__main__':
    sys.exit(main())
