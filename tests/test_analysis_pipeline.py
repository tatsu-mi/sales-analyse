import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import analysis_pipeline as pipeline
import mail_analyzer as app
import title_filter
import engineer_matching
import scheduled_run


def mail(uid, body="mail", received="2026-10-01T09:00:00+09:00"):
    return {"uid": str(uid), "from": "Sales <sales@example.invalid>", "subject": "project",
            "received_at": received, "body": body, "body_truncated": False}


def opportunity(budget="70万円/月", deadline="", status="open", project_id="P-1", notes=""):
    value = dict.fromkeys(pipeline.TEXT_FIELDS, "")
    value.update(title="Python development", project_id=project_id, budget=budget,
                 deadline_iso=deadline, status=status, skills=["Python"], notes=notes, kind="ses",
                 mandatory_requirements=[dict(requirement='Python実務', category='skill', source_quote='Python')],
                 assessments=[{"criterion": i, "state": "unknown", "evidence": ""}
                              for i in range(1, len(pipeline.prioritization.load_rules('priority_rules.toml')['ses']['criteria']) + 1)])
    return value


def checked_record(item, state='match', commute=None):
    return dict(id=item['id'], state=state, evidence='案件とプロフィールの照合',
                unassessed_requirements=[],
                requirements=[dict(criterion=index, state=state, project_quote=req['source_quote'],
                                   engineer_quote='Python', reason='実務経験を照合')
                              for index, req in enumerate(item.get('project', {}).get('mandatory_requirements', []), 1)],
                commute=commute or dict(mode='full_remote', nearest_station='', workplace='', minutes=-1,
                                       route='', evidence='出社不要'))


