"""Read one engineer per PDF and validate evidence-based project matching."""
import hashlib
import json
from pathlib import Path


SCHEMA = {"type": "OBJECT", "properties": {"records": {
    "type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "id": {"type": "STRING"},
        "state": {"type": "STRING", "enum": ["match", "mismatch", "unknown"]},
        "evidence": {"type": "STRING"},
        "commute": {"type": "OBJECT", "properties": {
            "mode": {"type": "STRING", "enum": ["full_remote", "onsite", "hybrid", "unknown"]},
            "nearest_station": {"type": "STRING"}, "workplace": {"type": "STRING"},
            "minutes": {"type": "INTEGER"}, "route": {"type": "STRING"},
            "evidence": {"type": "STRING"}},
            "required": ["mode", "nearest_station", "workplace", "minutes", "route", "evidence"]}},
        "required": ["id", "state", "evidence", "commute"]}}},
    "required": ["records"]}


def commute_state(commute):
    """A one-way journey of exactly 90 minutes is eligible; unknown is not."""
    if commute['mode'] == 'full_remote':
        return 'match'
    if (commute['mode'] == 'unknown' or commute['minutes'] < 0
            or not all(commute[key].strip() for key in ('nearest_station', 'workplace', 'route'))):
        return 'unknown'
    return 'match' if commute['minutes'] <= 90 else 'mismatch'


def load_profiles(directory):
    directory = Path(directory)
    if not directory.is_dir():
        raise ValueError(f"スキルシートフォルダがありません: {directory}")
    paths = sorted((p for p in directory.rglob('*') if p.is_file() and p.suffix.lower() == '.pdf'),
                   key=lambda p: p.relative_to(directory).as_posix())
    if not paths:
        return []
    try:
        from pypdf import PdfReader
    except ImportError:
        raise ValueError("PDFの読み取りには python -m pip install -r requirements.txt が必要です。") from None
    profiles = []
    for path in paths:
        name = path.relative_to(directory).as_posix()
        try:
            reader = PdfReader(path)
            pages = [page.extract_text() or '' for page in reader.pages]
            if not pages or any(not page.strip() for page in pages):
                raise ValueError("文字を読み取れないページがあります。OCR済みPDFに差し替えてください。")
        except Exception as error:
            raise ValueError(f"スキルシートを読めません: {name} ({error})") from None
        profiles.append({"file": name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                         "text": '\n\n'.join(pages)})
    return profiles


def validate(answer, items):
    try:
        records = json.loads(answer)['records']
        expected = {item['id'] for item in items}
        if not isinstance(records, list) or len(records) != len(expected):
            raise ValueError
        seen = set()
        for record in records:
            if (record['id'] not in expected or record['id'] in seen
                    or record['state'] not in {'match', 'mismatch', 'unknown'}
                    or not isinstance(record['evidence'], str) or not record['evidence'].strip()):
                raise ValueError
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
    except (ValueError, TypeError, KeyError):
        raise ValueError('エンジニア適合判定の件数・ID・根拠が不正です。') from None
