"""Resolve or create the reports folder using the workflow's Azure CLI login."""
import json
import os
import shutil
import subprocess
import sys
from urllib.parse import quote


def run_az(arguments):
    """Resolve Azure CLI, including the Windows az.cmd launcher, on PATH."""
    executable = shutil.which('az')
    if executable is None:
        raise ValueError(
            'Azure CLI (az) が見つかりません。Azure CLIをインストールし、'
            'ターミナルや実行元アプリを再起動して、az --version を確認してください。')
    try:
        return subprocess.run([executable, *arguments], capture_output=True, text=True, check=True)
    except FileNotFoundError as error:
        raise ValueError(
            'Azure CLIを起動できません。インストール先とPATHを確認し、'
            '実行元アプリを再起動して、az --version を確認してください。') from error


def graph(method, url, body=None):
    command = ['rest', '--method', method, '--url', url,
               '--resource', 'https://graph.microsoft.com/', '--output', 'json']
    if body is not None:
        command += ['--body', json.dumps(body)]
    result = run_az(command)
    return json.loads(result.stdout)


def ensure_reports_folder(drive_id, parent_id):
    if not drive_id.strip() or not parent_id.strip():
        raise ValueError('SharePointのドライブID・対象フォルダIDを設定してください。')
    parent = ('https://graph.microsoft.com/v1.0/drives/' + quote(drive_id, safe='')
              + '/items/' + quote(parent_id, safe=''))
    try:
        item = graph('GET', parent + ':/reports')
    except subprocess.CalledProcessError:
        try:
            item = graph('POST', parent + '/children', {
                'name': 'reports', 'folder': {}, '@microsoft.graph.conflictBehavior': 'fail'})
        except subprocess.CalledProcessError:
            # A concurrent workflow may have created the same folder.
            item = graph('GET', parent + ':/reports')
    if (not isinstance(item, dict) or item.get('name') != 'reports'
            or not isinstance(item.get('folder'), dict)
            or not isinstance(item.get('id'), str) or not item['id'].strip()):
        raise ValueError('対象フォルダ内のreportsが有効なフォルダではありません。')
    return item['id']


if __name__ == '__main__':
    try:
        print(ensure_reports_folder(os.environ.get('DRIVE_ID', ''),
                                    os.environ.get('FOLDER_ID', '')))
    except (OSError, ValueError, subprocess.CalledProcessError):
        print('reportsフォルダを確認・作成できません。SharePointの設定と書き込み権限を確認してください。',
              file=sys.stderr)
        sys.exit(1)
