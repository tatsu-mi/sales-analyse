"""Evidence-based assessments and deterministic, per-mail shortlists."""
import re
from . import title_filter
from pathlib import Path

KINDS = ("ses",)
STATES = ("match", "mismatch", "unknown")


def load_rules(path):
    path = Path(path)
    if path.suffix.lower() != '.md':
        raise ValueError('判定条件はMarkdown（.md）で指定してください。')
    rules = parse_markdown_rules(path.read_text(encoding='utf-8-sig'))
    return validate_rules(rules)


def parse_markdown_rules(text):
    """Read explicit Markdown sections without interpreting rules through an AI."""
    text = re.sub(r'<!--.*?-->', '', text, flags=re.DOTALL)
    sections, labels, current = {}, {}, None
    names = {'表示件数': 'top_n', '件名選別': 'prefilter',
             '優先して残す件名': 'keep_patterns', '除外する件名': 'exclude_patterns',
             '件名選別への指示': 'ai_instructions'}
    for line in text.splitlines():
        if line.startswith('## '):
            heading = line[3:].strip()
            match = re.fullmatch(r'(SES|受託)[:：]\s*(.+)', heading)
            if match:
                current = 'ses' if match[1] == 'SES' else 'contract'
                labels[current] = match[2].strip()
            else:
                current = names.get(heading)
            if current is None or current in sections:
                raise ValueError('判定条件の見出しに未対応の名前または重複があります。')
            sections[current] = []
        elif line.startswith('#') and not (current is None and line.startswith('# ')):
            raise ValueError('判定条件の見出しは ## で指定してください。')
        elif current is not None:
            sections[current].append(line)
    required = {'top_n', 'ses', 'prefilter', 'keep_patterns',
                'exclude_patterns', 'ai_instructions'}
    if set(sections) - {'contract'} != required:
        raise ValueError('判定条件の必須見出しが不足しています。サンプルを確認してください。')

    def bullets(key):
        items = []
        for line in sections[key]:
            if not line.strip():
                continue
            if line.startswith('- ') and line[2:].strip():
                items.append(line[2:].strip())
            elif line.startswith('  ') and items and key in KINDS:
                items[-1] += ' ' + line.strip()
            else:
                raise ValueError('条件・件名パターン・件名選別設定は - で始まる箇条書きにしてください。')
        return items

    limit = '\n'.join(sections['top_n']).strip()
    if not re.fullmatch(r'[0-9]+', limit):
        raise ValueError('表示件数は1以上10以下の整数で指定してください。')
    settings, seen = {}, set()
    setting_names = {'機械選別': 'enabled', 'AI選別': 'ai_enabled', '一括処理件数': 'batch_size'}
    for item in bullets('prefilter'):
        match = re.fullmatch(r'([^:：]+)[:：]\s*(.+)', item)
        key = setting_names.get(match[1].strip()) if match else None
        if key is None or key in seen:
            raise ValueError('件名選別の設定名に未対応の名前または重複があります。')
        seen.add(key)
        value = match[2].strip()
        if key == 'batch_size':
            if not re.fullmatch(r'[0-9]+', value):
                raise ValueError('一括処理件数は1以上100以下の整数で指定してください。')
            settings[key] = int(value)
        else:
            if value not in ('有効', '無効'):
                raise ValueError('機械選別・AI選別は有効または無効で指定してください。')
            settings[key] = value == '有効'
    if seen != set(setting_names.values()):
        raise ValueError('件名選別の設定が不足しています。')
    for key in ('keep_patterns', 'exclude_patterns'):
        settings[key] = [item[1:-1] if item.startswith('`') and item.endswith('`') else item
                         for item in bullets(key)]
    settings['ai_instructions'] = '\n'.join(sections['ai_instructions']).strip()
    return dict(top_n=int(limit), prefilter=settings,
                **{kind: dict(label=labels[kind], criteria=bullets(kind)) for kind in KINDS})


def validate_rules(rules):
    if set(rules) - {"top_n", "prefilter", "ses", "contract"}:
        raise ValueError("判定条件に未対応のセクション・設定名があります。")
    if type(rules.get("top_n")) is not int or not 1 <= rules["top_n"] <= 10:
        raise ValueError("top_n は1以上10以下の整数で指定してください。")
    for kind in KINDS:
        section = rules.get(kind)
        if isinstance(section, dict) and set(section) - {"label", "criteria"}:
            raise ValueError(f"{kind} に未対応の設定名があります。")
        if (not isinstance(section, dict) or not isinstance(section.get("label"), str)
                or not section["label"].strip() or not isinstance(section.get("criteria"), list)
                or not section["criteria"]
                or not all(isinstance(item, str) and item.strip() for item in section["criteria"])):
            raise ValueError(f"{kind} のlabelとcriteriaを設定してください。")
    title_filter.validate_settings(rules.get("prefilter", {}))
    return {key: value for key, value in rules.items() if key != "contract"}


