"""Evidence-based assessments and deterministic, per-mail shortlists."""
import tomllib
import re
import title_filter
from pathlib import Path

KINDS = ("ses", "contract")
STATES = ("match", "mismatch", "unknown")


def load_rules(path):
    with Path(path).open("rb") as file:
        rules = tomllib.load(file)
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
    return rules


def validate_assessments(opportunity, rules):
    kind = opportunity["kind"]
    checks = opportunity["assessments"]
    expected = len(rules[kind]["criteria"]) if kind in KINDS else 0
    if not isinstance(checks, list) or len(checks) != expected:
        raise ValueError("条件判定の件数が不正です。")
    for index, check in enumerate(checks):
        if (not isinstance(check, dict) or type(check.get("criterion")) is not int
                or check["criterion"] != index + 1 or check.get("state") not in STATES
                or not isinstance(check.get("evidence"), str)
                or (check["state"] != "unknown" and not check["evidence"].strip())):
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
            yield f"{rules['ses']['label']} / {name} / 適合確認済み", group["entries"]
            if 'pending_entries' in group:
                yield f"{rules['ses']['label']} / {name} / 要確認（推薦対象外）", group['pending_entries']
    else:
        yield rules["ses"]["label"], ranked["ses"]
    yield rules["contract"]["label"], ranked["contract"]


def render_report(ranked, rules):
    def text(value):
        return str(value or "不明").replace("\n", " ").replace("\r", " ").replace("|", "\\|")

    lines = ["## 案件タイトルサマリー", ""]
    for label, entries in report_sections(ranked, rules):
        lines += [f"**{label}（上位{rules['top_n']}件まで）**", ""]
        for entry in entries:
            title = str(entry['latest']['title'] or '不明').replace('\r', ' ').replace('\n', ' ')
            title = re.sub(r'([\\`*_{}\[\]()#+.!|>~-])', r'\\\1', title)
            lines.append(f"{entry['rank']}. {title}")
        if not entries:
            lines.append("該当候補はありません。")
        lines.append("")

    lines += ["条件に明確な不適合がある案件、募集終了・期限切れ案件、その他メールは優先対象から除外します。",
             "適合条件数、募集中の明記、受信日時の新しさの順で選定します。不明条件は要確認として順位を下げます。",
             "同じメールに複数案件がある場合は、カテゴリごとに最も適合する案件で代表します。", ""]
    if "ses_by_engineer" in ranked:
        lines += ["priority_rules.tomlのSES条件は全エンジニアに共通で適用し、engineers.mdの各人のスキル・通勤条件を追加判定します。", ""]
        lines += ["必須条件・共通条件・通勤条件を全て確認できた案件だけを適合確認済みとします。要確認は別枠で表示し、適合確認済みの件数を埋めるためには使いません。同じ案件が複数エンジニアの候補に入る場合があります。", ""]
        lines += ["勤務地は最寄り駅から片道90分以内、または出社不要のフルリモートを必須とします。通勤時間が確認できない案件は要確認です。", ""]
    for label, entries in report_sections(ranked, rules):
        lines += [f"## {label}（上位{rules['top_n']}件まで）", ""]
        if not entries:
            lines += ["該当候補はありません。", ""]
        for entry in entries:
            project = entry["latest"]
            lines += [f"### {entry['rank']}位: {text(project['title'])}", "",
                      f"判定: {entry['verification']}（共通条件の適合 {entry['matches']}/{entry['total_criteria']}）  ",
                      render_source_mail(entry["source_email"]), "",
                      f"送信元: {text(entry['sender'])} / 問い合わせ先: {text(project['contact'])}  ",
                      f"単価・予算: {text(project['budget'])} / 開始・期間: {text(project['start'])}  ",
                      f"勤務地・勤務条件: {text(project['location'])} / スキル: {text(', '.join(project['skills']))}  ",
                      f"商流・制約: {text(project['contract'])} / 懸念: {text(project['notes'])}", ""]
            if "engineer_match" in entry:
                lines += [f"エンジニア情報との照合: {text(entry['engineer_match']['evidence'])}", ""]
                checks = entry['engineer_match'].get('requirements', [])
                lines += [f"必須条件の適合: {sum(check['state'] == 'match' for check in checks)}/{len(checks)}（一致率・精度の評価値ではありません）", "",
                          "| 必須条件 | 判定 | 案件の原文 | エンジニアの原文 | 理由・確認事項 |",
                          "| --- | --- | --- | --- | --- |"]
                states = {'match': '適合', 'mismatch': '不適合', 'unknown': '要確認'}
                requirements = project.get('mandatory_requirements', [])
                for requirement, check in zip(requirements, checks):
                    lines.append(f"| {text(requirement['requirement'])} | {states[check['state']]} | {text(check['project_quote'])} | {text(check['engineer_quote'])} | {text(check['reason'])} |")
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
            lines += ["", f"次の対応: {action}。", ""]
    return "\n".join(lines)
