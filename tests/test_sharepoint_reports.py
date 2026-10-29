import subprocess
import unittest
from unittest.mock import call, patch

import sharepoint_reports as reports


class ReportsFolderTests(unittest.TestCase):
    parent = 'https://graph.microsoft.com/v1.0/drives/drive/items/parent'
    folder = {'name': 'reports', 'id': 'reports-id', 'folder': {}}

    @patch.object(reports, 'graph')
    def test_existing_folder_is_reused(self, graph):
        graph.return_value = self.folder
        self.assertEqual(reports.ensure_reports_folder('drive', 'parent'), 'reports-id')
        graph.assert_called_once_with('GET', self.parent + ':/reports')

    @patch.object(reports, 'graph')
    def test_missing_folder_is_created_without_renaming(self, graph):
        graph.side_effect = [subprocess.CalledProcessError(1, 'az'), self.folder]
        self.assertEqual(reports.ensure_reports_folder('drive', 'parent'), 'reports-id')
        self.assertEqual(graph.call_args, call('POST', self.parent + '/children', {
            'name': 'reports', 'folder': {}, '@microsoft.graph.conflictBehavior': 'fail'}))

    @patch.object(reports, 'graph')
    def test_concurrent_creation_is_resolved(self, graph):
        graph.side_effect = [subprocess.CalledProcessError(1, 'az'),
                             subprocess.CalledProcessError(1, 'az'), self.folder]
        self.assertEqual(reports.ensure_reports_folder('drive', 'parent'), 'reports-id')
        self.assertEqual(graph.call_args, call('GET', self.parent + ':/reports'))

    @patch.object(reports, 'graph')
    def test_file_name_collision_or_wrong_folder_is_rejected(self, graph):
        for item in ({'name': 'reports', 'id': 'file-id', 'file': {}},
                     dict(self.folder, name='reports 1'), dict(self.folder, id=''), None):
            graph.return_value = item
            with self.subTest(item=item), self.assertRaises(ValueError):
                reports.ensure_reports_folder('drive', 'parent')

    @patch.object(reports, 'graph')
    def test_permission_failure_does_not_fall_back_to_parent(self, graph):
        graph.side_effect = subprocess.CalledProcessError(1, 'az')
        with self.assertRaises(subprocess.CalledProcessError):
            reports.ensure_reports_folder('drive', 'parent')

    @patch.object(reports, 'graph')
    def test_missing_configuration_fails_before_network(self, graph):
        with self.assertRaises(ValueError):
            reports.ensure_reports_folder('drive', ' ')
        graph.assert_not_called()


class AzureCliTests(unittest.TestCase):
    @patch.object(reports.shutil, 'which', return_value=r'C:\Program Files\Azure CLI\az.cmd')
    @patch.object(reports.subprocess, 'run')
    def test_graph_uses_resolved_windows_launcher_without_shell(self, run, which):
        run.return_value.stdout = '{"id": "folder-id"}'
        self.assertEqual(reports.graph('GET', 'https://graph.microsoft.com/test'), {'id': 'folder-id'})
        which.assert_called_once_with('az')
        run.assert_called_once_with([
            which.return_value, 'rest', '--method', 'GET', '--url', 'https://graph.microsoft.com/test',
            '--resource', 'https://graph.microsoft.com/', '--output', 'json'],
            capture_output=True, text=True, check=True)

    @patch.object(reports.shutil, 'which', return_value=None)
    @patch.object(reports.subprocess, 'run')
    def test_missing_cli_explains_how_to_fix_path(self, run, which):
        with self.assertRaisesRegex(ValueError, 'Azure CLI.*az --version'):
            reports.run_az(['account', 'show'])
        run.assert_not_called()

    @patch.object(reports.shutil, 'which', return_value='/usr/bin/az')
    @patch.object(reports.subprocess, 'run', side_effect=FileNotFoundError('missing launcher'))
    def test_removed_launcher_explains_how_to_fix_path(self, run, which):
        with self.assertRaisesRegex(ValueError, 'PATH'):
            reports.run_az(['account', 'show'])

    @patch.object(reports.shutil, 'which', return_value='/usr/bin/az')
    @patch.object(reports.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'az'))
    def test_authentication_failure_is_preserved(self, run, which):
        with self.assertRaises(subprocess.CalledProcessError):
            reports.run_az(['account', 'show'])
