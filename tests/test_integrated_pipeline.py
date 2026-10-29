import copy
import json
import unittest
from unittest.mock import patch

from sales_analyze import analysis_pipeline as pipeline
from sales_analyze import integrated_matching as matching
from sales_analyze import run_progress
from sales_analyze import scheduled_run
import test_analysis_pipeline as harness
from test_integrated_matching import career, expression, fact, plan, atom


class IntegratedPipelineTests(unittest.TestCase):
    def setUp(self):
        self.h = harness.PipelineTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.profile_calls, self.project_calls = [], []
        self.original_generate = self.h.api.generate
        self.h.api.generate = self.generate
        self.profile_transform = lambda record, item: record
        self.project_transform = lambda record, item: record
        self.fail_batch = False
        self.invalid_project = None

    def generate(self, *args, **kwargs):
        schema = kwargs.get('response_schema')
        if schema == pipeline.EXTRACTION_SCHEMA:
            # Conditions and raw-source checks now belong to the first analysis.
            items = [json.loads(line) for line in self.h.uploads[kwargs['file_data']['uri']].splitlines()]
            original = self.h.extracted
            self.h.extracted = copy.deepcopy(original)
            try:
                for item in items:
                    for project in self.h.extracted.get(item['uid'], []):
                        full = dict(id=item['uid'], project=project, source_body=item['body'])
                        record = plan(full)
                        record.update(copy.deepcopy(project['matching']))
                        for expr, req in zip(record['requirements'], project['mandatory_requirements']):
                            expr.update(root=req['root'], nodes=req['nodes'])
                        record = self.project_transform(record, full)
                        for req, expr in zip(project['mandatory_requirements'], record['requirements']):
                            req.update(root=expr['root'], nodes=expr['nodes'])
                        project['mandatory_requirements'].extend(
                            {k: r[k] for k in ('requirement', 'category', 'source_quote', 'root', 'nodes')}
                            for r in record['additional_requirements'])
                        project['matching'] = {k: record[k] for k in
                            ('preferences', 'complete', 'unassessed_requirements', 'commute')}
                return self.original_generate(*args, **kwargs)
            finally:
                self.h.extracted = original
        if schema not in (matching.PROFILE_SCHEMA, matching.KEYWORD_SCHEMA):
            return self.original_generate(*args, **kwargs)
        kwargs['before_request']()
        items = [json.loads(line) for line in self.h.uploads[kwargs['file_data']['uri']].splitlines()]
        if self.fail_batch and len(items) > 1:
            raise self.h.api.OutputLimitError('truncated')
        if schema == matching.PROFILE_SCHEMA:
            self.profile_calls.extend(items)
            records = [self.profile_transform(career(item), item) for item in items]
        else:
            self.project_calls.extend(items)
            # Individual information must never appear in a project request.
            self.assertNotIn('career_', args[2])
            self.assertNotIn('personal-feedback', args[2])
            dictionary = {entry['n']: entry['i'] for entry in json.loads(args[2].split('\nk: ', 1)[1])}
            records = [dict(i=item['i'], m=[
                [dictionary['python']] if 'python' in u['k'] else [] for u in item['u']]) for item in items]
            for record, item in zip(records, items):
                if self.invalid_project is not None:
                    projects = json.loads((self.instance.output / 'keyword_projects.json').read_text(encoding='utf-8'))
                    if projects[item['i']]['p'] == self.invalid_project:
                        record['m'] = []
        return json.dumps({'r' if schema == matching.KEYWORD_SCHEMA else 'records': records})

    def setup_projects(self, count=2, contract=False):
        self.h.extracted = {str(i): [harness.opportunity(project_id=f'P-{i}')] for i in range(count)}
        for projects in self.h.extracted.values():
            for check in projects[0]['assessments']:
                check.update(state='match', evidence='共通条件を確認')
        if contract:
            c = harness.opportunity(project_id='C-1')
            self.h.extracted[str(count)] = []
        return [harness.mail(i, f'Python 出社不要 {i}') for i in range(count + int(contract))]

    def make(self, mails, profiles=None):
        instance = self.h.make(mails)
        self.instance = instance
        instance.profiles = profiles if profiles is not None else [
            dict(file=name, name=name, sha256=name, text='Python', feedback='personal-feedback')
            for name in ('A', 'B')]
        return instance

    def test_each_ses_project_matched_once_for_all_people_and_contract_is_omitted(self):
        instance = self.make(self.setup_projects(15, contract=True))
        with patch.object(run_progress, 'report') as progress:
            report = instance.run()
        events = [call.args[0] for call in progress.call_args_list]
        self.assertLess(events.index('extract_complete'), events.index('shared_matching'))
        completed = next(call for call in progress.call_args_list if call.args[0] == 'extract_complete')
        self.assertEqual(completed.kwargs['done'], 16)
        self.assertEqual(completed.kwargs['total'], 16)
        self.assertEqual(completed.kwargs['analyzed'], 16)
        self.assertEqual(completed.kwargs['skipped'], 0)
        self.assertEqual(len(self.profile_calls), 2)
        self.assertEqual(len(self.project_calls), 15)
        self.assertTrue(all(set(item) == {'i', 'u'} for item in self.project_calls))
        ranked = json.loads((instance.output / 'ranked_emails.json').read_text(encoding='utf-8'))
        self.assertEqual([len(g['entries']) for g in ranked['ses_by_engineer']], [10, 10])
        self.assertNotIn('contract', ranked)
        self.assertFalse(ranked['ses'])
        self.assertIn('マッチング得点: 10点', report)
        audit = json.loads((instance.output / 'engineer_assessments.json').read_text(encoding='utf-8'))
        self.assertEqual(len(audit), 30)
        self.assertEqual(len({(r['career_id'], r['project_key']) for r in audit}), 30)
        self.assertIn('source_email', ranked['ses_by_engineer'][0]['entries'][0])
        card = scheduled_run.card_payload('https://example.com/report', ranked, self.h.config['priority_rules']).decode()
        self.assertIn('A', card)
        self.assertNotIn('受託', card)

    def test_first_analysis_structures_conditions_and_lookup_sends_no_body_or_evidence(self):
        mails = self.setup_projects(1)
        marker = '原文のみにある監査用マーカー'
        mails[0]['body'] += marker * 20
        self.h.extracted['0'][0]['mandatory_requirements'][0]['nodes'][0]['min_months'] = 36
        instance = self.make(mails)
        instance.run()
        rows = [json.loads(line) for line in (instance.output / 'extracted_emails.jsonl').read_text(
            encoding='utf-8').splitlines()]
        requirement = rows[0]['opportunities'][0]['mandatory_requirements'][0]
        self.assertEqual(requirement['nodes'][0]['min_months'], 36)
        self.assertNotIn(marker, pipeline.encoded(requirement))
        self.assertEqual(len(self.project_calls), 1)
        payload = self.project_calls[0]
        self.assertEqual(set(payload), {'i', 'u'})
        for unit in payload['u']:
            self.assertEqual(set(unit), {'t', 'k'})
        serialized = pipeline.encoded(payload)
        self.assertNotIn(marker, serialized)
        for forbidden in ('source_body', 'source_quote', 'nodes', 'commute', 'assessments'):
            self.assertNotIn(forbidden, serialized)
        self.assertLess(len(serialized), len(pipeline.encoded(mails[0])))
        plans = json.loads((instance.output / 'project_matching.json').read_text(encoding='utf-8'))
        self.assertEqual(plans[0]['requirements'][0]['nodes'][0]['min_months'], 36)

    def test_quote_free_profile_cache_has_new_identity(self):
        instance = self.make(self.setup_projects(1))
        item = dict(id=matching.profile_id(instance.profiles[0]), text=instance.profiles[0]['text'])
        expected = pipeline.digest(pipeline.encoded(dict(version=7, system=instance.api.SYSTEM,
            text=pipeline.encoded(item), prompt=matching.PROFILE_PROMPT,
            model=instance.settings['model'], output=instance.settings['max_output_tokens'],
            thinking=instance.settings.get('thinking_level'), schema=matching.PROFILE_SCHEMA)))
        self.assertEqual(instance.identity(pipeline.encoded(item), matching.PROFILE_PROMPT,
                                         instance.settings, matching.PROFILE_SCHEMA), expected)

    def test_compact_matches_are_saved_and_used_without_expanding_ids_or_keys(self):
        instance = self.make(self.setup_projects(1))
        instance.run()
        links = json.loads((instance.output / 'keyword_matches.json').read_text(encoding='utf-8'))
        self.assertEqual(links, [dict(i=0, m=[[0]])])
        keywords = json.loads((instance.output / 'integrated_skills.json').read_text(encoding='utf-8'))
        self.assertEqual([(entry['i'], entry['n']) for entry in keywords], [(0, 'python')])
        projects = json.loads((instance.output / 'keyword_projects.json').read_text(encoding='utf-8'))
        self.assertEqual((projects[0]['i'], projects[0]['u'], projects[0]['p']), (0, '0', 'P-0'))
        plans = json.loads((instance.output / 'project_matching.json').read_text(encoding='utf-8'))
        self.assertEqual(plans[0]['requirements'][0]['keywords'], [0])
        ranked = json.loads((instance.output / 'ranked_emails.json').read_text(encoding='utf-8'))
        score = ranked['ses_by_engineer'][0]['entries'][0]['engineer_match']
        self.assertEqual(score['score'], 10)
        self.assertEqual(score['score_breakdown'][0]['keyword_names'], ['python'])

    def test_preferences_extracted_upfront_keep_nonprofessional_weight(self):
        mails = self.setup_projects(1)
        mails[0]['body'] += ' Java'
        self.h.extracted['0'][0]['matching']['preferences'] = [dict(
            id='welcome-java', kind='welcome', requirement='Java', terms=['Java'], quote='Java')]
        self.profile_transform = lambda record, item: dict(record, facts=[fact(), fact(
            ('Java',), experience='non_professional', quote='Java')])
        instance = self.make(mails, profiles=[dict(file='A', sha256='A', text='Python Java')])
        instance.run()
        ranked = json.loads((instance.output / 'ranked_emails.json').read_text(encoding='utf-8'))
        score = ranked['ses_by_engineer'][0]['entries'][0]['engineer_match']
        self.assertEqual(score['score'], 10.5)
        self.assertEqual(score['score_breakdown'][1]['factor'], 0.1)
        self.assertEqual([u['k'] for u in self.project_calls[0]['u']], [['python'], ['Java']])

    def test_changed_person_and_weights_reuse_shared_matches_across_runs(self):
        mails = self.setup_projects()
        first = self.make(mails)
        first.run()
        self.profile_calls.clear()
        self.project_calls.clear()
        second = self.make(mails)
        second.run()
        self.assertFalse(self.profile_calls)
        self.assertFalse(self.project_calls)
        changed = self.make(mails)
        changed.profiles[0].update(text='Python 5年', sha256='changed')
        changed.run()
        self.assertEqual(len(self.profile_calls), 1)
        self.assertFalse(self.project_calls)
        self.profile_calls.clear()
        self.h.config['analysis']['matching_mandatory_weight'] = 20
        weighted = self.make(mails)
        weighted.run()
        self.assertFalse(self.profile_calls)
        self.assertFalse(self.project_calls)
        ranked = json.loads((weighted.output / 'ranked_emails.json').read_text(encoding='utf-8'))
        self.assertEqual(ranked['ses_by_engineer'][0]['entries'][0]['engineer_match']['score'], 20)

    def test_new_dictionary_keyword_rematches_existing_projects(self):
        mails = self.setup_projects()
        self.make(mails).run()
        self.project_calls.clear()
        self.profile_transform = lambda record, item: dict(record, facts=[fact(), fact(('Java',), quote='Java')])
        instance = self.make(mails, profiles=[dict(file='C', sha256='C', text='Python Java')])
        instance.run()
        self.assertEqual(len(self.project_calls), 2)
        keywords = json.loads((instance.output / 'integrated_skills.json').read_text(encoding='utf-8'))
        self.assertEqual({k['n'] for k in keywords}, {'python', 'java'})

    def test_resume_reuses_extraction_and_shared_matching_after_first_person(self):
        instance = self.make(self.setup_projects())
        def interrupt(output, stage):
            if stage == 'engineer_completed':
                raise self.h.api.ToolError('interrupted after durable save')
        instance.progress = interrupt
        with self.assertRaisesRegex(self.h.api.ToolError, 'interrupted'):
            instance.run()
        self.profile_calls.clear()
        self.project_calls.clear()
        instance.progress = None
        with patch.object(instance, 'extract', side_effect=AssertionError('extraction repeated')):
            instance.run()
        self.assertFalse(self.profile_calls)
        self.assertFalse(self.project_calls)
        audit = json.loads((instance.output / 'engineer_assessments.json').read_text(encoding='utf-8'))
        self.assertEqual(len(audit), 4)

    def test_invalid_shared_record_is_pending_for_all_and_retried_next_run(self):
        mails = self.setup_projects(contract=True)
        self.invalid_project = 'P-0'
        instance = self.make(mails)
        report = instance.run()
        ranked = json.loads((instance.output / 'ranked_emails.json').read_text(encoding='utf-8'))
        for group in ranked['ses_by_engineer']:
            self.assertEqual([e['uid'] for e in group['entries']], ['1', '0'])
            self.assertEqual([e['verification'] for e in group['entries']], ['適合', '要確認'])
        self.assertNotIn('contract', ranked)
        self.assertIn('案件のキーワード照合を確認できません', report)
        self.invalid_project = None
        self.project_calls.clear()
        self.make(mails).run()
        self.assertEqual([item['i'] for item in self.project_calls], [0])

    def test_shared_rules_filter_before_matching_and_unknowns_stay_pending(self):
        mails = self.setup_projects()
        self.h.extracted['0'][0]['assessments'][0].update(state='mismatch', evidence='共通条件に不足')
        self.h.extracted['1'][0]['assessments'][0].update(state='unknown', evidence='共通条件が不明')
        instance = self.make(mails)
        instance.run()
        self.assertEqual([p['i'] for p in self.project_calls], [0])
        ranked = json.loads((instance.output / 'ranked_emails.json').read_text(encoding='utf-8'))
        for group in ranked['ses_by_engineer']:
            self.assertEqual([r['uid'] for r in group['entries']], ['1'])
            self.assertEqual(group['entries'][0]['verification'], '要確認')

    def test_all_candidates_checked_before_top10_and_explicit_shortage_excluded(self):
        mails = self.setup_projects(15)
        def transform(record, item):
            if item['project']['project_id'] != 'P-0':
                record['requirements'][0] = expression(atom(months=60))
            return record
        self.project_transform = transform
        instance = self.make(mails)
        instance.run()
        self.assertEqual(len(self.project_calls), 15)
        ranked = json.loads((instance.output / 'ranked_emails.json').read_text(encoding='utf-8'))
        self.assertEqual([[e['uid'] for e in g['entries']] for g in ranked['ses_by_engineer']], [['0'], ['0']])
        audit = json.loads((instance.output / 'engineer_assessments.json').read_text(encoding='utf-8'))
        self.assertEqual(sum(r['state'] == 'mismatch' for r in audit), 28)

    def test_added_requirement_is_checked_and_shown_in_report(self):
        mails = self.setup_projects(1)
        def transform(record, item):
            record['additional_requirements'] = [dict(expression(atom(('英語',))),
                requirement='英語', category='skill', source_quote='Python', keywords=[])]
            return record
        self.project_transform = transform
        instance = self.make(mails)
        report = instance.run()
        ranked = json.loads((instance.output / 'ranked_emails.json').read_text(encoding='utf-8'))
        group = ranked['ses_by_engineer'][0]
        self.assertEqual(group['entries'][0]['verification'], '要確認')
        self.assertEqual(len(group['entries'][0]['engineer_match']['requirements']), 2)
        self.assertIn('英語', report)

    def test_output_truncation_splits_shared_batches_without_losing_people_or_projects(self):
        self.fail_batch = True
        instance = self.make(self.setup_projects(3))
        updates = []

        def progress(event, **counts):
            if event == 'shared_matching_progress':
                # Progress is emitted as each split finishes, before the next request.
                self.assertEqual(counts['done'], len(self.project_calls))
                updates.append(counts)

        with patch.object(run_progress, 'report', side_effect=progress):
            instance.run()
        self.assertEqual(updates, [dict(done=i, total=3, failed=0) for i in range(4)])
        ranked = json.loads((instance.output / 'ranked_emails.json').read_text(encoding='utf-8'))
        self.assertEqual([len(g['entries']) for g in ranked['ses_by_engineer']], [3, 3])

    def test_project_batches_can_exceed_ten_and_still_match_every_project(self):
        instance = self.make(self.setup_projects(156))
        instance.report_limit = 60000
        with patch.object(instance, 'request', wraps=instance.request) as request:
            instance.run()
        sizes = [len(call.args[0]) for call in request.call_args_list
                 if call.args[7] == matching.KEYWORD_SCHEMA]
        self.assertEqual(sizes, [64, 64, 28])
        self.assertEqual(sum(sizes), 156)
        self.assertEqual(len({item['i'] for item in self.project_calls}), 156)

    def test_split_does_not_change_size_of_later_batches(self):
        instance = self.make(self.setup_projects(128))
        instance.report_limit = 60000
        attempts = []
        original = instance.api.generate

        def generate(*args, **kwargs):
            if kwargs.get('response_schema') == matching.KEYWORD_SCHEMA:
                count = len(self.h.uploads[kwargs['file_data']['uri']].splitlines())
                attempts.append(count)
                if count > 32:
                    raise instance.api.OutputLimitError('truncated')
            return original(*args, **kwargs)

        with patch.object(instance.api, 'generate', side_effect=generate):
            instance.run()
        self.assertEqual(attempts, [64, 32, 32, 64, 32, 32])
        self.assertEqual(len({item['i'] for item in self.project_calls}), 128)

    def test_answer_cache_reuses_shared_batch_without_sending_again(self):
        instance = self.make(self.setup_projects(1))
        items = [dict(i=0, u=[])]
        validator = lambda answer, data: matching.validate_keyword_matches(answer, data, [])
        fallback = lambda item, error: self.fail('unexpected fallback: ' + error)
        for _ in range(2):
            instance.structured_parts(items, matching.PROJECT_PROMPT + '\nk: []', matching.KEYWORD_SCHEMA,
                                      validator, 'shared_projects', fallback)
        self.assertEqual(len(self.project_calls), 1)

    def test_keyword_groups_also_limit_requirements_and_respect_output_budget(self):
        instance = self.make(self.setup_projects(1))
        items = [dict(i=i, u=[{}] * 30) for i in range(5)]
        settings = dict(instance.settings, max_output_tokens=16384)
        groups = list(instance.groups(items, 'prompt', settings, instance.report_limit, matching.KEYWORD_SCHEMA))
        self.assertEqual([len(batch) for batch, _, _ in groups], [2, 2, 1])
        settings['max_output_tokens'] = 4096
        groups = list(instance.groups(items, 'prompt', settings, instance.report_limit, matching.KEYWORD_SCHEMA))
        self.assertEqual([len(batch) for batch, _, _ in groups], [1] * 5)

    def test_shared_progress_counts_cached_records_and_failed_pending_records(self):
        mails = self.setup_projects(3)
        self.invalid_project = 'P-0'
        with patch.object(run_progress, 'report') as report:
            self.make(mails).run()
        updates = [call.kwargs for call in report.call_args_list
                   if call.args == ('shared_matching_progress',)]
        self.assertEqual(updates[-1], dict(done=3, total=3, failed=1))
        self.assertEqual([u['done'] for u in updates], sorted({u['done'] for u in updates}))

        self.invalid_project = None
        self.project_calls.clear()
        with patch.object(run_progress, 'report') as report:
            self.make(mails).run()
        updates = [call.kwargs for call in report.call_args_list
                   if call.args == ('shared_matching_progress',)]
        self.assertEqual(updates, [dict(done=2, total=3, failed=0), dict(done=3, total=3, failed=0)])
        self.assertEqual(len(self.project_calls), 1)

    def test_person_specific_commute_uses_routes_and_requires_every_destination(self):
        mails = self.setup_projects(2)
        def profiles(record, item):
            minutes = 90 if item['id'] == matching.profile_id(dict(file='A')) else 91
            return dict(record, attributes=[dict(field='nearest_station', value='Python', quote='Python')],
                        routes=[dict(origin='Python', workplace='Python', minutes=minutes,
                                     route='Python', quote='Python')])
        def projects(record, item):
            record['commute'] = dict(mode='hybrid', workplaces=['Python'], quote='Python')
            if item['project']['project_id'] == 'P-1':
                record['commute']['workplaces'].append('unknown')
            return record
        self.profile_transform, self.project_transform = profiles, projects
        instance = self.make(mails)
        instance.run()
        ranked = json.loads((instance.output / 'ranked_emails.json').read_text(encoding='utf-8'))
        a, b = ranked['ses_by_engineer']
        self.assertEqual([e['uid'] for e in a['entries']], ['0', '1'])
        self.assertEqual([e['verification'] for e in a['entries']], ['適合', '要確認'])
        self.assertFalse(b['entries'])

    def test_failed_person_extraction_stays_pending_and_does_not_affect_other_person(self):
        mails = self.setup_projects(1)
        def profiles(record, item):
            if item['id'] == matching.profile_id(dict(file='A')):
                record['facts'][0]['experience'] = 'invalid'
            return record
        self.profile_transform = profiles
        instance = self.make(mails)
        report = instance.run()
        ranked = json.loads((instance.output / 'ranked_emails.json').read_text(encoding='utf-8'))
        a, b = ranked['ses_by_engineer']
        self.assertEqual([e['uid'] for e in a['entries']], ['0'])
        self.assertEqual(a['entries'][0]['verification'], '要確認')
        self.assertEqual([e['uid'] for e in b['entries']], ['0'])
        self.assertIn('キャリアシートの構造化情報を確認できません', report)


if __name__ == '__main__':
    unittest.main()
