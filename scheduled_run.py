"""Prepare a report, notify Teams with its artifact link, then commit the checkpoint."""
import argparse
import json
import os
import shutil
import sys
import time
from email.utils import parsedate_to_datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import mail_analyzer as app


def validate_report_url(report_url):
    parsed = urlparse(report_url)
    if (parsed.scheme != "https" or not parsed.netloc
            or any(char in report_url for char in "\r\n ()")):
        raise app.ToolError("レポートのHTTPS URLを指定してください。")


def card_payload(report_url):
    validate_report_url(report_url)
    card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard", "version": "1.2",
        "body": [{"type": "TextBlock", "text": f"[メール分析レポートを開く]({report_url})", "wrap": True}],
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


def post_report(report_url, webhook_url, timeout):
    send_card(card_payload(report_url), webhook_url, timeout)


def prepare(config_path, checkpoint_dir, publish_dir):
    config_path = config_path.resolve()
    config = app.load_config(config_path)
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
                           emails_file=None, refresh=False, log_file=None)
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
    report = (output / "report.md").read_text(encoding="utf-8")
    if not report.strip():
        raise app.ToolError("レポートが空のため処理を停止します。")
    publish_dir.mkdir(parents=True, exist_ok=True)
    for name in ("report.md", "ranked_emails.json", "priority_rules.json"):
        shutil.copy2(output / name, publish_dir / name)
    metadata = app.read_metadata(output / "metadata.json")
    # Uploading this pending state as a delivered checkpoint is never allowed.
    app.analysis_pipeline.atomic_json(checkpoint_dir / "pending.json", {
        "mailbox": state_path.stem, "last_run_at": metadata["stop_at"],
    })
    print("レポートを準備しました。Artifact保存・Teams通知待ちです。", flush=True)


def notify(config_path, checkpoint_dir, publish_dir, report_url):
    validate_report_url(report_url)
    config_path = config_path.resolve()
    config = app.load_config(config_path)
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
    if not (publish_dir / "report.md").read_text(encoding="utf-8").strip():
        raise app.ToolError("レポートが空のため通知を停止します。")
    post_report(report_url, webhook, config["teams"]["timeout_seconds"])
    app.analysis_pipeline.atomic_json(checkpoint_dir / "checkpoint.json", pending)
    app.record_last_run(state_path, boundary)
    (checkpoint_dir / "pending.json").unlink()
    print("Teamsへのレポートリンク送信が受理されました。チェックポイントを更新しました。", flush=True)


def run(config_path, checkpoint_dir, publish_dir, report_url):
    validate_report_url(report_url)
    prepare(config_path, checkpoint_dir, publish_dir)
    notify(config_path, checkpoint_dir, publish_dir, report_url)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.toml", type=Path)
    parser.add_argument("--checkpoint-dir", default=".gha-state", type=Path)
    parser.add_argument("--publish-dir", default=".gha-report", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--prepare-only", action="store_true", help="分析と通知待ち状態の保存のみ")
    mode.add_argument("--notify-only", action="store_true", help="Artifact保存後にリンクを通知し時刻を更新")
    parser.add_argument("--report-url", default=os.environ.get("REPORT_URL", ""), help="保存済みレポートArtifactのHTTPS URL")
    args = parser.parse_args()
    try:
        if args.prepare_only:
            prepare(args.config, args.checkpoint_dir, args.publish_dir)
        elif args.notify_only:
            notify(args.config, args.checkpoint_dir, args.publish_dir, args.report_url)
        else:
            run(args.config, args.checkpoint_dir, args.publish_dir, args.report_url)
    except (app.ToolError, OSError, ValueError) as error:
        # Error messages from configuration validation are safe; webhook/transport
        # exceptions are already translated by post_report.
        print(f"エラー: {error}" if isinstance(error, app.ToolError) else "エラー: 設定・状態・出力ファイルを確認してください。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
