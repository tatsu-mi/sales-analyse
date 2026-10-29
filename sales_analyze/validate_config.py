"""Offline configuration validation. Never load .env or call services."""
import argparse
from pathlib import Path
import sys

from . import mail_analyzer as app
from . import prioritization


def validate(paths, *, structure_only=False):
    errors = 0
    for path in paths:
        try:
            if path.suffix.lower() == '.md':
                prioritization.load_rules(path)
            else:
                app.load_config(path, load_environment=False,
                                load_priority_rules=not structure_only)
        except (app.ToolError, OSError, ValueError):
            # TOML parse exceptions can include file content: do not print values.
            print(f"NG: {path} — 構文・設定名・型・範囲・参照ファイルを確認してください。", file=sys.stderr)
            errors += 1
        else:
            print(f"OK: {path}")
    return 1 if errors else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path,
                        default=[Path("config.toml"), Path("config.example.toml"), Path("priority_rules.example.md")])
    parser.add_argument('--structure-only', action='store_true',
                        help='設定ファイルの参照先ルールを読まずに検証（SharePoint取得前のCI用）')
    args = parser.parse_args()
    return validate(args.paths, structure_only=args.structure_only)


if __name__ == "__main__":
    sys.exit(main())
