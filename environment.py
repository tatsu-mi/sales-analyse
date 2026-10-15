"""Load a local .env without overriding process environment or expanding values."""
import json
import os
import re
from pathlib import Path


def load_env(path):
    path = Path(path)
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        return
    except (OSError, UnicodeError):
        raise ValueError(".envを読めません。UTF-8のファイルを指定してください。") from None
    values = {}
    for number, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)", line)
        if not match or match[1] in values:
            raise ValueError(f".envの{number}行目の形式またはキーの重複を確認してください。")
        key, value = match.groups()
        try:
            if value.startswith('"'):
                decoded, end = json.JSONDecoder().raw_decode(value)
                suffix = value[end:].strip()
                if not isinstance(decoded, str) or (suffix and not suffix.startswith('#')):
                    raise ValueError
                value = decoded
            elif value.startswith("'"):
                end = value.index("'", 1)
                suffix = value[end + 1:].strip()
                if suffix and not suffix.startswith("#"):
                    raise ValueError
                value = value[1:end]
            else:
                value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        except ValueError:
            raise ValueError(f".envの{number}行目の引用符・形式を確認してください。") from None
        values[key] = value
    # Parse the entire file before making any changes.
    for key, value in values.items():
        os.environ.setdefault(key, value)
