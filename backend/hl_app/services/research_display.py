"""Build a static display copy from registered files; source bytes stay intact.

All display dependencies are embedded so an opaque sandbox never needs a
credentialed subrequest. Only registered report links can navigate the outer
reader, and only following a user gesture. Nothing in a report is executed.
"""
from __future__ import annotations

import base64
import html
import re
from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import Callable
from urllib.parse import quote, unquote, urlsplit


class DisplayError(ValueError):
    pass


DISPLAY_REVISION = 1
MAX_EMBEDDED_BYTES = 64 * 1024 * 1024
CSS_URL = re.compile(r"url\(\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s)]+))\s*\)", re.I)
CSS_IMPORT = re.compile(
    r"@import\s+(?:url\(\s*(?:\"([^\"]+)\"|'([^']+)'|([^\s)]+))\s*\)|\"([^\"]+)\"|'([^']+)')\s*([^;]*);",
    re.I,
)
ATTR = re.compile(r'''(\s+)([^\s/>=]+)(\s*=\s*)(?:"([^"]*)"|'([^']*)'|([^\s>]+))''')
# The same shapes the standard library parser uses, so the recorded references
# line up one to one with the parsed ones.
ENTITY_REFERENCE = re.compile('&([a-zA-Z][-.a-zA-Z0-9]*)[^a-zA-Z0-9]')
CHARACTER_REFERENCE = re.compile('&#(?:[0-9]+|[xX][0-9a-fA-F]+)[^0-9a-fA-F]')
MIME = {
    '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
    '.gif': 'image/gif', '.webp': 'image/webp', '.avif': 'image/avif',
    '.svg': 'image/svg+xml', '.ico': 'image/x-icon', '.bmp': 'image/bmp',
    '.woff': 'font/woff', '.woff2': 'font/woff2', '.ttf': 'font/ttf',
    '.otf': 'font/otf', '.mp4': 'video/mp4', '.webm': 'video/webm',
    '.mp3': 'audio/mpeg', '.ogg': 'audio/ogg', '.wav': 'audio/wav',
}


def local_reference(value: str, base: str) -> tuple[str, str, str]:
    """Resolve a URL path once, without treating its query/fragment as a filename."""
    parts = urlsplit(value)
    if parts.scheme or parts.netloc or parts.path.startswith('/'):
        raise DisplayError(f'不允许的本地资源地址：{value}')
    decoded = unquote(parts.path)
    if '\\' in decoded or '\x00' in decoded or decoded.startswith('/'):
        raise DisplayError(f'非法资源路径：{value}')
    stack = list(PurePosixPath(base).parent.parts)
    for part in decoded.split('/'):
        if part in {'', '.'}:
            continue
        if part == '..':
            if not stack:
                raise DisplayError(f'资源路径越界：{value}')
            stack.pop()
        else:
            stack.append(part)
    if not stack:
        raise DisplayError(f'非法资源路径：{value}')
    return '/'.join(stack), parts.query, parts.fragment


def reference_forms(text: str) -> list[str]:
    """Record whether each character reference in the source ended with a semicolon.

    The parser reports a reference without its terminator, so the original
    spelling is captured up front. Re-emitting the same form keeps the copied
    text identical to the source instead of adding a semicolon to a bare "&".
    """
    forms: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        position = text.find('&', index)
        if position < 0:
            break
        index = position + 1
        if text.startswith('&#', position):
            match = CHARACTER_REFERENCE.match(text, position)
        else:
            match = ENTITY_REFERENCE.match(text, position)
        if match is None:
            continue
        index = match.end()
        forms.append(';' if text[match.end() - 1:match.end()] == ';' else '')
    return forms


def srcset_items(value: str) -> list[tuple[str, str]]:
    """Parse URL tokens (including data URLs) and optional width/density descriptors."""
    items = []
    rest = value.strip()
    while rest:
        rest = rest.lstrip(' ,\t\r\n')
        if not rest:
            break
        if rest.lower().startswith('data:'):
            match = re.match(r'\S+', rest)
        else:
            match = re.match(r'[^,\s]+', rest)
        if match is None:
            raise DisplayError('srcset 格式无效')
        url = match[0]
        rest = rest[match.end():]
        if url.endswith(','):
            items.append((url.rstrip(','), ''))
            continue
        descriptor, comma, rest = rest.partition(',')
        descriptor = descriptor.strip()
        if descriptor and not re.fullmatch(r'\d+(?:\.\d+)?[wx]', descriptor):
            raise DisplayError('srcset 描述符无效')
        items.append((url, descriptor))
        if not comma:
            break
    return items


