"""Read engineers.md sections and validate evidence-based project matching."""
import hashlib
import json
import re
from pathlib import Path


SCHEMA = {"type": "OBJECT", "properties": {"records": {
    "type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "id": {"type": "STRING"},
        "state": {"type": "STRING", "enum": ["match", "mismatch", "unknown"]},
        "evidence": {"type": "STRING"},
        "unassessed_requirements": {"type": "ARRAY", "items": {"type": "STRING"}},
        "requirements": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "criterion": {"type": "INTEGER"},
            "state": {"type": "STRING", "enum": ["match", "mismatch", "unknown"]},
            "project_quote": {"type": "STRING"}, "engineer_quote": {"type": "STRING"},
            "reason": {"type": "STRING"}},
            "required": ["criterion", "state", "project_quote", "engineer_quote", "reason"]}},
        "commute": {"type": "OBJECT", "properties": {
            "mode": {"type": "STRING", "enum": ["full_remote", "onsite", "hybrid", "unknown"]},
            "nearest_station": {"type": "STRING"}, "workplace": {"type": "STRING"},
            "minutes": {"type": "INTEGER"}, "route": {"type": "STRING"},
            "evidence": {"type": "STRING"}},
            "required": ["mode", "nearest_station", "workplace", "minutes", "route", "evidence"]}},
        "required": ["id", "state", "evidence", "requirements", "unassessed_requirements", "commute"]}}},
    "required": ["records"]}


def commute_state(commute):
    """A one-way journey of exactly 90 minutes is eligible; unknown is not."""
    if commute['mode'] == 'full_remote':
        return 'match'
    if (commute['mode'] == 'unknown' or commute['minutes'] < 0
            or not all(commute[key].strip() for key in ('nearest_station', 'workplace', 'route'))):
        return 'unknown'
    return 'match' if commute['minutes'] <= 90 else 'mismatch'


def load_profiles(path):
    path = Path(path)
    try:
        text = path.read_text(encoding='utf-8-sig')
    except (OSError, UnicodeError):
        raise ValueError(f"エンジニア一覧をUTF-8で読めません: {path}") from None
    # Each level-two heading is one person. Preamble applies to every person;
    # other people's sections must never be sent as evidence for this person.
    headings = list(re.finditer(r'^##[ \t]+(.+)$', text, re.MULTILINE))
    if not headings:
        raise ValueError('engineers.md にエンジニアごとの ## 見出しを記載してください。')
    preamble = text[:headings[0].start()].strip()
    profiles, names, identities = [], set(), set()
    for index, heading in enumerate(headings):
        name = re.sub(r'^\d+[.)．、]\s*', '', heading[1].strip()).split('｜', 1)[0].strip()
        body = text[heading.end():headings[index + 1].start() if index + 1 < len(headings) else len(text)].strip()
        if not name or name in names or not body:
            raise ValueError('エンジニアの名前の重複、または空のプロフィールがあります。')
        names.add(name)
        # Source sheet identity survives display-name changes and heading reorder.
        source_identity = heading[1].strip().split('｜', 1)
        identity_source = source_identity[1].strip() if len(source_identity) == 2 else name
        career_id = 'career_' + hashlib.sha256(identity_source.encode('utf-8')).hexdigest()[:20]
        if career_id in identities:
            raise ValueError('キャリアシートIDが重複しています。見出しの資料名を確認してください。')
        identities.add(career_id)
        feedback_sections = list(re.finditer(
            r'^###[ \t]+マッチングフィードバック[ \t]*(?:\n|\Z)(.*?)(?=^###[ \t]+|\Z)', body, re.MULTILINE | re.DOTALL))
        if len(feedback_sections) > 1:
            raise ValueError(f'{name} のマッチングフィードバック見出しが重複しています。')
        feedback = ''
        if feedback_sections:
            match = feedback_sections[0]
            feedback = re.sub(r'<!--.*?-->', '', match[1], flags=re.DOTALL).strip()
            body = (body[:match.start()] + body[match.end():]).strip()
        if not body:
            raise ValueError(f'{name} のプロフィール本文がありません。')
        section = '\n\n'.join((preamble, '## ' + name, body))
        identity = json.dumps(dict(text=section, feedback=feedback), ensure_ascii=False, sort_keys=True)
        profiles.append({'file': name, 'name': name, 'id': career_id, 'source': path.name,
                         'sha256': hashlib.sha256(identity.encode('utf-8')).hexdigest(),
                         'text': section, 'feedback': feedback})
    return profiles


