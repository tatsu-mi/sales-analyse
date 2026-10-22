import copy
import json
import unittest
from datetime import date

import integrated_matching as matching


def fact(terms=('Python',), experience='professional', months=48, **extra):
    return dict(dict(terms=list(terms), event_id='work-1', experience=experience,
                     polarity='present', months=months, duration_kind='total',
                     start='', end='', quote='Python'), **extra)


def career(item=None, facts=None, attributes=None, routes=None):
    return dict(id=item['id'] if item else 'career-A', facts=[fact()] if facts is None else facts,
                attributes=attributes or [], routes=routes or [], complete=True, unassessed_information=[])


def atom(terms=('Python',), months=0, experience='professional', identifier='a'):
    return dict(id=identifier, op='atom', field='skill', terms=list(terms),
                experience=experience, min_months=months)


def expression(node=None, criterion=1):
    node = node or atom()
    return dict(criterion=criterion, root=node['id'], nodes=[node],
                keywords=[matching.keyword_id('Python')])


def plan(item, keywords=None):
    keys = keywords if keywords is not None else [matching.keyword_id('Python')]
    requirements = [dict(expression(criterion=i), keywords=keys)
                    for i, _ in enumerate(item['project']['mandatory_requirements'], 1)]
    return dict(id=item['id'], requirements=requirements, additional_requirements=[],
                preferences=[], complete=True, unassessed_requirements=[],
                commute=dict(mode='full_remote', workplaces=[], quote=item['source_body']))


