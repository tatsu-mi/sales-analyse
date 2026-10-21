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