def contains_quote(quote, source):
    """Allow line wrapping, but never accept paraphrases as source quotations."""
    return bool(quote.strip()) and re.sub(r'\s+', '', quote) in re.sub(r'\s+', '', source)


def pending_record(item, error):
    """Preserve an unvalidated project without trusting any of the AI response."""
    reason = f'AIの適合判定を確認できないため判定保留: {error}'
    return dict(
        id=item['id'], state='unknown', evidence=reason,
        requirements=[dict(criterion=index, state='unknown',
                           project_quote=requirement['source_quote'], engineer_quote='', reason=reason)
                      for index, requirement in enumerate(
                          item.get('project', {}).get('mandatory_requirements', []), 1)],
        unassessed_requirements=[],
        commute=dict(mode='unknown', nearest_station='', workplace='', minutes=-1,
                     route='', evidence=reason))


def validate(answer, items, profile_text=''):
    try:
        records = json.loads(answer)['records']
        expected = {item['id'] for item in items}
        if not isinstance(records, list) or len(records) != len(expected):
            raise ValueError
        seen = set()
        by_id = {item['id']: item for item in items}
        for record in records:
            if (record['id'] not in expected or record['id'] in seen
                    or record['state'] not in {'match', 'mismatch', 'unknown'}
                    or not isinstance(record['evidence'], str) or not record['evidence'].strip()):
                raise ValueError
            requirements = by_id[record['id']].get('project', {}).get('mandatory_requirements', [])
            checks = record['requirements']
            if not isinstance(checks, list) or len(checks) != len(requirements):
                raise ValueError
            for index, (check, requirement) in enumerate(zip(checks, requirements), 1):
                if (type(check['criterion']) is not int or check['criterion'] != index
                        or check['state'] not in {'match', 'mismatch', 'unknown'}
                        or not all(isinstance(check[key], str) for key in ('project_quote', 'engineer_quote', 'reason'))
                        or not check['reason'].strip()):
                    raise ValueError
                # An unsupported AI assertion must never become a confirmed match.
                if (not contains_quote(check['project_quote'], requirement['source_quote'])
                        or not contains_quote(requirement['source_quote'], check['project_quote'])
                        or (check['state'] in {'match', 'mismatch'}
                            and not contains_quote(check['engineer_quote'], profile_text))):
                    check.update(state='unknown', reason=check['reason'] + '（引用根拠を原文で確認できません）')
            states = [check['state'] for check in checks]
            omissions = record['unassessed_requirements']
            if not isinstance(omissions, list) or not all(isinstance(value, str) and value.strip() for value in omissions):
                raise ValueError
            record['state'] = ('mismatch' if 'mismatch' in states else
                               'match' if states and not omissions and all(state == 'match' for state in states) else 'unknown')
            if omissions:
                record['evidence'] += '（未照合の必須条件: ' + ' / '.join(omissions) + '）'
            if by_id[record['id']].get('source_truncated') and record['state'] == 'match':
                record.update(state='unknown', evidence=record['evidence'] + '（メール本文省略のため全必須条件を確認できません）')
            seen.add(record['id'])
            commute = record['commute']
            if (not isinstance(commute, dict)
                    or commute['mode'] not in {'full_remote', 'onsite', 'hybrid', 'unknown'}
                    or type(commute['minutes']) is not int or commute['minutes'] < -1
                    or not all(isinstance(commute[key], str)
                               for key in ('nearest_station', 'workplace', 'route', 'evidence'))
                    or not commute['evidence'].strip()):
                raise ValueError
        return records
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ValueError('エンジニア適合判定の件数・ID・根拠が不正です。') from None