class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.person = career()
        self.item = dict(id='project-X', project=dict(mandatory_requirements=[
            dict(requirement='Python', category='skill', source_quote='Python')]),
            source_body='Python 出社不要', source_truncated=False)
        self.plan = plan(self.item)
        self.today = date(2026, 10, 22)

    def check(self, expr, person=None):
        matching.validate_expression(expr)
        return matching.evaluate(expr, person or self.person, self.today)[0]

    def assess(self):
        return matching.assess(self.plan, self.item, self.person, self.today, matching.DEFAULT_WEIGHTS)

    def test_profiles_validate_own_quotes_and_reject_other_people_and_feedback(self):
        item = dict(id='career-A', text='Python')
        valid = json.dumps(dict(records=[self.person]))
        self.assertEqual(matching.validate_profiles(valid, [item]), [self.person])
        for source in ('Java', '本文にフィードバックは含めない'):
            with self.subTest(source=source), self.assertRaises(ValueError):
                matching.validate_profiles(valid, [dict(item, text=source)])
        with self.assertRaises(ValueError):
            matching.validate_profiles(json.dumps(dict(records=[self.person, self.person])), [item])

    def test_index_retains_people_and_experience_and_normalizes_only_known_aliases(self):
        second = career(dict(id='B'), facts=[fact(('JS',), 'non_professional')])
        first = career(facts=[fact(('JavaScript',))])
        index = matching.build_index([first, second])
        self.assertEqual(len(index), 1)
        self.assertEqual({p['experience'] for p in index[0]['careers']}, {'professional', 'non_professional'})
        self.assertNotEqual(matching.keyword_id('Java'), matching.keyword_id('JavaScript'))
        self.assertNotEqual(matching.keyword_id('C'), matching.keyword_id('C++'))

    def test_experience_duration_and_explicit_missing_differ_from_omission(self):
        self.assertEqual(self.check(expression(atom(months=36))), 'match')
        self.assertEqual(self.check(expression(atom(months=60))), 'mismatch')
        self.assertEqual(self.check(expression(atom(('Java',)))), 'unknown')
        self.person['facts'].append(fact(('Java',), polarity='absent'))
        self.assertEqual(self.check(expression(atom(('Java',)))), 'mismatch')
        self.person['facts'] = [fact(experience='non_professional')]
        self.assertEqual(self.check(expression()), 'unknown')
        self.assertEqual(self.check(expression(atom(experience='any'))), 'match')

    def test_different_work_roles_are_not_joined(self):
        self.person['facts'] = [fact(('Java',)), fact(('Python', 'リーダー'), event_id='work-2')]
        self.assertEqual(self.check(expression(atom(('Java', 'リーダー')))), 'unknown')
        self.assertEqual(self.check(expression(atom(('Python', 'リーダー')))), 'match')

    def test_unknown_duration_does_not_become_insufficient(self):
        self.person['facts'] = [fact(months=12, duration_kind='period')]
        self.assertEqual(self.check(expression(atom(months=36))), 'unknown')

    def test_no_professional_experience_does_not_negate_learning_experience(self):
        self.person['facts'] = [fact(polarity='absent'), fact(experience='non_professional')]
        self.assertEqual(self.check(expression(atom(experience='any'))), 'match')
        self.assertEqual(self.check(expression(atom(experience='professional'))), 'mismatch')
        self.person['facts'].append(fact())
        self.plan['requirements'][0] = expression(atom(experience='any'))
        self.assertEqual(self.assess()['score'], 1)
        self.person['facts'] = [fact(polarity='absent')]
        self.assertEqual(self.check(expression(atom(experience='any'))), 'unknown')

    def test_parallel_periods_are_merged_and_undated_periods_are_not_summed(self):
        self.person['facts'] = [
            fact(months=12, duration_kind='period', start='2025-01', end='2025-12'),
            fact(months=12, duration_kind='period', start='2025-01', end='2025-12', event_id='work-2')]
        self.assertEqual(self.check(expression(atom(months=24))), 'unknown')
        self.assertEqual(self.check(expression(atom(months=12))), 'match')
        self.person['facts'][0].update(start='2024-01', end='2024-12')
        self.assertEqual(self.check(expression(atom(months=24))), 'match')
        for f in self.person['facts']:
            f.update(start='', end='')
        self.assertEqual(self.check(expression(atom(months=24))), 'unknown')

    def test_three_state_nested_and_or_and_invalid_graphs(self):
        expr = dict(root='root', nodes=[
            dict(id='root', op='all', children=['a', 'or']), atom(),
            dict(id='or', op='any', children=['b', 'c']),
            atom(('Java',), identifier='b'), atom(identifier='c')])
        self.assertEqual(self.check(expr), 'match')
        expr['nodes'][-1]['terms'] = ['Rust']
        self.assertEqual(self.check(expr), 'unknown')
        self.person['facts'].append(fact(('Java',), polarity='absent'))
        self.person['facts'].append(fact(('Rust',), polarity='absent'))
        self.assertEqual(self.check(expr), 'mismatch')
        for bad in (dict(root='root', nodes=[dict(id='root', op='all', children=['root'])]),
                    dict(root='root', nodes=[dict(id='root', op='all', children=[])]),
                    dict(root='a', nodes=[atom(), atom(identifier='unused')])):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                matching.validate_expression(bad)

    def test_missing_dictionary_requirement_stays_in_final_check(self):
        missing = dict(expression(atom(('英語での顧客折衝',)), criterion=2), keywords=[])
        self.plan['requirements'].append(missing)
        self.item['project']['mandatory_requirements'].append(
            dict(requirement='英語での顧客折衝', source_quote='英語での顧客折衝', category='skill'))
        assessment = self.assess()
        self.assertEqual(assessment['state'], 'unknown')
        self.assertEqual(len(assessment['requirements']), 2)

    def test_unassessed_truncated_incomplete_and_no_requirements_never_confirm(self):
        self.assertEqual(self.assess()['state'], 'match')
        for field, value in [('complete', False), ('unassessed_requirements', ['未処理条件'])]:
            original = self.plan[field]
            self.plan[field] = value
            self.assertEqual(self.assess()['state'], 'unknown')
            self.plan[field] = original
        self.item['source_truncated'] = True
        self.assertEqual(self.assess()['state'], 'unknown')
        self.item['source_truncated'] = False
        self.plan['requirements'] = []
        self.item['project']['mandatory_requirements'] = []
        self.assertEqual(self.assess()['state'], 'unknown')

    def test_preferences_nonprofessional_factor_duplicates_and_required_experience_gate(self):
        java = matching.keyword_id('Java')
        self.person['facts'].extend([fact(('Java',), 'non_professional'), fact(('Java',), 'non_professional')])
        self.plan['preferences'] = [dict(id='welcome-1', kind='welcome', keywords=[java], quote='Java'),
                                    dict(id='duplicate', kind='welcome', keywords=[java], quote='Java'),
                                    dict(id='required-again', kind='welcome', keywords=[matching.keyword_id('Python')], quote='Python')]
        result = self.assess()
        self.assertEqual(result['score'], 10.5)
        self.assertEqual(len(result['score_breakdown']), 2)
        self.person['facts'].append(fact(('Java',)))
        self.assertEqual(self.assess()['score'], 15)
        self.person['facts'] = [fact(experience='non_professional')]
        result = self.assess()
        self.assertEqual(result['state'], 'unknown')
        self.assertEqual(result['score'], 1)

    def test_overlapping_preferences_and_duplicate_requirements_do_not_add_twice(self):
        self.person['facts'].extend([fact(('Java',)), fact(('SQL',))])
        java, sql = matching.keyword_id('Java'), matching.keyword_id('SQL')
        self.plan['preferences'] = [dict(id='a', kind='welcome', keywords=[java], quote='Java'),
                                    dict(id='b', kind='related', keywords=[java, sql], quote='Java SQL')]
        self.plan['requirements'].append(expression(criterion=2))
        self.item['project']['mandatory_requirements'].append(
            dict(requirement='Python', category='skill', source_quote='Python'))
        result = self.assess()
        self.assertEqual(result['score'], 17)
        self.assertEqual(len(result['score_breakdown']), 3)

    def test_or_scoring_uses_the_experience_that_actually_satisfies_requirement(self):
        self.person['facts'] = [fact(('Java',), months=12), fact(('Kotlin',), experience='non_professional')]
        self.plan['requirements'][0] = dict(root='root', criterion=1,
            nodes=[dict(id='root', op='any', children=['a', 'b']),
                   atom(('Java',), months=36), atom(('Kotlin',), experience='any', identifier='b')],
            keywords=[matching.keyword_id('Java'), matching.keyword_id('Kotlin')])
        result = self.assess()
        self.assertEqual(result['state'], 'match')
        self.assertEqual(result['score'], 1)

    def test_future_periods_do_not_supply_unearned_experience(self):
        self.person['facts'] = [fact(months=24, duration_kind='period', start='2026-01', end='2027-12')]
        self.assertEqual(self.check(expression(atom(months=12))), 'unknown')

    def test_commute_atom_uses_same_deterministic_check_as_global_policy(self):
        self.plan['requirements'][0] = dict(expression(dict(id='a', op='atom', field='commute')),
                                             keywords=[matching.keyword_id('Python')])
        self.assertEqual(self.assess()['state'], 'match')
        self.plan['commute'] = dict(mode='unknown', workplaces=[], quote='')
        self.assertEqual(self.assess()['state'], 'unknown')

    def test_project_validation_rejects_missing_requirements_fabricated_quotes_and_keyword_ids(self):
        dictionary = [dict(id=matching.keyword_id('Python'), name='python')]
        self.assertEqual(matching.validate_projects(json.dumps(dict(records=[self.plan])), [self.item], dictionary), [self.plan])
        for mutate in (lambda p: p.update(requirements=[]),
                       lambda p: p['commute'].update(quote='not in source'),
                       lambda p: p['requirements'][0].update(keywords=['invented'])):
            bad = copy.deepcopy(self.plan)
            mutate(bad)
            with self.assertRaises(ValueError):
                matching.validate_projects(json.dumps(dict(records=[bad])), [self.item], dictionary)

    def test_commute_requires_all_destinations_and_accepts_exactly_90_minutes(self):
        self.person['attributes'] = [dict(field='nearest_station', value='駅A', quote='駅A')]
        self.person['routes'] = [dict(origin='駅A', workplace='駅B', minutes=90, route='電車と徒歩', quote='経路の記載')]
        commute = dict(mode='hybrid', workplaces=['駅B'], quote='初日だけ出社')
        self.assertEqual(matching.commute_check(commute, self.person)[0], 'match')
        commute['workplaces'].append('駅C')
        self.assertEqual(matching.commute_check(commute, self.person)[0], 'unknown')
        self.person['routes'][0]['minutes'] = 91
        self.assertEqual(matching.commute_check(commute, self.person)[0], 'mismatch')
        self.assertEqual(matching.commute_check(dict(mode='full_remote', workplaces=[], quote='出社不要'), self.person)[0], 'match')

    def test_numeric_date_and_text_attributes(self):
        self.person['attributes'] = [dict(field='monthly_rate_min', value='650000', quote='65万円'),
                                     dict(field='available_from', value='2026-11-02', quote='11月2日')]
        n = dict(id='a', op='atom', field='monthly_rate_min', comparison='lte', value='700000')
        self.assertEqual(self.check(expression(n)), 'match')
        n['value'] = '600000'
        self.assertEqual(self.check(expression(n)), 'mismatch')
        n.update(field='available_from', value='2026-11-01')
        self.assertEqual(self.check(expression(n)), 'mismatch')
        self.person['attributes'][-1]['value'] = '2026-10-01'
        self.assertEqual(self.check(expression(n)), 'unknown')

    def test_categorical_spelling_and_inclusion_are_not_false_mismatches(self):
        self.person['attributes'] = [dict(field='residence', value='Japan', quote='Japan')]
        node = dict(id='a', op='atom', field='residence', comparison='eq', value='日本')
        self.assertEqual(self.check(expression(node)), 'match')
        self.person['attributes'][0]['value'] = '東京都'
        self.assertEqual(self.check(expression(node)), 'unknown')
        self.person['attributes'][0]['value'] = 'US'
        self.assertEqual(self.check(expression(node)), 'mismatch')
        self.person['attributes'] = [dict(field='contract_type', value='業務委託', quote='業務委託')]
        node.update(field='contract_type', value='準委任')
        self.assertEqual(self.check(expression(node)), 'unknown')

    def test_shortlist_orders_by_score_deduplicates_mail_and_is_stable(self):
        values = [dict(uid=str(i), project_key=str(i), received_at='2026-10-22',
                       latest=dict(status='open', assessments=[]),
                       engineer_match=dict(state='match', score=i)) for i in range(15)]
        values.append(dict(values[0], project_key='better', engineer_match=dict(state='match', score=100)))
        first = matching.shortlist(values, {'top_n': 10})
        self.assertEqual(len(first), 10)
        self.assertEqual([v['uid'] for v in first[:2]], ['0', '14'])
        self.assertEqual(first, matching.shortlist(list(reversed(values)), {'top_n': 10}))


if __name__ == '__main__':
    unittest.main()
