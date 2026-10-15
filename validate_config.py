"""Offline TOML validation. Never load .env, resolve credentials, or call services."""
import argparse
from pathlib import Path
import sys

import mail_analyzer as app
import prioritization


def validate(paths):
    errors = 0
    for path in paths:
        try:
            # Rule files have a different schema from application settings.
            with path.open("rb") as file:
                data = app.tomllib.load(file)
            if "ses" in data or "contract" in data or "top_n" in data:
                prioritization.load_rules(path)
            else:
                app.load_config(path, load_environment=False)
        except (app.ToolError, OSError, ValueError):
            # TOML parse exceptions can include file content: do not print values.
            print(f"NG: {path} — TOML構文・設定名・型・範囲・参照ファイルを確認してください。", file=sys.stderr)
            errors += 1
        else:
            print(f"OK: {path}")
    return 1 if errors else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path,
                        default=[Path("config.toml"), Path("config.example.toml"), Path("priority_rules.toml")])
    return validate(parser.parse_args().paths)


if __name__ == "__main__":
    sys.exit(main())
