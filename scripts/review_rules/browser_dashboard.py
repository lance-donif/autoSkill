"""Check the dashboard's local module and entry-asset graph without running the app."""
from __future__ import annotations

import ast
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

TOKEN = re.compile(r"//[^\n]*|/\*[\s\S]*?\*/|(?P<string>'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\")|(?P<word>[\w$]+)|(?P<other>\S)")


def _imports(source: str):
    """Read literal static/dynamic module specifiers, ignoring comments and other strings."""
    tokens = [match for match in TOKEN.finditer(source) if not match.group().startswith(('//', '/*'))]
    for index, token in enumerate(tokens):
        if token.lastgroup != 'word' or token.group() not in ('import', 'export'):
            continue
        following = tokens[index + 1:]
        if not following or following[0].group() == '.':
            continue
        specifier = None
        if token.group() == 'import' and following[0].lastgroup == 'string':
            specifier = following[0]
        elif following[0].group() == '(':
            if len(following) > 1 and following[1].lastgroup == 'string':
                specifier = following[1]
        else:
            for offset, part in enumerate(following[:-1]):
                if part.group() == ';':
                    break
                if part.lastgroup == 'word' and part.group() == 'from' and following[offset + 1].lastgroup == 'string':
                    specifier = following[offset + 1]
                    break
        if specifier:
            yield ast.literal_eval(specifier.group()), source.count('\n', 0, specifier.start()) + 1


class _Assets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.assets = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'script' and values.get('src'):
            self.assets.append((values['src'], self.getpos()[0]))
        if tag == 'link' and values.get('rel') == 'stylesheet' and values.get('href'):
            self.assets.append((values['href'], self.getpos()[0]))


def run(root: Path) -> list[dict]:
    ui = root / 'skills/autoskill/app/ui'
    findings = []

    def report(file, line, message):
        findings.append({'rule': 'browser-dashboard-assets', 'severity': 'error',
                         'path': file.relative_to(root).as_posix(), 'line': line, 'message': message})

    entry = ui / 'index.html'
    if not entry.is_file():
        report(entry, 1, '仪表盘入口不存在，无法打开页面')
        return findings
    parser = _Assets()
    parser.feed(entry.read_text(encoding='utf-8'))
    for value, line in parser.assets:
        parsed = urlsplit(value)
        target = ui / parsed.path.lstrip('/')
        if parsed.scheme or parsed.netloc or not target.is_file():
            report(entry, line, f'仪表盘入口资源不是可读取的本地文件：{value}')
    for file in sorted((ui / 'js').rglob('*.js')):
        source = file.read_text(encoding='utf-8')
        for value, line in _imports(source):
            if not value.startswith(('./', '../')):
                report(file, line, f'浏览器模块需使用相对导入，保留整组界面资源版本：{value}')
                continue
            target = (file.parent / urlsplit(value).path).resolve()
            if not target.is_relative_to(ui.resolve()) or not target.is_file():
                report(file, line, f'浏览器导入目标不存在或不属于仪表盘：{value}')
    return findings
