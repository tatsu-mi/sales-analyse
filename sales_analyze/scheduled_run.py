"""Prepare an HTML report, post its link to Teams, then commit the checkpoint."""
import argparse
import json
import os
import re
import shutil
import sys
import time
from email.utils import parsedate_to_datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from . import mail_analyzer as app
from . import prioritization

# Leave room below Teams' 28 KB limit, including the JSON/card envelope.
MAX_CARD_BYTES = 24_000


def validate_report_url(report_url):
    parsed = urlparse(report_url)
    if (parsed.scheme != "https" or not parsed.netloc
            or any(char in report_url for char in "\r\n ()")):
        raise app.ToolError("レポートのHTTPS URLを指定してください。")


def card_payload(report_url, ranked=None, rules=None):
    validate_report_url(report_url)
    sections = [f"[メール分析レポートを開く]({report_url})"]
    if ranked is not None:
        for label, entries in prioritization.report_sections(ranked, rules):
            lines = [f"{label}（上位{rules['top_n']}件まで）"]
            for entry in entries:
                title = str(entry['latest']['title'] or '不明').replace('\r', ' ').replace('\n', ' ')
                # Keep project titles literal rather than interpreting Markdown links/emphasis.
                title = re.sub(r'([\\`*_{}\[\]()#+.!|>~-])', r'\\\1', title)
                lines.append(f"{entry['rank']}. {title}")
            if not entries:
                lines.append("該当候補はありません。")
            sections.append('\n\n'.join(lines))
    return text_card_payload('\n\n'.join(sections))


def text_card_payload(text):
    card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard", "version": "1.2",
        "body": [{"type": "TextBlock", "text": text, "wrap": True}],
    }
    return json.dumps({"type": "message", "attachments": [{
        "contentType": "application/vnd.microsoft.card.adaptive",
        "contentUrl": None, "content": card,
    }]}, ensure_ascii=False).encode("utf-8")


