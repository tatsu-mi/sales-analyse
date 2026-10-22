import copy
import json
import unittest
from unittest.mock import patch

import analysis_pipeline as pipeline
import integrated_matching as matching
import scheduled_run
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
        if schema not in (matching.PROFILE_SCHEMA, matching.PROJECT_SCHEMA):
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
            records = [self.project_transform(plan(item), item) for item in items]
            for record, item in zip(records, items):
                if item['project']['project_id'] == self.invalid_project:
                    record['requirements'] = []
        return json.dumps(dict(records=records))

    def setup_projects(self, count=2, contract=False):
        self.h.extracted = {str(i): [harness.opportunity(project_id=f'P-{i}')] for i in range(count)}
        for projects in self.h.extracted.values():
            for check in projects[0]['assessments']:
                check.update(state='match', evidence='共通条件を確認')
        if contract:
            c = harness.opportunity(project_id='C-1')
            c.update(kind='contract', assessments=[dict(criterion=i + 1, state='match', evidence='開発相談')
                     for i in range(len(self.h.config['priority_rules']['contract']['criteria']))])
            self.h.extracted[str(count)] = [c]
        return [harness.mail(i, f'Python 出社不要 {i}') for i in range(count + int(contract))]

    def make(self, mails, profiles=None):
        instance = self.h.make(mails)
        instance.profiles = profiles if profiles is not None else [
            dict(file=name, name=name, sha256=name, text='Python', feedback='personal-feedback')
            for name in ('A', 'B')]
        return instance

    def test_each_project_matched_once_for_all_people_and_contract_is_independent(self):
        instance = self.make(self.setup_projects(15, contract=True))
        report = instance.run()
        self.assertEqual(len(self.profile_calls), 2)
        self.assertEqual(len(self.project_calls), 15)
        self.assertTrue(all('assessments' not in item['project'] for item in self.project_calls))
        ranked = json.loads((instance.output / 'ranked_emails.json').read_text(encoding='utf-8'))
        self.assertEqual([len(g['entries']) for g in ranked['ses_by_engineer']], [10, 10])
        self.assertEqual([r['uid'] for r in ranked['contract']], ['15'])
        self.assertFalse(ranked['ses'])
        self.assertIn('マッチング得点: 10点', report)
        audit = json.loads((instance.output / 'engineer_assessments.json').read_text(encoding='utf-8'))
        self.assertEqual(len(audit), 30)
        self.assertEqual(len({(r['career_id'], r['project_key']) for r in audit}), 30)
        self.assertEqual(ranked['ses_by_engineer'][0]['entries'][0]['source_email']['body'],
                         next(m['body'] for m in instance.api.iter_saved_mails(instance.source)
                              if m['uid'] == ranked['ses_by_engineer'][0]['entries'][0]['uid']))
        card = scheduled_run.card_payload('https://example.com/report', ranked, self.h.config['priority_rules']).decode()
        self.assertIn('A', card)
        self.assertIn('受託', card)

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
        self.assertEqual({k['name'] for k in keywords}, {'python', 'java'})

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
            self.assertEqual([e['uid'] for e in group['entries']], ['1'])
            self.assertEqual([e['uid'] for e in group['pending_entries']], ['0'])
        self.assertEqual([e['uid'] for e in ranked['contract']], ['2'])
        self.assertIn('案件の構造化照合を確認できません', report)
        self.invalid_project = None
        self.project_calls.clear()
        self.make(mails).run()
        self.assertEqual([item['project']['project_id'] for item in self.project_calls], ['P-0'])

    def test_shared_rules_filter_before_matching_and_unknowns_stay_pending(self):
        mails = self.setup_projects()
        self.h.extracted['0'][0]['assessments'][0].update(state='mismatch', evidence='共通条件に不足')
        self.h.extracted['1'][0]['assessments'][0].update(state='unknown', evidence='共通条件が不明')
        instance = self.make(mails)
        instance.run()
        self.assertEqual([p['project']['project_id'] for p in self.project_calls], ['P-1'])
        ranked = json.loads((instance.output / 'ranked_emails.json').read_text(encoding='utf-8'))
        for group in ranked['ses_by_engineer']:
            self.assertFalse(group['entries'])
            self.assertEqual([r['uid'] for r in group['pending_entries']], ['1'])

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
        self.assertFalse(group['entries'])
        self.assertEqual(len(group['pending_entries'][0]['engineer_match']['requirements']), 2)
        self.assertIn('英語', report)

    def test_output_truncation_splits_shared_batches_without_losing_people_or_projects(self):
        self.fail_batch = True
        instance = self.make(self.setup_projects(3))
        instance.run()
        ranked = json.loads((instance.output / 'ranked_emails.json').read_text(encoding='utf-8'))
        self.assertEqual([len(g['entries']) for g in ranked['ses_by_engineer']], [3, 3])

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
        self.assertEqual([e['uid'] for e in a['entries']], ['0'])
        self.assertEqual([e['uid'] for e in a['pending_entries']], ['1'])
        self.assertFalse(b['entries'])
        self.assertFalse(b['pending_entries'])

    def test_failed_person_extraction_stays_pending_and_does_not_affect_other_person(self):
        mails = self.setup_projects(1)
        def profiles(record, item):
            if item['id'] == matching.profile_id(dict(file='A')):
                record['facts'][0]['quote'] = '存在しない引用'
            return record
        self.profile_transform = profiles
        instance = self.make(mails)
        report = instance.run()
        ranked = json.loads((instance.output / 'ranked_emails.json').read_text(encoding='utf-8'))
        a, b = ranked['ses_by_engineer']
        self.assertFalse(a['entries'])
        self.assertEqual([e['uid'] for e in a['pending_entries']], ['0'])
        self.assertEqual([e['uid'] for e in b['entries']], ['0'])
        self.assertIn('キャリアシートの構造化情報を確認できません', report)


if __name__ == '__main__':
    unittest.main()