class PipelineTests(unittest.TestCase):
    def setUp(self):
        setting = patch.object(app.environment, "load_env")
        setting.start()
        self.addCleanup(setting.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = app.load_config(Path("config.example.toml"))
        self.config["analysis"].pop("engineers_file", None)
        self.config["priority_rules"]["prefilter"]["enabled"] = False
        self.config["analysis"].update(start_date="2026-09-27", end_date="2026-10-03",
                                       chunk_input_tokens=1024, report_input_tokens=1024)
        self.uploads, self.calls = {}, []
        self.extracted = {}
        self.screen_excluded = set()
        self.screen_calls = []
        self.fail_uid = None
        self.output_cutoff = None
        self.counter = 0
        self.api = SimpleNamespace(ToolError=app.ToolError, OutputLimitError=app.OutputLimitError,
                                   SYSTEM=app.SYSTEM, iter_saved_mails=app.iter_saved_mails,
                                   write_json=app.write_json,
                                   model_limits=lambda *_: {"inputTokenLimit": 1048576, "outputTokenLimit": 65536},
                                   count_input_tokens=self.count, upload_mail_file=self.upload,
                                   generate=self.generate)

    def count(self, settings, key, prompt, text=None, file_data=None, **kwargs):
        if text is None:
            text = self.uploads[file_data["uri"]]
        return 100 + len(text) // 2

    def upload(self, settings, key, path):
        uri = f"file-{len(self.uploads)}"
        self.uploads[uri] = path.read_text(encoding="utf-8")
        return {"uri": uri, "mimeType": "text/plain"}

    def generate(self, settings, key, prompt, file_data=None, response_schema=None, before_request=None):
        before_request()
        items = [json.loads(line) for line in self.uploads[file_data["uri"]].splitlines()]
        if response_schema == title_filter.SCHEMA:
            self.screen_calls.append(items)
            return json.dumps({"records": [{"uid": item["uid"], "exclude": item["uid"] in self.screen_excluded}
                                            for item in items]})
        self.calls.append(items)
        if response_schema:
            if self.fail_uid and any(item["uid"] == self.fail_uid for item in items):
                raise app.ToolError("transient failure")
            if self.output_cutoff and len(items) > self.output_cutoff:
                raise app.OutputLimitError("output truncated")
            for item in items:
                for project in self.extracted.get(item['uid'], []):
                    for req in project.get('mandatory_requirements', []):
                        req['source_quote'] = item['body']
            return json.dumps({"records": [{"uid": item["uid"], "kind": "ses", "tags": ["Python"],
                                             "market_notes": "Market observations",
                                             "opportunities": self.extracted.get(item["uid"], [])} for item in items]})
        return "Report: retained evidence UID 1"

    def make(self, mails):
        self.counter += 1
        output = self.root / f"run-{self.counter}"
        output.mkdir()
        source = output / "emails.jsonl"
        source.write_text("".join(pipeline.encoded(item) + "\n" for item in mails), encoding="utf-8")
        metadata = {"count": len(mails), "start_date": "2026-09-27", "end_date": "2026-10-03",
                    "truncated_bodies": 0}
        instance = pipeline.Pipeline(self.api, self.config, "key", source, output, metadata)
        instance.limiter.reserve = Mock()
        return instance

    def test_token_budget_splits_on_record_boundaries(self):
        instance = self.make([mail(1)])
        items = [mail(index, "x" * 1100) for index in range(6)]
        parts = list(instance.fit(items, "prompt", instance.settings, 1024))
        self.assertEqual([item["uid"] for part, _, _ in parts for item in part], [item["uid"] for item in items])
        self.assertTrue(all(tokens <= 1024 for _, _, tokens in parts))
        self.assertGreater(len(parts), 1)
        with self.assertRaises(app.ToolError):
            list(instance.fit([mail(7, "x" * 3000)], "prompt", instance.settings, 1024))

    def test_requirement_extraction_must_quote_the_original_mail(self):
        project = opportunity()
        record = dict(uid='1', kind='ses', tags=[], market_notes='', opportunities=[project])
        answer = lambda: json.dumps(dict(records=[record]))
        rules = self.config['priority_rules']
        self.assertEqual(len(pipeline.validate_extraction(answer(), [mail(1, 'Python実務必須')], rules)), 1)
        project['mandatory_requirements'][0]['source_quote'] = 'Java実務必須'
        with self.assertRaises(pipeline.InvalidExtraction):
            pipeline.validate_extraction(answer(), [mail(1, 'Python実務必須')], rules)
        del project['mandatory_requirements']
        with self.assertRaises(pipeline.InvalidExtraction):
            pipeline.validate_extraction(answer(), [mail(1, 'Python実務必須')], rules)

    def test_engineer_shortlists_check_all_candidates_and_keep_contract_independent(self):
        self.extracted = {str(i): [opportunity(project_id=f'P-{i}')] for i in range(15)}
        for projects in self.extracted.values():
            for check in projects[0]['assessments']:
                check.update(state='match', evidence='共通条件を確認')
        contract = opportunity(project_id='C-1')
        contract.update(kind='contract', assessments=[
            {'criterion': i + 1, 'state': 'match', 'evidence': '開発相談'}
            for i in range(len(self.config['priority_rules']['contract']['criteria']))])
        self.extracted['15'] = [contract]
        mails = [mail(i, str(i)) for i in range(16)]
        instance = self.make(mails)
        instance.profiles = [{'file': 'A.pdf', 'sha256': 'a', 'text': 'Python'},
                             {'file': 'B.pdf', 'sha256': 'b', 'text': 'Python Java'}]
        instance.profiles[0]['feedback'] = 'feedback-for-A'
        instance.profiles[1]['feedback'] = 'feedback-for-B'
        generate = instance.api.generate
        match_calls = []

        def matching(*args, **kwargs):
            if kwargs.get('response_schema') != engineer_matching.SCHEMA:
                return generate(*args, **kwargs)
            items = [json.loads(line) for line in self.uploads[kwargs['file_data']['uri']].splitlines()]
            match_calls.extend(items)
            engineer_a = 'A.pdf' in args[2]
            self.assertIn('feedback-for-A' if engineer_a else 'feedback-for-B', args[2])
            self.assertNotIn('feedback-for-B' if engineer_a else 'feedback-for-A', args[2])
            return json.dumps({'records': [checked_record(item,
                state=('match' if engineer_a or item['project']['project_id'] == 'P-0' else 'mismatch'),
                commute=dict(mode='full_remote', nearest_station='', workplace='', minutes=-1,
                             route='', evidence='出社不要のフルリモート')) for item in items]})

        with patch.object(instance.api, 'generate', side_effect=matching):
            report = instance.run()
        ranked = json.loads((instance.output / 'ranked_emails.json').read_text(encoding='utf-8'))
        self.assertEqual(len(match_calls), 30)
        first, second = ranked['ses_by_engineer']
        self.assertEqual(len(first['entries']), 10)
        self.assertEqual([entry['uid'] for entry in second['entries']], ['0'])
        self.assertEqual([entry['uid'] for entry in ranked['contract']], ['15'])
        self.assertEqual(ranked['ses'], [])
        audit = json.loads((instance.output / 'engineer_assessments.json').read_text(encoding='utf-8'))
        self.assertEqual(len(audit), 30)
        self.assertEqual(len({(entry['engineer'], entry['uid'], entry['project_key']) for entry in audit}), 30)
        self.assertTrue(any(entry['state'] == 'mismatch' for entry in audit))
        self.assertEqual(second['entries'][0]['source_email']['body'], '0')
        self.assertIn('A\\.pdf', report)
        self.assertIn('B\\.pdf', report)
        self.assertIn('エンジニア情報との照合:', report)
        card = scheduled_run.card_payload('https://example.com/report', ranked, self.config['priority_rules']).decode()
        self.assertIn('A', card)
        self.assertIn('B', card)
        self.assertIn('受託', card)
        profiles = instance.profiles
        match_calls.clear()
        second_run = self.make(mails)
        second_run.profiles = profiles
        with patch.object(second_run.api, 'generate', side_effect=matching):
            second_run.run()
        self.assertFalse(match_calls)
        reused_audit = json.loads((second_run.output / 'engineer_assessments.json').read_text(encoding='utf-8'))
        self.assertEqual([entry['project_key'] for entry in audit], [entry['project_key'] for entry in reused_audit])
        changed_run = self.make(mails)
        changed_run.profiles = [dict(profiles[0], text='Python 5年', sha256='new'), profiles[1]]
        with patch.object(changed_run.api, 'generate', side_effect=matching):
            changed_run.run()
        self.assertEqual(len(match_calls), 15)

    def test_engineer_response_truncation_splits_without_losing_candidates(self):
        instance = self.make([mail(1)])
        items = [{'id': str(i)} for i in range(3)]

        def matching(*args, **kwargs):
            batch = [json.loads(line) for line in self.uploads[kwargs['file_data']['uri']].splitlines()]
            if len(batch) > 1:
                raise app.OutputLimitError('truncated')
            return json.dumps({'records': [checked_record(batch[0], state='unknown',
                commute=dict(mode='unknown', nearest_station='', workplace='', minutes=-1,
                             route='', evidence='勤務地不明'))]})

        with patch.object(instance.api, 'generate', side_effect=matching):
            batch, text, tokens = next(instance.groups(items, 'prompt', instance.settings, instance.report_limit, engineer_matching.SCHEMA))
            records = instance.match_part(batch, text, tokens, 'prompt', 'test')
        self.assertEqual([r['id'] for r in records], ['0', '1', '2'])

    def test_no_profiles_has_no_shared_ses_shortlist(self):
        self.extracted = {'1': [opportunity()]}
        instance = self.make([mail(1)])
        instance.profiles = []
        report = instance.run()
        ranked = json.loads((instance.output / 'ranked_emails.json').read_text(encoding='utf-8'))
        self.assertEqual(ranked['ses_by_engineer'], [])
        self.assertEqual(ranked['ses'], [])
        self.assertIn('エンジニア未登録', report)

    def test_commute_is_required_per_engineer_and_contract_is_independent(self):
        self.extracted = {str(i): [opportunity(project_id=f'P-{i}')] for i in range(6)}
        for projects in self.extracted.values():
            for check in projects[0]['assessments']:
                check.update(state='match', evidence='共通条件を確認')
        contract = opportunity(project_id='C-1')
        contract.update(kind='contract', assessments=[dict(criterion=i + 1, state='match', evidence='開発相談')
                        for i in range(len(self.config['priority_rules']['contract']['criteria']))])
        self.extracted['6'] = [contract]
        instance = self.make([mail(i, str(i)) for i in range(7)])
        instance.profiles = [dict(file='A.pdf', sha256='a', text='Python 最寄り駅: 仙台駅'),
                             dict(file='B.pdf', sha256='b', text='Python 最寄り駅: 東京駅')]
        generate = instance.api.generate

        def matching(*args, **kwargs):
            if kwargs.get('response_schema') != engineer_matching.SCHEMA:
                return generate(*args, **kwargs)
            self.assertIn('片道90分以内', args[2])
            self.assertIn('初日や研修時のみ出社', args[2])
            batch = [json.loads(line) for line in self.uploads[kwargs['file_data']['uri']].splitlines()]
            records = []
            for item in batch:
                number = int(item['project']['project_id'].split('-')[1])
                commute = dict(mode='onsite', nearest_station='仙台駅', workplace='就業先',
                               minutes=90 if 'A.pdf' in args[2] else 91, route='電車と徒歩', evidence='資料の経路')
                if number == 1:
                    commute['minutes'] = 91
                elif number == 2:
                    commute.update(mode='full_remote', minutes=-1, nearest_station='', route='', evidence='出社不要')
                elif number == 3:
                    commute['minutes'] = -1
                elif number == 4:
                    commute['nearest_station'] = ''
                elif number == 5:
                    commute.update(mode='hybrid', minutes=91)
                records.append(checked_record(item, commute=commute))
            return json.dumps(dict(records=records))

        with patch.object(instance.api, 'generate', side_effect=matching):
            report = instance.run()
        ranked = json.loads((instance.output / 'ranked_emails.json').read_text(encoding='utf-8'))
        first, second = ranked['ses_by_engineer']
        self.assertEqual({e['uid'] for e in first['entries']}, {'0', '2'})
        self.assertEqual([e['uid'] for e in second['entries']], ['2'])
        self.assertEqual({e['uid'] for e in first['pending_entries']}, {'3', '4'})
        self.assertEqual([e['uid'] for e in ranked['contract']], ['6'])
        self.assertIn('片道90分', report)
        self.assertIn('フルリモート（出社不要）', report)
        self.assertIn('通勤の根拠:', report)

    def test_shared_rules_exclude_projects_for_every_engineer_before_matching(self):
        accepted = opportunity(project_id='P-OK')
        rejected = opportunity(project_id='P-NG')
        rejected['assessments'][0].update(state='mismatch', evidence='共通の案件地域条件に不適合')
        self.extracted = {'1': [accepted], '2': [rejected]}
        instance = self.make([mail(1, 'accepted'), mail(2, 'rejected')])
        instance.profiles = [dict(file='A.pdf', sha256='a', text='Python'),
                             dict(file='B.pdf', sha256='b', text='Python')]
        generate = instance.api.generate
        matched_projects = []

        def matching(*args, **kwargs):
            if kwargs.get('response_schema') != engineer_matching.SCHEMA:
                return generate(*args, **kwargs)
            batch = [json.loads(line) for line in self.uploads[kwargs['file_data']['uri']].splitlines()]
            matched_projects.extend(item['project']['project_id'] for item in batch)
            return json.dumps({'records': [checked_record(item,
                commute=dict(mode='full_remote', nearest_station='', workplace='', minutes=-1,
                             route='', evidence='出社不要')) for item in batch]})

        with patch.object(instance.api, 'generate', side_effect=matching):
            report = instance.run()
        ranked = json.loads((instance.output / 'ranked_emails.json').read_text(encoding='utf-8'))
        self.assertEqual(matched_projects, ['P-OK', 'P-OK'])
        for group in ranked['ses_by_engineer']:
            self.assertEqual(group['entries'], [])
            self.assertEqual([e['uid'] for e in group['pending_entries']], ['1'])
            self.assertEqual(group['pending_entries'][0]['verification'], '要確認')
        self.assertIn('全エンジニアに共通で適用', report)

    def test_exact_duplicate_preserves_latest_uid_and_all_sources(self):
        instance = self.make([mail(1), mail(2, received="2026-10-02T09:00:00+09:00"), mail(3, "changed")])
        with closing(sqlite3.connect(instance.output / "test.sqlite")) as connection:
            instance.prepare(connection)
            rows = connection.execute("SELECT uid, aliases FROM mails ORDER BY uid").fetchall()
        self.assertEqual(instance.metadata["unique_email_count"], 2)
        self.assertEqual(rows[0][0], "2")
        self.assertEqual([alias["uid"] for alias in json.loads(rows[0][1])], ["1", "2"])
        self.assertEqual(list(app.iter_saved_mails(instance.source))[0]["uid"], "1")

    def test_duplicate_uid_is_rejected_before_generation(self):
        instance = self.make([mail(1), mail(1, "different")])
        with self.assertRaises(app.ToolError):
            instance.run()
        self.assertFalse(self.calls)

    def test_equal_truncated_prefixes_are_never_deduplicated(self):
        first, second = mail(1), mail(2)
        first["body_truncated"] = second["body_truncated"] = True
        instance = self.make([first, second])
        instance.run()
        self.assertEqual(instance.metadata["unique_email_count"], 2)
        self.assertEqual(instance.metadata["duplicate_email_count"], 0)

    def test_long_project_history_is_preserved_and_latest_used_without_summarizing(self):
        self.extracted = {str(index): [opportunity(notes="x" * 200)] for index in range(15)}
        instance = self.make([mail(index, str(index), f"2026-10-01T09:{index:02d}:00+09:00") for index in range(15)])
        instance.run()
        values = [json.loads(line) for line in (instance.output / "opportunities.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(values), 1)
        self.assertEqual(len(values[0]["versions"]), 15)
        histories = [item for group in self.calls for item in group if "condition_history" in item]
        self.assertEqual(len(histories), 0)  # Ranking uses latest conditions directly.

    def test_restart_reuses_completed_chunks_after_failure(self):
        mails = [mail(index, str(index) + "x" * 1200) for index in range(1, 4)]
        self.fail_uid = "2"
        first = self.make(mails)
        with self.assertRaisesRegex(app.ToolError, "transient failure"):
            first.run()
        self.assertEqual(first.metadata["extracted_unique_count"], 1)
        self.fail_uid = None
        self.calls.clear()
        second = self.make(mails)
        second.run()
        generated_uids = [item["uid"] for group in self.calls for item in group if "body" in item]
        self.assertEqual(generated_uids, ["2", "3"])
        self.assertEqual(second.metadata["extracted_unique_count"], 3)
        self.assertGreater(second.metadata["reused_steps"], 0)

    def test_output_truncation_splits_and_remembers_split_on_restart(self):
        self.output_cutoff = 1
        mails = [mail(1, "a"), mail(2, "b"), mail(3, "c")]
        first = self.make(mails)
        first.run()
        self.assertEqual(first.metadata["extracted_unique_count"], 3)
        self.assertTrue(any(len(group) > 1 for group in self.calls if "body" in group[0]))
        self.calls.clear()
        second = self.make(mails)
        second.run()
        self.assertFalse(self.calls)
        self.assertEqual(second.metadata["extracted_unique_count"], 3)

    def test_bad_single_mail_is_skipped_after_splitting_and_retried_on_restart(self):
        mails = [mail(1, 'first'), mail(2, 'bad'), mail(3, 'last')]
        original_generate = self.api.generate

        def generate(*args, **kwargs):
            answer = original_generate(*args, **kwargs)
            if any(item['uid'] == '2' for item in self.calls[-1]):
                return '{invalid json'
            return answer

        instance = self.make(mails)
        with patch.object(self.api, 'generate', side_effect=generate):
            summary = instance.run()
        self.assertEqual(instance.metadata['extracted_unique_count'], 2)
        self.assertEqual(instance.metadata['skipped_unique_count'], 1)
        self.assertEqual(instance.metadata['skipped_emails'], [{'uid': '2', 'reason': 'invalid_extraction'}])
        rows = [json.loads(line) for line in (instance.output / 'extracted_emails.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual([row['uid'] for row in rows], ['1', '3'])
        stats = json.loads((instance.output / 'statistics.json').read_text(encoding='utf-8'))
        self.assertEqual(stats['skipped_emails'], instance.metadata['skipped_emails'])
        self.assertTrue(any(path.read_text(encoding='utf-8') == '{invalid json'
                            for path in instance.work.glob('*.result.txt')))
        app.write_report(instance.output, instance.metadata, summary)
        self.assertIn('抽出失敗によるスキップ: 1 件', (instance.output / 'report.md').read_text(encoding='utf-8'))

        self.calls.clear()
        second = self.make(mails)
        second.run()
        self.assertEqual([item['uid'] for group in self.calls for item in group], ['2'])
        self.assertEqual(second.metadata['extracted_unique_count'], 3)
        self.assertEqual(second.metadata['skipped_unique_count'], 0)

    def test_single_mail_output_limit_skips_without_inventing_results(self):
        instance = self.make([mail(1)])
        with patch.object(self.api, 'generate', side_effect=app.OutputLimitError('output truncated')):
            instance.run()
        self.assertEqual(instance.metadata['extracted_unique_count'], 0)
        self.assertEqual(instance.metadata['skipped_emails'], [{'uid': '1', 'reason': 'output_limit'}])
        self.assertEqual((instance.output / 'extracted_emails.jsonl').read_text(), '')

    def test_aggregation_keeps_condition_history_and_deterministic_counts(self):
        self.extracted = {"1": [opportunity("60万円/月")], "2": [opportunity("70万円/月")],
                          "3": [opportunity(deadline="2026-10-01", project_id="P-2")],
                          "4": [opportunity(status="closed", project_id="")],
                          "5": [opportunity(project_id="")]}
        instance = self.make([mail(index, str(index), f"2026-10-0{index}T09:00:00+09:00") for index in range(1, 6)])
        instance.run()
        stats = json.loads((instance.output / "statistics.json").read_text(encoding="utf-8"))
        self.assertEqual(stats["raw_emails"], 5)
        self.assertEqual(stats["opportunity_groups"], 4)
        self.assertEqual(stats["potential_candidates"], 2)
        self.assertEqual(stats["closed_or_expired_at_period_end"], 2)
        values = [json.loads(line) for line in (instance.output / "opportunities.jsonl").read_text(encoding="utf-8").splitlines()]
        grouped = next(value for value in values if value["latest"]["project_id"] == "P-1")
        self.assertEqual(grouped["latest"]["budget"], "70万円/月")
        self.assertEqual(len(grouped["versions"]), 2)
        self.assertEqual(grouped["source_uids"], ["1", "2"])
        self.assertNotIn("total_budget", stats)

    def test_ranking_preserves_full_extracted_artifacts_without_report_api_calls(self):
        self.extracted = {str(index): [opportunity(project_id=f"P-{index}", notes="x" * 400)] for index in range(12)}
        instance = self.make([mail(index, str(index)) for index in range(12)])
        instance.run()
        report_calls = [group for group in self.calls if "body" not in group[0]]
        self.assertEqual(len(report_calls), 0)
        self.assertEqual(len(json.loads((instance.output / "ranked_emails.json").read_text(encoding="utf-8"))["ses"]), 10)
        self.assertEqual(len((instance.output / "opportunities.jsonl").read_text(encoding="utf-8").splitlines()), 12)
        self.assertEqual(instance.metadata["extracted_unique_count"], 12)

    def test_mixed_mail_ranks_in_both_categories_and_mismatch_is_excluded(self):
        ses = opportunity()
        contract = opportunity(project_id="C-1")
        contract.update(kind="contract", assessments=[
            {"criterion": i, "state": "match", "evidence": "予算300万円・東京の既存システム改修"}
            for i in range(1, 5)])
        rejected = opportunity(project_id="P-2")
        rejected["assessments"][0].update(state="mismatch", evidence="主要技術はPHP")
        self.extracted = {"1": [ses, contract], "2": [rejected]}
        instance = self.make([mail(1), mail(2, "PHP development")])
        report = instance.run()
        result = json.loads((instance.output / "ranked_emails.json").read_text(encoding="utf-8"))
        self.assertEqual([value["uid"] for value in result["ses"]], ["1"])
        self.assertEqual([value["uid"] for value in result["contract"]], ["1"])
        self.assertEqual(result["contract"][0]["verification"], "適合")
        self.assertIn("SES派遣の要員募集", report)
        self.assertIn("受託案件としてアプローチできそうな案件", report)

    def test_changed_rule_reextracts_instead_of_reusing_old_assessments(self):
        self.make([mail(1)]).run()
        self.calls.clear()
        self.config["priority_rules"]["ses"]["criteria"][0] = "主要技術はPythonのみ"
        instance = self.make([mail(1)])
        instance.run()
        self.assertEqual(len(self.calls), 1)
        snapshot = json.loads((instance.output / "priority_rules.json").read_text(encoding="utf-8"))
        self.assertEqual(snapshot["ses"]["criteria"][0], "主要技術はPythonのみ")

    def test_cache_identity_includes_instructions_model_and_output_settings(self):
        instance = self.make([mail(1)])
        key = instance.identity("data", "instructions", instance.settings, pipeline.EXTRACTION_SCHEMA)
        for field, value in (("model", "other-model"), ("max_output_tokens", 1000), ("thinking_level", "high")):
            self.assertNotEqual(key, instance.identity("data", "instructions", dict(instance.settings, **{field: value}), pipeline.EXTRACTION_SCHEMA))
        self.assertNotEqual(key, instance.identity("data", "new instructions", instance.settings, pipeline.EXTRACTION_SCHEMA))
        self.assertNotEqual(key, instance.identity("changed", "instructions", instance.settings, pipeline.EXTRACTION_SCHEMA))

    def test_title_stages_send_only_subjects_then_analyze_only_survivors(self):
        self.config["priority_rules"]["prefilter"]["enabled"] = True
        self.screen_excluded = {"2"}
        mails = [dict(mail(1), subject="社員紹介"), dict(mail(2), subject="製品の販売情報"),
                 dict(mail(3), subject="Python開発案件募集")]
        instance = self.make(mails)
        instance.run()
        screened = [item for batch in self.screen_calls for item in batch]
        self.assertEqual({item['uid'] for item in screened}, {'2', '3'})
        self.assertTrue(all(set(item) == {'uid', 'subject'} for item in screened))
        analyzed = [item['uid'] for batch in self.calls for item in batch]
        self.assertEqual(analyzed, ['3'])
        self.assertEqual(instance.metadata['mechanical_excluded_count'], 1)
        self.assertEqual(instance.metadata['ai_excluded_count'], 1)
        self.assertEqual(instance.metadata['analysis_target_count'], 1)
        self.assertEqual(instance.metadata['extracted_unique_count'], 1)
        stats = json.loads((instance.output / 'statistics.json').read_text(encoding='utf-8'))
        self.assertEqual(stats['raw_emails'], 3)
        self.assertEqual(sum(stats['raw_emails_by_kind'].values()), 1)

    def test_analysis_cap_boundaries_keep_newest_received_not_input_or_uid_order(self):
        start = datetime.fromisoformat('2026-10-01T09:00:00+09:00')
        for count in (999, 1000, 1001):
            with self.subTest(count=count):
                mails = [mail(count - i, str(i), (start + timedelta(seconds=i)).isoformat())
                         for i in range(count)]
                instance = self.make(list(reversed(mails)))
                with closing(sqlite3.connect(':memory:')) as connection:
                    instance.prepare(connection)
                    instance.screen_titles(connection)
                    selected = {row[0] for row in connection.execute('SELECT uid FROM selected')}
                self.assertEqual(selected, {item['uid'] for item in mails[-1000:]})
                self.assertEqual(instance.metadata['analysis_target_count'], min(count, 1000))
                self.assertEqual(instance.metadata['limit_excluded_count'], max(0, count - 1000))

    def test_analysis_cap_applies_after_both_title_filters_and_limits_body_extraction(self):
        self.config['priority_rules']['prefilter']['enabled'] = True
        self.screen_excluded = {'1002'}
        start = datetime.fromisoformat('2026-10-01T09:00:00+09:00')
        mails = [mail(i, str(i), (start + timedelta(seconds=i)).isoformat()) for i in range(1004)]
        mails[-1]['subject'] = '社員紹介'
        instance = self.make(list(reversed(mails)))
        instance.extract_limit = 1000000
        report = instance.run()
        screened = {item['uid'] for batch in self.screen_calls for item in batch}
        self.assertEqual(screened, {str(i) for i in range(1003)})
        analyzed = {item['uid'] for batch in self.calls for item in batch}
        self.assertEqual(analyzed, {str(i) for i in range(2, 1002)})
        self.assertEqual(instance.metadata['mechanical_excluded_count'], 1)
        self.assertEqual(instance.metadata['ai_excluded_count'], 1)
        self.assertEqual(instance.metadata['limit_excluded_count'], 2)
        self.assertEqual(instance.metadata['analysis_target_count'], 1000)
        self.assertEqual(instance.metadata['extracted_unique_count'], 1000)
        stats = json.loads((instance.output / 'statistics.json').read_text(encoding='utf-8'))
        self.assertEqual(stats['limit_excluded_count'], 2)
        self.assertEqual(stats['analysis_target_count'], 1000)
        app.write_report(instance.output, instance.metadata, report)
        self.assertIn('上限超過による除外: 2 件',
                      (instance.output / 'report.md').read_text(encoding='utf-8'))

    def test_report_embeds_latest_source_mail_without_rewriting_it(self):
        self.extracted = {'1': [opportunity()], '2': [opportunity()]}
        mails = [dict(mail(1, '以前の条件'), subject='以前の件名'),
                 dict(mail(2, '最新本文\n\n  空白と|記号', '2026-10-02T09:00:00+09:00'),
                      subject='最新の件名', to='receiver@example.invalid')]
        instance = self.make(mails)
        report = instance.run()
        self.assertIn('最新本文\n\n  空白と|記号', report)
        self.assertIn('件名: 最新の件名', report)
        self.assertNotIn('以前の条件', report)
        self.assertNotIn('UID:', report)
        ranked = json.loads((instance.output / 'ranked_emails.json').read_text(encoding='utf-8'))
        self.assertEqual(ranked['ses'][0]['source_email']['body'], mails[1]['body'])

    def test_all_mechanically_excluded_never_calls_generation(self):
        self.config['priority_rules']['prefilter']['enabled'] = True
        instance = self.make([dict(mail(1), subject='新年のご挨拶'), dict(mail(2), subject='自社製品のご紹介')])
        report = instance.run()
        self.assertFalse(self.screen_calls)
        self.assertFalse(self.calls)
        self.assertEqual(instance.metadata['analysis_target_count'], 0)
        self.assertEqual((instance.output / 'extracted_emails.jsonl').read_text(), '')
        self.assertIn('該当候補はありません', report)

    def test_all_ai_excluded_never_calls_body_extraction(self):
        self.config['priority_rules']['prefilter']['enabled'] = True
        self.screen_excluded = {'1', '2'}
        instance = self.make([dict(mail(1), subject='商材の情報'), dict(mail(2), subject='担当変更について')])
        instance.run()
        self.assertTrue(self.screen_calls)
        self.assertFalse(self.calls)
        self.assertEqual(instance.metadata['ai_excluded_count'], 2)

    def test_title_ai_disabled_still_applies_mechanical_filter(self):
        self.config['priority_rules']['prefilter'].update(enabled=True, ai_enabled=False)
        instance = self.make([dict(mail(1), subject='社員紹介'), dict(mail(2), subject='開発相談')])
        instance.run()
        self.assertFalse(self.screen_calls)
        self.assertEqual([item['uid'] for batch in self.calls for item in batch], ['2'])

    def test_title_batches_are_bounded_and_cached(self):
        self.config['priority_rules']['prefilter'].update(enabled=True, batch_size=2)
        mails = [dict(mail(i, str(i)), subject=f'開発相談 {i}') for i in range(5)]
        self.make(mails).run()
        self.assertEqual([len(batch) for batch in self.screen_calls], [2, 2, 1])
        self.screen_calls.clear()
        self.calls.clear()
        self.make(mails).run()
        self.assertFalse(self.screen_calls)
        self.assertFalse(self.calls)

    def test_invalid_title_response_stops_before_body_analysis(self):
        self.config['priority_rules']['prefilter']['enabled'] = True
        instance = self.make([dict(mail(1), subject='開発相談')])
        with patch.object(instance.api, 'generate', return_value='{"records": []}'):
            with self.assertRaisesRegex(app.ToolError, '件名のAI選別'):
                instance.run()
        self.assertFalse(self.calls)

    def test_truncated_title_response_splits_into_smaller_batches(self):
        self.config['priority_rules']['prefilter']['enabled'] = True
        instance = self.make([dict(mail(i, str(i)), subject=f'開発相談 {i}') for i in range(3)])
        generate = instance.api.generate

        def capped(*args, **kwargs):
            items = self.uploads[kwargs['file_data']['uri']].splitlines()
            if kwargs.get('response_schema') == title_filter.SCHEMA and len(items) > 1:
                raise app.OutputLimitError('truncated')
            return generate(*args, **kwargs)

        with patch.object(instance.api, 'generate', side_effect=capped):
            instance.run()
        self.assertEqual(instance.metadata['analysis_target_count'], 3)

    def test_model_and_local_quota_limit_effective_file_budget(self):
        self.config["gemini"]["input_tokens_per_minute"] = 2000
        self.config["analysis"].update(chunk_input_tokens=30000, report_input_tokens=60000)
        instance = self.make([mail(1)])
        self.assertEqual(instance.extract_limit, 1600)
        self.assertEqual(instance.report_limit, 1600)


class ValidationAndRateTests(unittest.TestCase):
    def test_missing_duplicate_unknown_uid_and_invalid_date_are_rejected(self):
        record = {"uid": "1", "kind": "ses", "tags": [], "market_notes": "", "opportunities": []}
        mails = [mail(1), mail(2)]
        cases = [[], [record], [record, record], [record, dict(record, uid="unknown")],
                 [record, dict(record, uid="2", opportunities=[opportunity(deadline="2026-02-30")])],
                 [record, dict(record, uid="2", opportunities=[opportunity(deadline="20261001")])]]
        for records in cases:
            with self.subTest(records=records), self.assertRaises(pipeline.InvalidExtraction):
                pipeline.validate_extraction(json.dumps({"records": records}), mails)
        valid = [record, dict(record, uid="2")]
        self.assertEqual(pipeline.validate_extraction(json.dumps({"records": valid}), mails), valid)

    def test_rolling_token_and_request_budget_survives_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "rate.json"
            now = [100.0]
            waits = []
            def sleep(seconds):
                waits.append(seconds)
                now[0] += seconds
            first = pipeline.RateLimiter(path, 5, 1000, lambda: now[0], sleep)
            first.reserve(600)
            second = pipeline.RateLimiter(path, 5, 1000, lambda: now[0], sleep)
            second.reserve(600)
            self.assertGreaterEqual(now[0], 160)
            self.assertTrue(all(wait <= 30 for wait in waits))
            second.requests = 1
            second.reserve(100)
            self.assertGreaterEqual(now[0], 220)
            with self.assertRaises(ValueError):
                second.reserve(1001)

    def test_bad_rate_history_does_not_crash(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "rate.json"
            pipeline.atomic_json(path, {"events": [None, "bad", [3], ["x", 1], [0, 2]]})
            limiter = pipeline.RateLimiter(path, 5, 1000, clock=lambda: 100)
            limiter.reserve(100)
            self.assertEqual(list(limiter.events), [(100, 100)])


if __name__ == "__main__":
    unittest.main()