class StaticDisplay:
    def __init__(self, read: Callable[[str], bytes], assets: set[str],
                 reports: dict[str, str], snapshot_id: str):
        self.read = read
        self.assets = assets
        self.reports = reports
        self.snapshot_id = snapshot_id
        self.active: set[str] = set()
        self.used: set[str] = set()
        self.embedded_bytes = 0

    def resource(self, url: str, base: str) -> str:
        if url.startswith('#') or url.lower().startswith('data:'):
            return url
        path, _, fragment = local_reference(url, base)
        if path not in self.assets:
            raise DisplayError(f'必需展示资源未获准发布：{url}')
        if path in self.active:
            raise DisplayError(f'资源循环引用：{path}')
        suffix = PurePosixPath(path).suffix.lower()
        mime = MIME.get(suffix)
        if mime is None:
            raise DisplayError(f'不支持的展示资源类型：{path}')
        self.active.add(path)
        try:
            payload = self.read(path)
            self.used.add(path)
            if suffix == '.svg':
                # SVG image dependencies also become self contained. Preserve
                # case-sensitive SVG attributes by rewriting original tag text.
                svg_text = payload.decode('utf-8')
                parser = _StaticHTML(self, path, reference_forms(svg_text))
                parser.feed(svg_text)
                parser.close()
                payload = ''.join(parser.output).encode('utf-8')
            self.embedded_bytes += len(payload)
            if self.embedded_bytes > MAX_EMBEDDED_BYTES:
                raise DisplayError('内嵌展示资源超过大小限制')
            result = f'data:{mime};base64,' + base64.b64encode(payload).decode('ascii')
            return result + ('#' + quote(unquote(fragment), safe='') if fragment else '')
        finally:
            self.active.remove(path)

    def css(self, text: str, base: str) -> str:
        # Reject CSS escapes in resource syntax rather than accepting a hidden
        # dependency that the URL scanner cannot correctly resolve.
        if '\\' in text or re.search(r'/\*.*?\*/', text, re.S):
            text = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
            if '\\' in text:
                raise DisplayError('展示 CSS 不支持转义资源语法')

        def imported(match: re.Match) -> str:
            url = next(value for value in match.groups()[:5] if value is not None)
            path, _, _ = local_reference(url, base)
            if path not in self.assets or PurePosixPath(path).suffix.lower() != '.css':
                raise DisplayError(f'必需样式资源未获准发布：{url}')
            if path in self.active:
                raise DisplayError(f'样式循环引用：{path}')
            self.active.add(path)
            try:
                payload = self.read(path)
                self.used.add(path)
                self.embedded_bytes += len(payload)
                if self.embedded_bytes > MAX_EMBEDDED_BYTES:
                    raise DisplayError('内嵌展示资源超过大小限制')
                body = self.css(payload.decode('utf-8'), path)
            finally:
                self.active.remove(path)
            media = match[6].strip()
            # Layer/supports imports need a real CSS parser; refuse rather than
            # silently changing their semantics.
            if re.search(r'\b(layer|supports)\b', media, re.I):
                raise DisplayError('暂不支持带 layer/supports 的样式导入')
            return f'@media {media} {{{body}}}' if media else body

        text = CSS_IMPORT.sub(imported, text)
        if re.search(r'@import\b', text, re.I):
            raise DisplayError('无法解析的样式导入')
        text = CSS_URL.sub(lambda m: 'url("' + self.resource(
            next(v for v in m.groups() if v is not None), base
        ).replace('"', '%22') + '")', text)
        return text

    def stylesheet(self, url: str, base: str) -> str:
        return self.css('@import "' + url.replace('"', '%22') + '";', base)

    def hyperlink(self, url: str, base: str) -> str | None:
        if url.startswith('#'):
            return url
        try:
            path, _, fragment = local_reference(url, base)
        except (DisplayError, ValueError):
            return None
        report_id = self.reports.get(path)
        if report_id is None:
            return None
        result = '/research/reports/' + quote(report_id, safe='')
        result += '?snapshot=' + quote(self.snapshot_id, safe='')
        if fragment:
            result += '#' + quote(unquote(fragment), safe='')
        return result

    def render(self, text: str, path: str) -> bytes:
        parser = _StaticHTML(self, path, reference_forms(text))
        parser.feed(text)
        parser.close()
        return ''.join(parser.output).encode('utf-8')