def validate_assessments(opportunity, rules):
    kind = opportunity["kind"]
    checks = opportunity["assessments"]
    expected = len(rules[kind]["criteria"]) if kind in KINDS else 0
    if not isinstance(checks, list) or len(checks) != expected:
        raise ValueError("条件判定の件数が不正です。")
    for index, check in enumerate(checks):
        if (not isinstance(check, dict) or type(check.get("criterion")) is not int
                or check["criterion"] != index + 1 or check.get("state") not in STATES):
            raise ValueError("条件判定の順序・状態・根拠が不正です。")


def shortlist(values, rules):
    """One mail per category, represented by its best eligible project."""
    by_kind = {kind: {} for kind in KINDS}
    for value in values:
        kind, latest = value["kind"], value["latest"]
        if (kind not in KINDS or latest["status"] == "closed"
                or value["expired_at_period_end"]):
            continue
        validate_assessments(latest, rules)
        checks = latest["assessments"]
        if any(check["state"] == "mismatch" for check in checks):
            continue
        matches = sum(check["state"] == "match" for check in checks)
        # Explicitly open, then recent mail, then stable UID are tie breakers.
        engineer_state = value.get("engineer_match", {}).get("state", "match")
        skill_checks = value.get('engineer_match', {}).get('requirements', [])
        coverage = sum(check['state'] == 'match' for check in skill_checks) / len(skill_checks) if skill_checks else 0
        score = (engineer_state == "match", coverage, matches, latest["status"] == "open", value["received_at"], value["uid"])
        entry = dict(value, matches=matches, total_criteria=len(checks),
                     verification="適合" if matches == len(checks) and engineer_state == "match" else "要確認")
        previous = by_kind[kind].get(value["uid"])
        if previous is None or score > previous[0]:
            by_kind[kind][value["uid"]] = (score, entry)
    return {kind: [dict(entry, rank=index) for index, (_, entry) in enumerate(
        sorted(by_kind[kind].values(), key=lambda item: item[0], reverse=True)[:rules["top_n"]], 1)]
        for kind in KINDS}


def transform_report_text(report, transform):
    """Apply link rewrites outside verbatim source-mail blocks only."""
    parts, start = [], 0
    for match in re.finditer(r"(?ms)^(`{3,})text\n.*?^\1[ \t]*(?=\n|$)", report):
        parts.extend((transform(report[start:match.start()]), match[0]))
        start = match.end()
    parts.append(transform(report[start:]))
    return "".join(parts)


def render_source_mail(mail):
    """Keep source text literal, including whitespace and Markdown-like content."""
    content = (f"件名: {mail.get('subject', '')}\n"
               f"受信日時: {mail.get('received_at') or '不明'}\n"
               f"送信元: {mail.get('from', '')}\n"
               f"宛先: {mail.get('to', '')}\n\n" + mail["body"])
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    fence = "`" * max(3, max((len(run) + 1 for run in re.findall(r"`+", content)), default=3))
    note = "\n\n※取得時の文字数上限により、参照元メールの本文は一部省略されています。" if mail.get("body_truncated") else ""
    return "参照元メール\n\n" + fence + "text\n" + content + "\n" + fence + note


def report_sections(ranked, rules):
    if "ses_by_engineer" in ranked:
        if not ranked["ses_by_engineer"]:
            yield rules["ses"]["label"] + "（エンジニア未登録）", []
        for group in ranked["ses_by_engineer"]:
            name = re.sub(r'([\\`*_{}\[\]()#+.!|>~-])', r'\\\1', group['file'].replace('\n', ' ').replace('\r', ' '))
            yield f"{rules['ses']['label']} / {name}", group["entries"]
    else:
        yield rules["ses"]["label"], ranked["ses"]


