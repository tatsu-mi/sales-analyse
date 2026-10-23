"""Read isolated career sheet sections and personal feedback."""
import hashlib
import json
import re
from pathlib import Path


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
