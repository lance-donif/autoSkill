"""Find the compact dashboard's empty navigation slot paired with an automatic first grid row."""
import re
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit

from review_rules.browser_dashboard import DashboardEntry

UI = Path("skills/autoskill/app/ui")
SHELL = r"(?:div)?(?:#app(?:\.shell)?|\.shell(?:#app)?)"
SIDE = r"(?:aside)?(?:#side(?:\.side)?|\.side(?:#side)?)(?::empty)?"
EMBED = r"""(?:(?:html)?(?::root)?\[data-embed\s*=\s*["']?true["']?\]\s+)?"""
SELECTOR = re.compile(rf"{EMBED}(?:(?P<shell>{SHELL})|(?:{SHELL}(?:\s*>\s*|\s+))?{SIDE})")
BRACE_OR_STRING = re.compile(r"""'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*"|[{}]""")
SLOTS = {"grid-template": "row", "grid-template-rows": "row", "height": "size", "block-size": "size",
         "min-height": "minimum", "min-block-size": "minimum"}


class Declaration(NamedTuple):
    scope: tuple[str, ...]
    embedded: bool
    priority: tuple[bool, bool, int, int, int]
    slot: str
    value: str
    location: tuple[str, int]


def _blocks(css):
    # Keep line numbers when removing comments; braces inside strings are not structural.
    css = re.sub(r"/\*[\s\S]*?\*/", lambda m: " " + "\n" * m[0].count("\n"), css)
    parents = []
    start = 0
    for match in BRACE_OR_STRING.finditer(css):
        if match[0] == "{":
            heading = css[start:match.start()].strip()
            if heading.lower().startswith("@media"):
                heading = re.sub(r"\s+", "", heading.lower()).replace("@mediascreenand(", "@media(")
            parents.append(heading)
        elif match[0] == "}":
            selectors = parents.pop()
            yield tuple(parents), selectors, css[start:match.start()], css.count("\n", 0, start) + 1
        else:
            continue
        start = match.end()


def _declarations(path, css, *, inline=False):
    for scope, selectors, body, first_line in _blocks(css):
        if any(group.startswith("@mediaprint") and "," not in group for group in scope):
            continue
        for selector in map(str.strip, selectors.split(",")):
            match = SELECTOR.fullmatch(selector)
            if not match:
                continue
            embedded = "data-embed" in selector
            slots = {"row"} if match["shell"] else {"size", "minimum"}
            specificity = (selector.count("#"), selector.count(".") + selector.count("[") + selector.count(":"),
                           len(re.findall(r"\b(?:html|div|aside)\b", selector)))
            for prop in re.finditer(r"(?:^|;)\s*([\w-]+)\s*:\s*([^;]+)", body):
                slot = SLOTS.get(prop[1].lower())
                if slot in slots:
                    line = first_line + body.count("\n", 0, prop.start(1))
                    value = prop[2].strip().lower()
                    important = re.search(r"\s*!\s*important$", value)
                    priority = (bool(important), inline, *specificity)
                    yield Declaration(scope, embedded, priority, slot, value[:important.start()] if important else value, (path, line))


def _has_length(value):
    number = re.fullmatch(r"(\d*\.?\d+)(?:[a-z]+|%)", value)
    return bool(number and float(number[1]) > 0) or value.startswith(("calc(", "clamp(", "max(", "min(", "var("))


def unreserved_rows(sheets, inline_styles=()):
    """Inspect grid and navigation declarations that match the dashboard's initial shell."""
    declarations = [item for path, css in sheets for item in _declarations(path, css)]
    declarations.extend(item for path, css in inline_styles for item in _declarations(path, css, inline=True))
    declarations.sort(key=lambda item: item.priority)
    modes = {(d.scope, d.embedded) for d in declarations if d.slot == "row" and
             (d.embedded or any("max-width" in group or re.search(r"\bwidth\s*<", group) for group in d.scope))}
    locations = set()
    for mode, embedded in modes:
        active = {d.slot: d for d in declarations if mode[:len(d.scope)] == d.scope and (not d.embedded or embedded)}
        row = active["row"]
        reserved = any(_has_length(active[slot].value) for slot in ("size", "minimum") if slot in active)
        if re.match(r"auto(?:\s|/|$)", row.value) and not reserved:
            locations.add(row.location)
    return sorted(locations)


def _entry_styles(root, entry):
    parser = DashboardEntry()
    parser.feed(entry.read_text(encoding="utf-8"))
    if not parser.empty_navigation:
        return (), ()
    name = entry.relative_to(root).as_posix()
    sheets = []
    for sheet in parser.stylesheets:
        if sheet.href:
            url = urlsplit(sheet.href)
            if url.scheme or url.netloc:
                continue
            path = (root / UI / url.path.lstrip("/") if url.path.startswith("/") else entry.parent / url.path).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                continue
            origin, css = path.relative_to(root).as_posix(), path.read_text(encoding="utf-8")
        else:
            origin, css = name, "\n" * (sheet.line - 1) + sheet.css
        if sheet.media.strip().lower() not in {"", "screen", "all"}:
            css = f"@media {sheet.media} {{{css}}}"
        sheets.append((origin, css))
    inline = [(name, "\n" * (line - 1) + f"#{element} {{{style}}}") for element, style, line in parser.inline_styles]
    return tuple(sheets), tuple(inline)


def run(root: Path) -> list[dict]:
    entries = [root / UI / "index.html", *sorted((root / "preview").glob("*.html"))]
    layouts = {_entry_styles(root, entry) for entry in entries}
    locations = set()
    for sheets, inline in layouts:
        locations.update(unreserved_rows(sheets, inline))
    return [{"rule": "compact-navigation", "severity": "error", "path": path, "line": line,
             "message": "紧凑布局的首行随空导航展开。请在首屏为导航或网格首行预留高度。"}
            for path, line in sorted(locations)]