def send_card(payload, webhook_url, timeout):
    for attempt in range(4):
        retry_after = None
        request = Request(webhook_url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(request, timeout=timeout) as response:
                status = response.status
                # Legacy connectors sometimes return an error with HTTP 200.
                body = response.read(4096).decode("utf-8", errors="replace").strip()
                if "Microsoft Teams endpoint returned HTTP error 429" in body:
                    retry_after = ""
                elif not 200 <= status < 300 or (status == 200 and body not in ("", "1")):
                    raise app.ToolError("Teams投稿が受理されませんでした。Teams Workflowsの実行履歴を確認してください。")
                else:
                    return
        except HTTPError as error:
            if error.code != 429:
                raise app.ToolError(f"Teams投稿に失敗しました（HTTP {error.code}）。") from None
            retry_after = error.headers.get("Retry-After", "") if error.headers else ""
            error.close()
        except (URLError, OSError, ValueError):
            # Never expose the signed webhook URL or response containing report data.
            raise app.ToolError("Teams投稿の通信に失敗しました。") from None
        if attempt == 3:
            raise app.ToolError("Teamsの投稿制限が解消しませんでした。チェックポイントは更新しません。")
        wait = 2 ** (attempt + 1)
        if retry_after:
            try:
                wait = int(retry_after)
            except ValueError:
                try:
                    wait = max(0, parsedate_to_datetime(retry_after).timestamp() - time.time())
                except (ValueError, TypeError, OverflowError):
                    pass
        if wait > 60:
            raise app.ToolError("Teamsの再試行待ち時間が60秒を超えています。後で再実行してください。")
        time.sleep(max(1, wait))


def report_blocks(report):
    """Keep each project intact; ignore heading-like text in verbatim emails."""
    blocks, current = [], []
    fence = None
    has_project = False
    for line in report.splitlines(keepends=True):
        if fence is None:
            category = line.startswith('## ')
            project = line.startswith('### ')
            if current and (category or (project and has_project)):
                blocks.append(''.join(current))
                current = []
                has_project = False
            has_project = has_project or project
            opening = re.match(r'^(`{3,}|~{3,})', line)
            if opening:
                fence = opening[1]
        elif re.fullmatch(re.escape(fence[0]) + '{' + str(len(fence)) + r',}[ \t]*[\r\n]*', line):
            fence = None
        current.append(line)
    if current:
        blocks.append(''.join(current))
    return blocks


def report_card_payloads(summary, report, title='メール分析レポート'):
    # Reserve bytes for numbering so chunking uses the serialized UTF-8 payload size.
    prefix = f'{title}（0000000000/0000000000）\n\n'

    def fits(text):
        return len(text_card_payload(prefix + text)) <= MAX_CARD_BYTES

    chunks, current = [], ''
    has_section = False
    for block in [summary + '\n\n', *report_blocks(report)]:
        if block.startswith('## '):
            # Each skill sheet ends its own post.
            if has_section and current:
                chunks.append(current)
                current = ''
            has_section = True
        if fits(current + block):
            current += block
            continue
        if current:
            chunks.append(current)
            current = ''
        # Split within a project only when that project cannot fit in a fresh card.
        while not fits(block):
            low, high = 0, len(block)
            while low < high:
                middle = (low + high + 1) // 2
                if fits(block[:middle]):
                    low = middle
                else:
                    high = middle - 1
            if low == 0:
                raise app.ToolError('Teams投稿のサイズ上限が小さすぎます。')
            # Prefer a line boundary, falling back to a character boundary for long lines.
            newline = block.rfind('\n', 0, low)
            cut = newline + 1 if newline >= low // 2 else low
            chunks.append(block[:cut])
            block = block[cut:]
        current = block
    if current:
        chunks.append(current)
    return [text_card_payload(f'{title}（{index}/{len(chunks)}）\n\n' + chunk)
            for index, chunk in enumerate(chunks, 1)]


def post_report(report_url, webhook_url, timeout, ranked=None, rules=None, report=None):
    summary_payload = card_payload(report_url, ranked, rules)
    if report is None:
        payloads = [summary_payload]
    else:
        summary = json.loads(summary_payload)['attachments'][0]['content']['body'][0]['text']
        # Data files remain available in the saved artifact, not as relative Teams links.
        report = prioritization.transform_report_text(report, lambda text: re.sub(
            r'\]\((?:extracted_emails\.jsonl|opportunities\.jsonl|statistics\.json|ranked_emails\.json|priority_rules\.json)\)',
            lambda match: f']({report_url})', text))
        payloads = report_card_payloads(summary, report)
    send_report_cards(payloads, webhook_url, timeout)


def send_report_cards(payloads, webhook_url, timeout):
    for index, payload in enumerate(payloads):
        if index:
            time.sleep(10)
        send_card(payload, webhook_url, timeout)


def prepare(config_path, checkpoint_dir, publish_dir, *, resume_dir=None, progress=None, run_period=None):
    config_path = config_path.resolve()
    config = app.load_config(config_path)
    app.configure_logging(config)
    webhook = os.environ.get("TEAMS_WEBHOOK_URL", "").strip()
    if urlparse(webhook).scheme != "https" or not urlparse(webhook).netloc:
        raise app.ToolError("TEAMS_WEBHOOK_URL にTeams WorkflowsのHTTPS URLを設定してください。")
    for name in ("SAKURA_MAIL_HOST", "SAKURA_MAIL_USERNAME", "SAKURA_MAIL_PASSWORD", "GEMINI_API_KEY"):
        if not os.environ.get(name, "").strip():
            raise app.ToolError(f"GitHub Secretsの {name} を設定してください。")
    root = Path(config["analysis"]["output_dir"])
    if not root.is_absolute():
        root = config_path.parent / root
    state_path = app.last_run_path(root, config)
    checkpoint = checkpoint_dir / "checkpoint.json"
    if checkpoint.exists():
        saved = app.read_metadata(checkpoint)
        if saved.get("mailbox") != state_path.stem:
            raise app.ToolError("保存済みチェックポイントとメールボックスが異なります。新しいメールボックス用に状態を初期化してください。")
        # Validate before replacing a valid local boundary with restored data.
        try:
            boundary = app.datetime.fromisoformat(saved["last_run_at"])
            if boundary.tzinfo is None or boundary > app.datetime.now(app.JST):
                raise ValueError
        except (KeyError, ValueError, TypeError):
            raise app.ToolError("保存済みチェックポイントの時刻が不正、または未来です。") from None
        app.record_last_run(state_path, boundary)
    # Validate even before running; invalid/future checkpoints must not cause a fallback.
    app.incremental_period(config["analysis"], state_path, app.datetime.now(app.JST))
    args = SimpleNamespace(config=str(config_path), start=None, end=None, fetch_only=False,
                           emails_file=None, refresh=False, log_file=None, resume_dir=resume_dir,
                           progress=progress, run_period=run_period)
    previous_state = state_path.read_bytes() if state_path.exists() else None
    try:
        output = app.run(args)
    finally:
        # app.run commits analysis locally; defer that commit until delivery.
        if previous_state is None:
            state_path.unlink(missing_ok=True)
        else:
            state_path.write_bytes(previous_state)
    if output is None:
        raise app.ToolError("新しい取得期間がありません。チェックポイントは更新しません。")
    report = (output / "report.html").read_text(encoding="utf-8")
    if not report.strip():
        raise app.ToolError("レポートが空のため処理を停止します。")
    publish_dir.mkdir(parents=True, exist_ok=True)
    for name in ("report.html", "ranked_emails.json", "priority_rules.json"):
        shutil.copy2(output / name, publish_dir / name)
    statistics = output / 'statistics.json'
    if statistics.is_file():
        shutil.copy2(statistics, publish_dir / statistics.name)
    assessments = output / 'engineer_assessments.json'
    if assessments.is_file():
        shutil.copy2(assessments, publish_dir / assessments.name)
    metadata = app.read_metadata(output / "metadata.json")
    # Uploading this pending state as a delivered checkpoint is never allowed.
    app.analysis_pipeline.atomic_json(checkpoint_dir / "pending.json", {
        "mailbox": state_path.stem, "last_run_at": metadata["stop_at"],
    })
    app.LOGGER.info("レポートを準備しました。レポート保存・Teams通知待ちです。")


def notify(config_path, checkpoint_dir, publish_dir, report_url, *, link_only=False):
    validate_report_url(report_url)
    config_path = config_path.resolve()
    config = app.load_config(config_path)
    app.configure_logging(config)
    webhook = os.environ.get("TEAMS_WEBHOOK_URL", "").strip()
    if urlparse(webhook).scheme != "https" or not urlparse(webhook).netloc:
        raise app.ToolError("TEAMS_WEBHOOK_URL にTeams WorkflowsのHTTPS URLを設定してください。")
    root = Path(config["analysis"]["output_dir"])
    if not root.is_absolute():
        root = config_path.parent / root
    state_path = app.last_run_path(root, config)
    pending = app.read_metadata(checkpoint_dir / "pending.json")
    try:
        boundary = app.datetime.fromisoformat(pending["last_run_at"])
        if (pending["mailbox"] != state_path.stem or boundary.tzinfo is None
                or boundary > app.datetime.now(app.JST)):
            raise ValueError
    except (KeyError, ValueError, TypeError):
        raise app.ToolError("通知待ちレポートの状態が不正です。") from None
    report = (publish_dir / "report.html").read_text(encoding="utf-8")
    if not report.strip():
        raise app.ToolError("レポートが空のため通知を停止します。")
    post_report(report_url, webhook, config["teams"]["timeout_seconds"])
    app.analysis_pipeline.atomic_json(checkpoint_dir / "checkpoint.json", pending)
    app.record_last_run(state_path, boundary)
    (checkpoint_dir / "pending.json").unlink()
    app.LOGGER.info("Teamsへの通知が受理されました。チェックポイントを更新しました。")


def run(config_path, checkpoint_dir, publish_dir, report_url, *, link_only=False):
    validate_report_url(report_url)
    prepare(config_path, checkpoint_dir, publish_dir)
    notify(config_path, checkpoint_dir, publish_dir, report_url, link_only=link_only)


def send_test_notification(config_path):
    config = app.load_config(config_path.resolve())
    app.configure_logging(config)
    webhook = os.environ.get("TEAMS_WEBHOOK_URL", "").strip()
    if urlparse(webhook).scheme != "https" or not urlparse(webhook).netloc:
        raise app.ToolError("TEAMS_WEBHOOK_URL にTeams WorkflowsのHTTPS URLを設定してください。")
    payload = text_card_payload("【テスト投稿】ローカル環境からTeamsへの接続確認です。")
    send_card(payload, webhook, config["teams"]["timeout_seconds"])
    app.LOGGER.info("Teams Webhookがテスト投稿を受理しました。")


def send_test_report(config_path, report_path):
    config = app.load_config(config_path.resolve())
    app.configure_logging(config)
    webhook = os.environ.get("TEAMS_WEBHOOK_URL", "").strip()
    if urlparse(webhook).scheme != "https" or not urlparse(webhook).netloc:
        raise app.ToolError("TEAMS_WEBHOOK_URL にTeams WorkflowsのHTTPS URLを設定してください。")
    try:
        report = report_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        raise app.ToolError("指定したレポートをUTF-8で読み込めません。ファイルのパスと文字コードを確認してください。") from None
    if not report.strip():
        raise app.ToolError("レポートが空のためテスト投稿を停止します。")
    payloads = report_card_payloads("ローカルのreport.mdからのテスト投稿です。", report,
                                   title="【テスト投稿】メール分析レポート")
    send_report_cards(payloads, webhook, config["teams"]["timeout_seconds"])
    app.LOGGER.info("Teams Webhookがレポートのテスト投稿（%s件）を受理しました。", len(payloads))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.toml", type=Path)
    parser.add_argument("--checkpoint-dir", default=".gha-state", type=Path)
    parser.add_argument("--publish-dir", default=".gha-report", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--prepare-only", action="store_true", help="分析と通知待ち状態の保存のみ")
    mode.add_argument("--notify-only", action="store_true", help="レポート保存後にTeamsへ通知し時刻を更新")
    mode.add_argument("--test-notification", action="store_true", help="メール取得・分析・時刻更新なしでTeamsへテスト文を投稿")
    mode.add_argument("--test-report", type=Path, metavar="REPORT_MD", help="ローカルのreport.mdをTeamsへ分割テスト投稿（メール取得・分析・時刻更新なし）")
    parser.add_argument("--report-url", default=os.environ.get("REPORT_URL", ""), help="保存済みレポートのHTTPS URL")
    parser.add_argument("--link-only", action="store_true", help="Teamsにはレポートのリンクだけを1件投稿")
    args = parser.parse_args()
    app.configure_logging({})
    try:
        if args.test_notification:
            send_test_notification(args.config)
        elif args.test_report is not None:
            send_test_report(args.config, args.test_report)
        elif args.prepare_only:
            prepare(args.config, args.checkpoint_dir, args.publish_dir)
        elif args.notify_only:
            notify(args.config, args.checkpoint_dir, args.publish_dir, args.report_url, link_only=args.link_only)
        else:
            run(args.config, args.checkpoint_dir, args.publish_dir, args.report_url, link_only=args.link_only)
    except (app.ToolError, OSError, ValueError) as error:
        # Error messages from configuration validation are safe; webhook/transport
        # exceptions are already translated by post_report.
        app.LOGGER.error("エラー: %s", error if isinstance(error, app.ToolError) else "設定・状態・出力ファイルを確認してください。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