class _StaticHTML(HTMLParser):
    def __init__(self, display: StaticDisplay, path: str, forms: list[str] | None = None):
        super().__init__(convert_charrefs=False)
        self.display, self.path = display, path
        self.output: list[str] = []
        self.skip_script = False
        self.in_style = False
        self.styles: list[str] = []
        self.forms = list(forms or [])

    def handle_starttag(self, tag, attrs):
        if tag == 'script':
            self.skip_script = True
            return
        if self.skip_script:
            return
        attributes = dict(attrs)
        if tag in {'iframe', 'embed', 'object', 'base'}:
            raise DisplayError(f'不支持的静态展示元素：{tag}')
        if tag == 'meta' and attributes.get('http-equiv', '').lower() == 'refresh':
            raise DisplayError('静态报告不允许自动跳转')
        if tag == 'link':
            rel = attributes.get('rel', '').lower().split()
            if 'stylesheet' in rel:
                css = self.display.stylesheet(attributes.get('href', ''), self.path)
                css = re.sub(r'</style', r'<\/style', css, flags=re.I)
                media = attributes.get('media')
                self.output.append('<style' + (' media="' + html.escape(media, quote=True) + '"' if media else '') + '>' + css + '</style>')
            # Preloads/icons are unnecessary for the self contained display.
            return
        if tag == 'style':
            self.in_style = True
            self.styles = []
        raw = self.get_starttag_text()
        report_link = None
        if tag == 'a' and attributes.get('href'):
            report_link = self.display.hyperlink(attributes['href'], self.path)

        def rewrite(match):
            name = match[2].lower()
            value = html.unescape(next(v for v in match.groups()[3:] if v is not None))
            replacement = value
            if name.startswith('on') or name in {'target', 'ping', 'formaction', 'action', 'srcdoc'}:
                return ''
            if name == 'style':
                replacement = self.display.css(value, self.path)
            elif name == 'srcset':
                replacement = ', '.join(self.display.resource(url, self.path) + (' ' + desc if desc else '')
                                        for url, desc in srcset_items(value))
            elif name in {'src', 'poster'} or (name in {'href', 'xlink:href'} and tag not in {'a', 'link'}):
                replacement = self.display.resource(value, self.path)
            elif name == 'href' and tag == 'a':
                replacement = report_link or '#'
            else:
                return match[0]
            return match[1] + match[2] + '="' + html.escape(replacement, quote=True) + '"'

        raw = ATTR.sub(rewrite, raw)
        if report_link and not report_link.startswith('#'):
            raw = raw[:-1] + ' target="_top">'
        self.output.append(raw)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag == 'script':
            self.skip_script = False
            return
        if self.skip_script:
            return
        if tag == 'style':
            css = self.display.css(''.join(self.styles), self.path)
            self.output.append(re.sub(r'</style', r'<\/style', css, flags=re.I))
            self.in_style = False
        self.output.append('</' + tag + '>')

    def handle_data(self, data):
        if not self.skip_script:
            (self.styles if self.in_style else self.output).append(data)

    def handle_entityref(self, name):
        self.handle_data('&' + name + (self.forms.pop(0) if self.forms else ';'))

    def handle_charref(self, name):
        self.handle_data('&#' + name + (self.forms.pop(0) if self.forms else ';'))

    def handle_decl(self, decl):
        if not self.skip_script:
            self.output.append('<!' + decl + '>')

    def handle_comment(self, data):
        if not self.skip_script:
            self.output.append('<!--' + data + '-->')