def render_report(ranked, rules):
    def text(value):
        return str(value or "不明").replace("\n", " ").replace("\r", " ").replace("|", "\\|")

    lines = ["## 案件タイトルサマリー", ""]
    for section_index, (label, entries) in enumerate(report_sections(ranked, rules), 1):
        lines += [f"**{label}（上位{rules['top_n']}件まで）**", ""]
        for entry_index, entry in enumerate(entries, 1):
            title = str(entry['latest']['title'] or '不明').replace('\r', ' ').replace('\n', ' ')
            title = re.sub(r'([\\`*_{}\[\]()#+.!|>~-])', r'\\\1', title)
            lines.append(f"{entry['rank']}. [{title}](#project-{section_index}-{entry_index})")
        if not entries:
            lines.append("該当候補はありません。")
        lines.append("")

    order = ("共通条件の適合数、募集中の明記、受信日時の新しさを判定します。" if 'ses_by_engineer' in ranked else
             "適合条件数、募集中の明記、受信日時の新しさの順で選定します。")
    lines += ["条件に明確な不適合がある案件、募集終了・期限切れ案件、その他メールは優先対象から除外します。",
             order + "不明条件は要確認として扱います。",
             "同じメールに複数案件がある場合は、カテゴリごとに最も適合する案件で代表します。", ""]
    if "ses_by_engineer" in ranked:
        lines += [f"個人別の案件は適合確認済みを優先し、各判定の中で要件ごとの重みの合計点数順に選定します。要確認も含めて合計で上位{rules['top_n']}件までとします。基準点を加点対象の項目数で等分し、一致した項目の経験係数を掛けて合算します。実務以外の加点は実務の0.1倍とし、同義語や同じ経験の重複加点は行いません。", ""]
        lines += ["priority_rules.mdのSES条件は全エンジニアに共通で適用し、engineers.mdの各人のスキル・通勤条件を追加判定します。", ""]
        lines += ["必須条件・共通条件・通勤条件を全て確認できた案件だけを適合確認済みとします。各案件に適合・要確認の判定を表示します。同じ案件が複数エンジニアの候補に入る場合があります。", ""]
        lines += ["勤務地は最寄り駅から片道90分以内、または出社不要のフルリモートを必須とします。通勤時間が確認できない案件は要確認です。", ""]
    for section_index, (label, entries) in enumerate(report_sections(ranked, rules), 1):
        lines += [f"## {label}（上位{rules['top_n']}件まで）", ""]
        if not entries:
            lines += ["該当候補はありません。", ""]
        for entry_index, entry in enumerate(entries, 1):
            project = entry["latest"]
            lines += [f"### {entry['rank']}位: {text(project['title'])} {{#project-{section_index}-{entry_index}}}", "",
                      f"判定: {entry['verification']}（共通条件の適合 {entry['matches']}/{entry['total_criteria']}）  ",
                      f"送信元: {text(entry['sender'])} / 問い合わせ先: {text(project['contact'])}  ",
                      f"単価・予算: {text(project['budget'])} / 開始・期間: {text(project['start'])}  ",
                      f"勤務地・勤務条件: {text(project['location'])} / スキル: {text(', '.join(project['skills']))}  ",
                      f"商流・制約: {text(project['contract'])} / 懸念: {text(project['notes'])}", ""]
            if "engineer_match" in entry:
                lines += [f"エンジニア情報との照合: {text(entry['engineer_match']['evidence'])}", ""]
                match = entry['engineer_match']
                if 'score' in match:
                    lines += [f"マッチング得点: {match['score']:g}点", "",
                              "| 加点対象 | 種類 | スキル | 基準点 | 該当数/対象数 | 経験係数 | 得点 |",
                              "| --- | --- | --- | --- | --- | --- | --- |"]
                    for part in match.get('score_breakdown', []):
                        coverage = f"{part['matched_count']}/{part['target_count']}" if 'target_count' in part else '—'
                        lines.append(f"| {text(part.get('label', part['requirement']))} | {text(part['kind'])} | {text(', '.join(part.get('keyword_names', part['keywords'])))} | {part['weight']} | {coverage} | {part['factor']:g} | {part['points']:g} |")
                    lines.append('')
                checks = entry['engineer_match'].get('requirements', [])
                lines += [f"必須条件の適合: {sum(check['state'] == 'match' for check in checks)}/{len(checks)}（一致率・精度の評価値ではありません）", "",
                          "| 必須条件 | 判定 | キーワード | 理由・確認事項 |",
                          "| --- | --- | --- | --- |"]
                states = {'match': '適合', 'mismatch': '不適合', 'unknown': '要確認'}
                requirements = project.get('mandatory_requirements', [])
                for requirement, check in zip(requirements, checks):
                    lines.append(f"| {text(requirement['requirement'])} | {states[check['state']]} | {text(check.get('keywords', ''))} | {text(check['reason'])} |")
                if not checks:
                    lines += ["", "必須条件を特定できないため要確認です。"]
                lines.append('')
                commute = entry['engineer_match'].get('commute')
                if commute:
                    location = ("通勤条件を確認できません" if commute['minutes'] < 0 and commute['mode'] != 'full_remote' else
                                "フルリモート（出社不要）" if commute['mode'] == 'full_remote' else
                                f"{text(commute['nearest_station'])} → {text(commute['workplace'])} / 片道{commute['minutes']}分 / {text(commute['route'])}")
                    lines += [f"通勤条件: {location}  ", f"通勤の根拠: {text(commute['evidence'])}", ""]
            pending = (any(check["state"] == "unknown" for check in project["assessments"])
                       or entry.get("engineer_match", {}).get("state") == "unknown")
            action = "未確認の案件条件を問い合わせる" if pending else "募集継続・参画体制を確認してアプローチする"
            lines += ["", f"次の対応: {action}。", "",
                      render_source_mail(entry['source_email']), ""]
    return "\n".join(lines)
