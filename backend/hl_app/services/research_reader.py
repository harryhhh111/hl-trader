"""Read explicit research entries from one immutable Git commit. No repo code runs."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path, PurePosixPath

import yaml


class ResearchReadError(RuntimeError):
    """Invalid/unavailable research is not the same as an absent security entry."""


MARKETS = {'US': ('US', {'XNYS', 'XNAS'}),
           'HK': ('CN_HK', {'XHKG'}), 'CN': ('CN_A', {'XSHG', 'XSHE', 'XBSE'})}
KINDS = {'company', 'topic', 'protocol', 'token', 'other'}
SECTIONS = ('thesis', 'risks', 'metrics', 'open_questions', 'research_metadata')
LABELS = {'thesis': '核心判断', 'risks': '风险', 'metrics': '跟踪指标', 'open_questions': '待验证问题'}
EXCLUDED = {'.git', '.venv', 'venv', '__pycache__', '参考库', 'node_modules'}


def require(condition, message):
    if not condition:
        raise ResearchReadError(message)


def string(value):
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


def shape(value, required, optional=()):
    require(isinstance(value, dict) and set(required) <= value.keys()
            and value.keys() <= set(required) | set(optional), '字段缺失或包含未知字段')


class EntryLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            require(isinstance(key, str), 'YAML 键必须是字符串')
            require(key not in result, f'重复 YAML 键：{key}')
            result[key] = self.construct_object(value_node, deep=deep)
        return result


class GitSnapshot:
    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()
        require(self.root.is_dir(), '研究仓库目录不存在')
        top = self.git('rev-parse', '--show-toplevel').decode().strip()
        require(Path(top).resolve() == self.root, '研究路径必须是 Git 仓库根目录')
        self.commit = self.git('rev-parse', '--verify', 'HEAD^{commit}').decode().strip()
        self.files = {}
        self.texts = {}
        for row in self.git('ls-tree', '-r', '-l', '-z', '--full-tree', self.commit).split(b'\0'):
            if not row:
                continue
            header, path = row.split(b'\t', 1)
            mode, kind, oid, size = header.decode('ascii').split()
            self.files[path.decode('utf-8')] = (mode, kind, oid, size)

    def git(self, *args):
        try:
            result = subprocess.run(['git', '--no-replace-objects', '-C', str(self.root), *args],
                                    capture_output=True, timeout=15, check=True)
            return result.stdout
        except (OSError, subprocess.SubprocessError) as exc:
            raise ResearchReadError('研究 Git 读取失败，请检查仓库与提交是否可用') from exc

    def read(self, path, limit=2_000_000):
        require(path in self.files, f'已提交文件不存在：{path}')
        mode, kind, oid, size = self.files[path]
        require(mode in {'100644', '100755'} and kind == 'blob', f'禁止符号链接或非普通文件：{path}')
        require(size.isdigit() and int(size) <= limit, f'研究文件超过读取限制：{path}')
        try:
            if path not in self.texts:
                self.texts[path] = self.git('cat-file', 'blob', oid).decode('utf-8')
            return self.texts[path]
        except UnicodeError as exc:
            raise ResearchReadError(f'研究文件不是有效 UTF-8：{path}') from exc

    def document(self, entry, value):
        require(string(value), '文档路径必须是非空字符串')
        path = PurePosixPath(value)
        require(not path.is_absolute() and '\\' not in value, '禁止绝对路径或反斜线')
        parts = list(PurePosixPath(entry).parent.parts)
        for part in path.parts:
            if part == '..':
                require(bool(parts), f'文档路径越界：{value}')
                parts.pop()
            elif part != '.':
                parts.append(part)
        target = '/'.join(parts)
        require(PurePosixPath(target).suffix == '.md', '引用必须是 Markdown 文档')
        # Only Git tree blobs are read; symlinks in either the commit or working tree cannot escape.
        self.read(target)
        return target


def extract_section(text, heading_path):
    """Match an exact ATX ancestor chain, ignoring fenced-code headings."""
    lines = text.splitlines(keepends=True)
    stack, headings, fence = [], [], None
    for index, line in enumerate(lines):
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', line.rstrip('\r\n'))
        if marker:
            run, tail = marker.groups()
            if fence is None:
                if run[0] != '`' or '`' not in tail:
                    fence = (run[0], len(run))
            elif run[0] == fence[0] and len(run) >= fence[1] and not tail.strip():
                fence = None
            continue
        if fence:
            continue
        match = re.match(r'^ {0,3}(#{1,6})(?:[ \t]+|$)(.*)$', line.rstrip('\r\n'))
        if not match:
            continue
        level = len(match[1])
        title = re.sub(r'[ \t]+#+[ \t]*$', '', match[2]).strip()
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        headings.append((index, level, [title for _, title in stack]))
    matches = [item for item in headings if item[2] == heading_path]
    require(len(matches) == 1, '章节标题不存在' if not matches else '章节标题匹配不唯一')
    start, level, _ = matches[0]
    end = next((i for i, depth, _ in headings if i > start and depth <= level), len(lines))
    require(bool(''.join(lines[start + 1:end]).strip()), '研究章节正文为空')
    return ''.join(lines[start:end]).strip(), start + 1


def read_entry(snapshot, entry):
    try:
        raw = snapshot.read(entry, limit=64_000)
        require(not any(isinstance(token, yaml.tokens.AliasToken) for token in yaml.scan(raw)),
                '研究入口不支持 YAML 别名')
        data = yaml.load(raw, Loader=EntryLoader)
        shape(data, {'schema_version', 'entity_id', 'kind', 'name', 'securities',
                     'primary_document', 'documents', 'related_entities', 'sections'}, {'aliases'})
        require(type(data['schema_version']) is int and data['schema_version'] == 2,
                '仅支持 schema_version: 2')
        require(string(data['entity_id']) and re.fullmatch(
            r'(company|topic|protocol|token|other):[a-z0-9][a-z0-9-]*', data['entity_id']), '非法主体 ID')
        require(data['kind'] in KINDS and data['kind'] == data['entity_id'].split(':')[0], '主体类型不一致')
        require(string(data['name']), '主体名称无效')
        require(isinstance(data.get('aliases', []), list) and all(string(s) for s in data.get('aliases', [])),
                '别名必须是字符串列表')
        for key in ('securities', 'documents', 'related_entities'):
            require(isinstance(data[key], list), f'{key} 必须是列表')
        require(data['kind'] == 'company' or not data['securities'], '非公司主体不能声明股票证券')
        identities = set()
        for security in data['securities']:
            shape(security, {'market', 'mic', 'symbol'})
            require(all(string(security[k]) for k in security), '证券字段必须是字符串')
            market, mic, symbol = security['market'], security['mic'], security['symbol']
            require(market in MARKETS and mic in MARKETS[market][1], '未知市场、MIC 或组合冲突')
            require(re.fullmatch(r'[A-Za-z0-9.\-]{1,20}', symbol) is not None, '非法股票代码')
            identity = (mic, symbol.upper())
            require(identity not in identities, '入口内重复证券')
            identities.add(identity)
        declared = {snapshot.document(entry, data['primary_document'])}
        for doc in data['documents']:
            shape(doc, {'path', 'role'})
            require(doc['role'] in {'review', 'supplement', 'source', 'opinion'}, '未知文档角色')
            path = snapshot.document(entry, doc['path'])
            require(path not in declared, '重复文档路径')
            declared.add(path)
        for relation in data['related_entities']:
            shape(relation, {'entity_id', 'relation'})
            require(string(relation['entity_id']) and relation['relation'] in
                    {'related', 'theme', 'parent', 'subsidiary', 'underlying'}, '非法主体关联')
        shape(data['sections'], set(SECTIONS))
        extracted = {}
        for key, locator in data['sections'].items():
            if locator is None:
                continue
            shape(locator, {'path', 'heading_path'})
            require(isinstance(locator['heading_path'], list) and locator['heading_path']
                    and all(string(s) for s in locator['heading_path']), '章节标题链无效')
            path = snapshot.document(entry, locator['path'])
            require(path in declared, '章节引用未声明文档')
            text, line = extract_section(snapshot.read(path), locator['heading_path'])
            require(bool(text.strip()), '研究章节为空')
            extracted[key] = {'path': path, 'line': line, 'heading_path': locator['heading_path'], 'text': text}
        return data, extracted
    except (ResearchReadError, yaml.YAMLError, TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ResearchReadError(f'研究入口 {entry}：{exc}') from exc


def read_research(root: Path, market: str, code: str):
    require(market in {item[0] for item in MARKETS.values()}, '不支持的证券市场')
    snapshot = GitSnapshot(root)
    entities, securities = {}, set()
    for path in sorted(snapshot.files):
        parts = PurePosixPath(path).parts
        if parts[-1] != 'research.yaml' or EXCLUDED.intersection(parts):
            continue
        data, extracted = read_entry(snapshot, path)
        require(data['entity_id'] not in entities, f'重复主体：{data["entity_id"]}')
        for security in data['securities']:
            identity = (security['mic'], security['symbol'].upper())
            require(identity not in securities, f'重复证券：{identity[0]}:{identity[1]}')
            securities.add(identity)
        entities[data['entity_id']] = (data, extracted)
    for data, _ in entities.values():
        for relation in data['related_entities']:
            require(relation['entity_id'] in entities, f'关联主体不存在：{relation["entity_id"]}')
            require(relation['relation'] != 'theme' or entities[relation['entity_id']][0]['kind'] == 'topic',
                    'theme 关联必须指向主题')
    matches = [(data, sections) for data, sections in entities.values()
               if data['kind'] == 'company' and any(
                   MARKETS[s['market']][0] == market and s['symbol'].upper() == code.upper()
                   for s in data['securities'])]
    require(len(matches) <= 1, '同市场股票代码匹配多个研究主体，需消除 MIC 歧义')
    diagnostic = {'commit_sha': snapshot.commit, 'entity_id': None, 'sections': [], 'missing_sections': []}
    if not matches:
        return [], '', {**diagnostic, 'status': 'no_entry'}
    data, sections = matches[0]
    docs, excerpts = [], []
    diagnostic.update(entity_id=data['entity_id'], status='matched')
    for key, label in LABELS.items():
        if key not in sections:
            diagnostic['missing_sections'].append(key)
            continue
        section = sections[key]
        body = section['text'][:1500]
        truncated = len(section['text']) > len(body)
        diagnostic['sections'].append({k: v for k, v in section.items() if k != 'text'} |
                                      {'kind': key, 'truncated': truncated, 'total_chars': len(section['text'])})
        if section['path'] not in docs:
            docs.append(section['path'])
        excerpts.append(f'### {label} | {section["path"]}:{section["line"]}'
                        f'{" | 已截断" if truncated else ""}\n{body}')
    require(bool(excerpts), '已匹配研究入口，但没有配置任何可用观点章节')
    return docs, '\n\n'.join(excerpts), diagnostic
