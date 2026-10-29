"""Render the Markdown subset emitted by the report builder as standalone HTML."""
from html import escape
import re


def inline(text):
    """Interpret generated links/emphasis, escaping all untrusted HTML."""
    pattern = r'\\([\\`*_{}\[\]()#+.!|>~-])|\[([^\]\n]+)\]\(([^)\s]+)\)|\*\*([^\n]+?)\*\*'
    parts, start = [], 0
    for match in re.finditer(pattern, text):
        parts.append(escape(text[start:match.start()]))
        literal, label, target, bold = match.groups()
        if literal is not None:
            parts.append(escape(literal))
        elif label is not None:
            # Only report data files and HTTPS links can become clickable links.
            safe = (re.fullmatch(r'(?:[\w.-]+/)*[\w.-]+\.(?:json|jsonl)', target)
                    and '..' not in target.split('/')) or target.startswith('https://')
            parts.append(f'<a href="{escape(target, quote=True)}">{escape(label)}</a>'
                         if safe else escape(label))
        else:
            parts.append(f'<strong>{inline(bold)}</strong>')
        start = match.end()
    parts.append(escape(text[start:]))
    return ''.join(parts)


def render_report(content):
    """Keep literal email blocks separate from formatting and link processing."""
    lines = content.splitlines()
    blocks, index = [], 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if not line.strip():
            continue
        fence = re.fullmatch(r'(`{3,})text', line)
        if fence:
            body = []
            while index < len(lines) and lines[index] != fence[1]:
                body.append(lines[index])
                index += 1
            index += 1
            blocks.append('<pre class="source-mail">' + escape('\n'.join(body)) + '</pre>')
            continue
        heading = re.match(r'^(#{1,3}) (.*)$', line)
        if heading:
            level = len(heading[1])
            blocks.append(f'<h{level}>{inline(heading[2])}</h{level}>')
            continue
        if line.startswith('| ') and index < len(lines) and re.fullmatch(r'\|[\s:|\-]+', lines[index]):
            def cells(row):
                return re.split(r'(?<!\\)\|', row.strip()[1:-1])

            blocks.append('<div class="table-scroll"><table><thead><tr>' + ''.join(
                f'<th scope="col">{inline(cell.strip())}</th>' for cell in cells(line)) + '</tr></thead><tbody>')
            index += 1
            while index < len(lines) and lines[index].startswith('| '):
                blocks.append('<tr>' + ''.join(f'<td>{inline(cell.strip())}</td>'
                                               for cell in cells(lines[index])) + '</tr>')
                index += 1
            blocks.append('</tbody></table></div>')
            continue
        ordered = re.match(r'^(\d+)\. (.*)$', line)
        if ordered:
            blocks.append(f'<ol start="{int(ordered[1])}">')
            blocks.append(f'<li>{inline(ordered[2])}</li>')
            while index < len(lines) and (item := re.match(r'^\d+\. (.*)$', lines[index])):
                blocks.append(f'<li>{inline(item[1])}</li>')
                index += 1
            blocks.append('</ol>')
            continue
        paragraph = [inline(line.rstrip())]
        while index < len(lines) and lines[index].strip():
            if re.match(r'^(#{1,3} |`{3,}text$|\| |\d+\. )', lines[index]):
                break
            paragraph.append(inline(lines[index].rstrip()))
            index += 1
        blocks.append('<p>' + '<br>\n'.join(paragraph) + '</p>')
    return '''<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>メール分析レポート</title>
<style>
* { box-sizing: border-box; }
body { margin: 0; color: #243247; background: #f1f5f9;
       font-family: "Yu Gothic", "Meiryo", sans-serif; line-height: 1.8; }
main { max-width: 1120px; margin: 32px auto; padding: 32px;
       background: #fff; border-radius: 12px; overflow-wrap: anywhere; }
h1 { font-size: 1.8rem; color: #163b65; }
h2 { margin-top: 2.5rem; padding-bottom: .4rem; border-bottom: 2px solid #cbd5e1; }
h3 { margin-top: 2rem; padding: .7rem 1rem; background: #edf4fb; border-left: 4px solid #2864a0; }
a { color: #185a9d; }
.source-mail { white-space: pre-wrap; overflow-wrap: anywhere; font: inherit;
               padding: 20px; background: #f8fafc; border: 1px solid #dbe3ec; border-radius: 6px; }
.table-scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; margin: 1rem 0; }
th, td { border: 1px solid #cbd5e1; padding: .6rem .8rem; text-align: left; vertical-align: top; }
th { background: #edf4fb; }
@media (max-width: 640px) { main { margin: 0; padding: 16px; border-radius: 0; } }
@media print { body { background: #fff; } main { margin: 0; padding: 0; max-width: none; }
               h2, h3 { break-after: avoid; } .table-scroll { overflow: visible; } }
</style>
</head>
<body><main>
''' + '\n'.join(blocks) + '\n</main></body>\n</html>\n'
