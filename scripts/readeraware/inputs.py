"""Local preliminary source adapters; Main must verify editable Markdown.

``load_sources`` returns pipeline sources and JSON-serializable provenance.
``provenance['files']`` enumerates exactly the existing files needed for an
original-file snapshot. Its relative paths are safe and source-ID namespaced.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import logging
import re
import shutil
import subprocess
import warnings
from pathlib import Path

from .util import ValidationError, check_no_credentials


class InputError(ValidationError):
    """An input cannot be extracted without guessing or executing its contents."""


def _version(package):
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return 'unknown'


def _utf8(path):
    try:
        return path.read_bytes().decode('utf-8-sig')
    except (OSError, UnicodeError) as exc:
        raise InputError(f'Cannot read UTF-8 source {path}: {exc}') from exc


def _inside(path, root):
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise InputError(f'TeX dependency escapes project root: {path} (root: {root})') from exc
    return resolved


def _file_record(path, root, source_id, role):
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise InputError(f'Cannot read source dependency {path}: {exc}') from exc
    return {'path': str(path), 'relative_path': source_id + '/' + path.relative_to(root).as_posix(),
            'sha256': hashlib.sha256(data).hexdigest(), 'size_bytes': len(data),
            'role': role, 'source_id': source_id}


def _finding(source_id, code, message, action, *, status='unresolved', **details):
    return {'source_id': source_id, 'code': code, 'status': status,
            'message': message, 'required_action': action, **details}


class _Resources:
    """Extract original embedded images only, never equation/table screenshots."""
    def __init__(self, source_id, source_path, prepared_dir):
        self.source_id = source_id
        self.source_path = source_path
        self.prepared_dir = prepared_dir
        self.resources = []
        self.files = []
        self.by_locator = {}
        self.findings = []

    def add(self, data, suffix, kind, locator, **details):
        key = (kind, locator)
        if key in self.by_locator:
            return self.by_locator[key]
        suffix = suffix.lower()
        if not re.fullmatch(r'\.[a-z0-9]{1,10}', suffix):
            suffix = '.bin'
        number = len(self.resources) + 1
        subpath = f'{kind}/image-{number:04}{suffix}'
        relative = f'resources/{self.source_id}/{subpath}'
        record = {'resource_id': f'{self.source_id}-I{number:03}',
            'source_id': self.source_id, 'source_path': str(self.source_path),
            'kind': kind, 'container_locator': locator,
            'output_relative_path': relative,
            'sha256': hashlib.sha256(data).hexdigest(), 'size_bytes': len(data),
            'status': 'needs_materialization',
            'intended_use': 'original-embedded-image',
            'requires_visual_classification': True, **details}
        if self.prepared_dir is not None:
            target = (self.prepared_dir / relative).resolve()
            try:
                target.relative_to(self.prepared_dir)
            except ValueError as exc:
                raise InputError('图片资源路径超出准备目录') from exc
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and (not target.is_file() or target.read_bytes() != data):
                raise InputError(f'图片资源目标已存在且内容不同：{target}')
            target.write_bytes(data)
            record.update(path=str(target), status='materialized')
            self.files.append({'path': str(target),
                'relative_path': f'{self.source_id}/resources/{subpath}',
                'output_relative_path': relative, 'role': kind,
                'source_id': self.source_id, 'sha256': record['sha256'],
                'size_bytes': len(data)})
        else:
            self.findings.append(_finding(self.source_id, 'image_resource_not_materialized',
                '已识别原文内嵌图片，但尚未写入本地准备资源目录。',
                '通过 prepared_dir 提取本地资源，并核对 Markdown 图片链接。',
                resource_id=record['resource_id']))
        self.findings.append(_finding(self.source_id, 'image_semantics_require_review',
            '内嵌图片的用途尚未人工核对，不能据此认定公式或表格已恢复。',
            '主代理核对图片、位置和图注；图片中的公式或表格必须另行恢复为可编辑内容，不得以图片替代。',
            status='needs_visual_verification', resource_id=record['resource_id']))
        self.resources.append(record)
        self.by_locator[key] = record
        return record

    @staticmethod
    def markdown(record, label=None):
        label = (label or f'原文图片 {record["resource_id"]}').replace(']', '\\]')
        return f'![{label}]({record["output_relative_path"]})'


def _tex_mask(text):
    """Mask comments and literal environments, preserving offsets and newlines.

    This is a deliberately bounded lexical reader, not a TeX interpreter. An
    escaped percent sign and consecutive backslashes follow TeX token rules.
    """
    chars = list(text)
    length = len(text)

    def blank(start, end):
        for position in range(start, end):
            if chars[position] not in '\r\n':
                chars[position] = ' '

    position = 0
    while position < length:
        if text[position] == '%':
            end = text.find('\n', position)
            end = length if end < 0 else end
            blank(position, end)
            position = end
        elif text[position] == '\\':
            if position + 1 < length and not text[position + 1].isalpha():
                if text[position + 1] == '\\':
                    blank(position, position + 2)
                position += 2
                continue
            literal = re.match(r'\\(?:verb\*?|lstinline\*?)(?![A-Za-z])', text[position:])
            if literal:
                start = position
                position += literal.end()
                if position < length and text[position] == '[':
                    option_end = text.find(']', position + 1)
                    if option_end >= 0:
                        position = option_end + 1
                if position < length and text[position] not in '\r\n':
                    delimiter = text[position]
                    end = text.find(delimiter, position + 1)
                    if end >= 0:
                        blank(start, end + 1)
                        position = end + 1
                        continue
            env = re.match(r'\\begin\s*\{(verbatim\*?|Verbatim\*?|lstlisting|minted|comment)\}', text[position:])
            if env:
                end_match = re.search(r'\\end\s*\{' + re.escape(env[1]) + r'\}', text[position + env.end():])
                end = length if end_match is None else position + env.end() + end_match.end()
                blank(position, end)
                position = end
            else:
                command = re.match(r'\\[A-Za-z]+', text[position:])
                position += command.end() if command else 1
        else:
            position += 1
    return ''.join(chars)


def _argument(mask, position, optional=False):
    while position < len(mask) and mask[position].isspace():
        position += 1
    opening, closing = ('[', ']') if optional else ('{', '}')
    if position >= len(mask) or mask[position] != opening:
        return None, position
    start, depth = position + 1, 1
    position += 1
    while position < len(mask):
        if mask[position] == '\\':
            position += 2
            continue
        if mask[position] == opening:
            depth += 1
        elif mask[position] == closing:
            depth -= 1
            if depth == 0:
                return mask[start:position], position + 1
        position += 1
    raise InputError('Unclosed TeX dependency argument; check matching braces/brackets')


class _TexProject:
    COMMANDS = re.compile(r'\\(input|include|import|subimport|includegraphics|bibliography|addbibresource|usepackage|RequirePackage|documentclass|bibliographystyle|graphicspath)(?![A-Za-z])\*?')
    GRAPHIC_SUFFIXES = ('', '.pdf', '.png', '.jpg', '.jpeg', '.eps', '.svg', '.webp')

    def __init__(self, root, source_id):
        self.root = root
        self.source_id = source_id
        self.files = {}
        self.references = []
        self.warnings = []
        self.graphic_dirs = []

    def add_file(self, path, role):
        if path not in self.files:
            self.files[path] = _file_record(path, self.root, self.source_id, role)

    def resolve(self, raw, parent, suffixes, command, *, required=False, system=False, extra_dirs=()):
        raw = raw.strip().strip('"')
        record = {'command': command, 'requested': raw}
        self.references.append(record)
        if not raw or any(character in raw for character in '\\#$\x00{}'):
            record['status'] = 'dynamic-unsupported'
            message = f'Cannot resolve nonliteral \\{command} argument: {raw!r}'
            if required:
                raise InputError(message)
            self.warnings.append(message)
            return None
        if re.match(r'^[A-Za-z][A-Za-z0-9+.-]*:', raw) or Path(raw).is_absolute():
            raise InputError(f'Only project-local relative TeX dependencies are allowed: {raw}')
        # File-local resolution first, then the explicitly bounded project root.
        directories = list(dict.fromkeys([parent, *extra_dirs, self.root]))
        for directory in directories:
            candidate = _inside(directory / raw, self.root)
            options = [candidate] if candidate.suffix else [Path(str(candidate) + suffix) for suffix in suffixes]
            for option in options:
                option = _inside(option, self.root)
                if option.is_file():
                    record.update(status='local', path=str(option))
                    return option
        if system and '/' not in raw and '..' not in raw:
            record['status'] = 'system-package'
        else:
            record['status'] = 'missing'
            message = f'Missing local TeX dependency for \\{command}: {raw}'
            if required:
                raise InputError(message)
            self.warnings.append(message)
        return None

    def expand(self, path, stack=(), *, main=False):
        path = _inside(path, self.root)
        if path in stack:
            raise InputError('TeX inclusion cycle: ' + ' -> '.join(p.name for p in (*stack, path)))
        if len(stack) >= 100:
            raise InputError('TeX inclusion depth exceeds 100 files')
        self.add_file(path, 'source' if main else 'tex-include')
        text = _utf8(path)
        mask = _tex_mask(text)
        replacements = []
        position = 0
        while True:
            match = self.COMMANDS.search(mask, position)
            if match is None:
                break
            command = match[1]
            argument_start = match.end()
            _, after_option = _argument(mask, argument_start, optional=True)
            argument, end = _argument(mask, after_option)
            if argument is None and command == 'input':
                plain = re.match(r'\s*([^\s%{}]+)', mask[after_option:])
                if plain:
                    argument, end = plain[1], after_option + plain.end()
            if argument is None:
                if command in ('input', 'include', 'import', 'subimport'):
                    raise InputError(f'Cannot resolve \\{command} without a literal filename in {path}')
                self.warnings.append(f'Unparsed \\{command} argument in {path.name}')
                position = match.end()
                continue
            position = end
            if command == 'graphicspath':
                for directory in re.findall(r'\{([^{}]*)\}', argument):
                    if any(c in directory for c in '\\#$'):
                        self.warnings.append('Dynamic \\graphicspath is unsupported')
                        continue
                    if Path(directory).is_absolute() or re.match(r'^[A-Za-z][A-Za-z0-9+.-]*:', directory):
                        raise InputError('Only project-local relative graphicspath entries are allowed')
                    self.graphic_dirs.append(_inside(path.parent / directory, self.root))
                continue
            if command in ('input', 'include', 'import', 'subimport'):
                if command in ('import', 'subimport'):
                    filename, end = _argument(mask, end)
                    if filename is None:
                        raise InputError(f'Cannot resolve \\{command} without a literal filename')
                    argument = argument.rstrip('/') + '/' + filename
                    position = end
                dependency = self.resolve(argument, path.parent, ('.tex', ''), command, required=True)
                expanded = self.expand(dependency, (*stack, path))
                replacements.append((match.start(), end, expanded))
                continue
            roles = {'includegraphics': ('tex-graphic', self.GRAPHIC_SUFFIXES, False),
                     'bibliography': ('tex-bibliography', ('.bib', ''), False),
                     'addbibresource': ('tex-bibliography', ('.bib', ''), False),
                     'bibliographystyle': ('tex-style', ('.bst', ''), True),
                     'usepackage': ('tex-style', ('.sty', ''), True),
                     'RequirePackage': ('tex-style', ('.sty', ''), True),
                     'documentclass': ('tex-style', ('.cls', ''), True)}
            role, suffixes, system = roles[command]
            arguments = argument.split(',') if command in ('bibliography', 'usepackage', 'RequirePackage') else [argument]
            for item in arguments:
                dependency = self.resolve(item, path.parent, suffixes, command, system=system,
                                          extra_dirs=self.graphic_dirs if command == 'includegraphics' else ())
                if dependency:
                    self.add_file(dependency, role)
                    if role == 'tex-style':
                        # Retain local class/style dependencies as well as the
                        # entry file; their source is not inserted into prose.
                        self.expand(dependency, (*stack, path))
        for start, end, replacement in reversed(replacements):
            text = text[:start] + replacement + text[end:]
        return text


def _pdf(path, resources):
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise InputError('PDF input requires optional dependency pypdf; install it in the runtime used to run this skill') from exc
    captured = []

    class Capture(logging.Handler):
        def emit(self, record):
            captured.append(record.getMessage())

    handler = Capture(logging.WARNING)
    logger = logging.getLogger('pypdf')
    logger.addHandler(handler)
    try:
        with warnings.catch_warnings(record=True) as notices:
            warnings.simplefilter('always')
            reader = PdfReader(str(path))
            encrypted = bool(reader.is_encrypted)
            if encrypted and not reader.decrypt(''):
                raise InputError('PDF is encrypted and requires a password; supply a locally decrypted PDF')
            pages = []
            blank_pages = []
            image_failures = []
            for number, page in enumerate(reader.pages, 1):
                text = page.extract_text() or ''
                if not text.strip():
                    blank_pages.append(number)
                links = []
                try:
                    for image_number, image in enumerate(page.images, 1):
                        record = resources.add(image.data, Path(image.name).suffix,
                            'pdf-image', f'page:{number}/image:{image_number}',
                            page_number=number, original_name=image.name)
                        links.append(resources.markdown(record))
                except InputError:
                    raise
                except Exception as exc:
                    image_failures.append(number)
                    captured.append(f'PDF 第 {number} 页内嵌图片提取失败：{type(exc).__name__}: {exc}')
                pages.append(f'[PDF page {number}]\n{text}' + ('\n\n' + '\n\n'.join(links) if links else ''))
            if not pages or len(blank_pages) == len(pages):
                raise InputError('PDF contains no extractable text (possibly scanned/image-only); provide OCR text or a searchable PDF. OCR is not run automatically')
            metadata = {str(key): str(value) for key, value in (reader.metadata or {}).items()}
            captured.extend(str(notice.message) for notice in notices)
            if blank_pages:
                captured.append('PDF pages without extractable text: ' + ', '.join(map(str, blank_pages)) + '; images/scans were not OCRed')
            captured.append('PDF text follows page order; columns, figures, tables and visual reading order may require review')
            return '\n\n'.join(pages), {'engine': 'pypdf', 'version': _version('pypdf'),
                    'page_count': len(pages), 'empty_text_pages': blank_pages,
                    'image_count': len(resources.resources), 'image_extraction_failed_pages': image_failures,
                    'editable_formula_fidelity': 'unknown',
                    'editable_table_fidelity': 'unknown',
                    'encrypted': encrypted, 'metadata': metadata}, list(dict.fromkeys(captured))
    except InputError:
        raise
    except Exception as exc:
        raise InputError(f'Cannot extract PDF {path.name}: {type(exc).__name__}: {exc}') from exc
    finally:
        logger.removeHandler(handler)


def _docx(path, resources, findings):
    try:
        from docx import Document
        from docx.table import Table
        from docx.text.paragraph import Paragraph
        from lxml.etree import tostring
    except ImportError as exc:
        raise InputError('Word .docx input requires optional dependency python-docx; install it in the runtime used to run this skill') from exc
    try:
        document = Document(str(path))
        counts = {'paragraph_count': 0, 'table_count': 0, 'math_count': 0,
                  'image_count': 0, 'complex_table_count': 0,
                  'text_character_count': 0}
        extraction_warnings = ['DOCX extraction reads main-document paragraphs and editable tables in order; headers, footers, footnotes, comments and page layout require Main review']
        w = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
        m = '{http://schemas.openxmlformats.org/officeDocument/2006/math}'
        a = '{http://schemas.openxmlformats.org/drawingml/2006/main}'
        r = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
        v = '{urn:schemas-microsoft-com:vml}'

        def paragraph_text(paragraph):
            def inline(node):
                if node.tag == m + 'oMath':
                    counts['math_count'] += 1
                    identifier = f'{resources.source_id}-M{counts["math_count"]:03}'
                    linear = ''.join(child.text or '' for child in node.iter(m + 't'))
                    check_no_credentials(linear, 'Office Math source input')
                    counts['text_character_count'] += len(linear.strip())
                    findings.append(_finding(resources.source_id, 'docx_omml_requires_editable_math',
                        '检测到 Word 原生公式；线性文本不能保留分数、上下标等结构。',
                        '通过 Pandoc 或主代理工具恢复为可编辑的 Markdown/LaTeX 公式，并逐式对照原文。不得使用公式截图。',
                        equation_id=identifier, linear_text=linear,
                        original_omml=tostring(node, encoding='unicode')))
                    return f'[待恢复为可编辑公式：{identifier}]'
                if node.tag in (w + 't', m + 't'):
                    return node.text or ''
                if node.tag == w + 'tab':
                    return '\t'
                if node.tag in (w + 'br', w + 'cr'):
                    return '\n'
                if node.tag in (a + 'blip', v + 'imagedata'):
                    relation_id = node.get(r + 'embed') or node.get(r + 'link') or node.get(r + 'id')
                    if not relation_id:
                        return ''
                    relation = paragraph.part.rels.get(relation_id)
                    if relation is None or relation.is_external:
                        findings.append(_finding(resources.source_id, 'docx_external_image_unavailable',
                            'Word 文档使用外部链接图片，适配器不会联网读取。',
                            '主代理取得经授权的本地原图，并替换 Markdown 图片链接。',
                            relationship_id=relation_id))
                        return f'[待补充本地图片：{relation_id}]'
                    part = relation.target_part
                    record = resources.add(part.blob, Path(str(part.partname)).suffix,
                        'docx-image', str(part.partname),
                        content_type=part.content_type)
                    counts['image_count'] += 1
                    return resources.markdown(record)
                return ''.join(inline(child) for child in node)

            text = inline(paragraph._p)
            counts['text_character_count'] += len(text.strip())
            heading = re.fullmatch(r'Heading\s+([1-9])', paragraph.style.name or '', re.I) if paragraph.style else None
            return ('#' * int(heading[1]) + ' ' + text) if heading and text else text

        def blocks(container):
            if hasattr(container, 'iter_inner_content'):
                yield from container.iter_inner_content()
            else:
                element = container.element.body if hasattr(container, 'element') else container._tc
                for child in element.iterchildren():
                    if child.tag == w + 'p':
                        yield Paragraph(child, container)
                    elif child.tag == w + 'tbl':
                        yield Table(child, container)

        def extract(container):
            result = []
            for block in blocks(container):
                if isinstance(block, Paragraph):
                    counts['paragraph_count'] += 1
                    result.append(paragraph_text(block))
                elif isinstance(block, Table):
                    counts['table_count'] += 1
                    identifier = f'{resources.source_id}-T{counts["table_count"]:03}'
                    complex_table = any(node.tag in (w + 'gridSpan', w + 'vMerge') for node in block._tbl.iter()) or bool(block._tbl.xpath('.//w:tc/w:tbl'))
                    if complex_table:
                        counts['complex_table_count'] += 1
                    rows = []
                    for row in block.rows:
                        # A merged cell's repeated grid slots are emitted once.
                        seen = set()
                        cells = []
                        for cell in row.cells:
                            if cell._tc in seen:
                                continue
                            seen.add(cell._tc)
                            cells.append('<br>'.join(part for part in extract(cell) if part).replace('|', '\\|').replace('\n', '<br>'))
                        rows.append(cells)
                    width = max((len(row) for row in rows), default=0)
                    if width:
                        result.append('| ' + ' | '.join(f'列 {index + 1}' for index in range(width)) + ' |')
                        result.append('| ' + ' | '.join('---' for _ in range(width)) + ' |')
                        result.extend('| ' + ' | '.join(row + [''] * (width - len(row))) + ' |' for row in rows)
                    findings.append(_finding(resources.source_id, 'docx_table_requires_review',
                        '表格已转成可编辑 Markdown；合并单元格或嵌套结构可能被展平。' if complex_table else '表格已转成可编辑 Markdown，原始各行均保留，列标题为辅助编号。',
                        '主代理逐格核对行列、数值、表头与合并关系；复杂结构恢复为可编辑表格，不得使用表格截图。',
                        status='unresolved' if complex_table else 'needs_visual_verification',
                        table_id=identifier, complex_structure=complex_table,
                        original_table_xml=block._tbl.xml if complex_table else None))
            return result

        text = '\n\n'.join(extract(document))
        if not counts['text_character_count']:
            raise InputError('Word document contains no extractable main-document text; provide text or an OCR-enabled .docx')
        if any(node.tag in (w + 'sdt', w + 'ins', w + 'del', w + 'txbxContent') for node in document.element.body.iter()):
            extraction_warnings.append('DOCX has content controls, tracked revisions or text boxes; these features may be incomplete in paragraph/table extraction; review normalized source')
            findings.append(_finding(resources.source_id, 'docx_unhandled_body_features',
                '文档含内容控件、修订或文本框，正文提取可能遗漏内容。',
                '主代理检查原文并补齐这些内容，确认修订取舍。'))
        if counts['math_count']:
            extraction_warnings.append('Office Math requires editable conversion and visual verification; linear text is retained only in preparation findings, never accepted as equation fidelity')
        props = document.core_properties
        metadata = {name: str(getattr(props, name) or '') for name in ('title', 'subject', 'author', 'keywords')}
        return text, {'engine': 'python-docx', 'version': _version('python-docx'), **counts,
                      'editable_formula_fidelity': 'unresolved' if counts['math_count'] else 'not_determined_from_visual_content',
                      'editable_table_fidelity': 'requires_visual_verification' if counts['table_count'] else 'not_determined_from_visual_content',
                      'metadata': metadata}, extraction_warnings
    except InputError:
        raise
    except Exception as exc:
        raise InputError(f'Cannot extract Word document {path.name}: {type(exc).__name__}: {exc}') from exc


def _pandoc_docx(path, resources, findings, extraction, pandoc_bin):
    """Explicit local conversion; resources are extracted by us, not fetched."""
    executable = shutil.which(str(pandoc_bin or 'pandoc'))
    if executable is None and pandoc_bin is None:
        try:
            import pypandoc
            executable = str(pypandoc.get_pandoc_path())
        except (ImportError, OSError, RuntimeError):
            pass
    if executable is None:
        raise InputError('选择了 Pandoc Word 公式转换，但未找到本地 pandoc；请指定 pandoc_bin，或由主代理工具完成可编辑转换')
    command = [executable, '--from=docx',
               '--to=markdown+tex_math_dollars+pipe_tables-grid_tables-simple_tables-multiline_tables',
               '--wrap=none', str(path)]
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=60, check=False, shell=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InputError(f'本地 Pandoc 转换未完成：{type(exc).__name__}') from exc
    if result.returncode:
        raise InputError(f'本地 Pandoc 转换失败，退出码 {result.returncode}')
    try:
        text = result.stdout.decode('utf-8-sig')
        notices = result.stderr.decode('utf-8-sig')
    except UnicodeError as exc:
        raise InputError('Pandoc 未返回有效 UTF-8 Markdown') from exc
    if not text.strip():
        raise InputError('Pandoc 未返回可编辑 Markdown 内容')
    for record in resources.resources:
        member = record['container_locator'].lstrip('/')
        # Pandoc DOCX reader emits media/... while OOXML parts use word/media/...
        original = member.removeprefix('word/')
        text = text.replace('](' + original + ')', '](' + record['output_relative_path'] + ')')
        text = text.replace('](<' + original + '>)', '](' + record['output_relative_path'] + ')')
        text = text.replace('](' + original + ' ', '](' + record['output_relative_path'] + ' ')
    expected_images = {record['output_relative_path'] for record in resources.resources}
    for wrapped, unwrapped in re.findall(r'!\[[^\]]*\]\((?:<([^>]+)>|([^\s)]+))', text):
        target = wrapped or unwrapped
        if target not in expected_images:
            findings.append(_finding(resources.source_id, 'pandoc_image_reference_unresolved',
                'Pandoc 输出含尚未匹配本地图片资源的 Markdown 图片链接。',
                '主代理取得正确本地原图并修正链接；适配器不会联网下载或生成替代截图。',
                markdown_target=target))
    math = re.findall(r'(?<!\\)\$\$[\s\S]+?(?<!\\)\$\$|(?<![\\$])\$(?!\$)(?:\\.|[^$\n])+(?<!\\)\$(?!\$)', text)
    math_count = extraction['math_count']
    extraction.update(preliminary_engine='python-docx', engine='pandoc',
        pandoc_executable=executable, pandoc_math_count=len(math),
        editable_formula_fidelity='requires_visual_verification' if len(math) >= math_count else 'unresolved')
    if math_count and len(math) >= math_count:
        for finding in findings:
            if finding['code'] == 'docx_omml_requires_editable_math':
                finding.update(status='needs_visual_verification',
                    message='Pandoc 已生成可编辑 Markdown/LaTeX 公式；公式正确性仍需主代理逐式视觉核对。')
    elif math_count:
        findings.append(_finding(resources.source_id, 'pandoc_math_conversion_incomplete',
            'Pandoc 输出中可识别的公式数少于 Word 原生公式数，不能认定公式已完整保留。',
            '主代理使用原始 OMML 和原文逐式恢复遗漏或损坏的可编辑公式，禁止图片替代。',
            expected_math_count=math_count, extracted_math_count=len(math)))
    if notices.strip():
        findings.append(_finding(resources.source_id, 'pandoc_conversion_warnings',
            '本地 Pandoc 返回转换警告。', '主代理检查转换警告并确认是否有内容损失。',
            converter_warnings=notices.strip()))
    return text


def _tex_main(path, root, main_tex):
    if main_tex is not None:
        selected = Path(main_tex)
        selected = _inside(selected if selected.is_absolute() else root / selected, root)
        if selected.suffix.lower() != '.tex' or not selected.is_file():
            raise InputError(f'TeX main file does not exist or is not .tex: {selected}')
        return selected
    if path.is_file():
        return _inside(path, root)
    candidates = []
    for candidate in sorted(path.rglob('*.tex')):
        candidate = _inside(candidate, root)
        if candidate.is_file() and re.search(r'\\documentclass(?![A-Za-z])', _tex_mask(_utf8(candidate))):
            candidates.append(candidate)
    if len(candidates) != 1:
        raise InputError('TeX project requires an explicit main_tex when discovery is ambiguous; '
                         f'found {len(candidates)} files with an active \\documentclass')
    return candidates[0]


def load_sources(paths, *, project_root=None, main_tex=None, prepared_dir=None,
                 docx_engine='python-docx', pandoc_bin=None):
    """Extract local sources, retaining exact UTF-8 text/newlines for TXT/MD.

    ``project_root`` bounds TeX reads (defaults to each TeX source's parent).
    A directory is one TeX project; ``main_tex`` selects its main file. With
    multiple TeX source entries, select each main using its explicit file path.
    ``prepared_dir`` opts into local embedded-image extraction under resources/.
    Without it no resources are written. ``docx_engine='pandoc'`` explicitly
    opts into a local Pandoc subprocess for editable Word math. No OCR, model,
    network fetch, formula screenshot or table screenshot is performed.
    All rich-format drafts require Main's visual and editable-content review.
    """
    if isinstance(paths, (str, Path)):
        paths = [paths]
    paths = [Path(path).resolve() for path in paths]
    if not paths:
        raise InputError('Provide at least one source file or TeX project')
    if docx_engine not in ('python-docx', 'pandoc'):
        raise InputError('docx_engine 必须为 python-docx 或 pandoc')
    prepared_root = Path(prepared_dir).resolve() if prepared_dir is not None else None
    if prepared_root is not None and prepared_root.exists() and not prepared_root.is_dir():
        raise InputError('prepared_dir 必须为目录')
    if main_tex is not None and sum(path.is_dir() or path.suffix.lower() == '.tex' for path in paths) != 1:
        raise InputError('main_tex requires exactly one TeX source/project entry')
    explicit_root = Path(project_root).resolve() if project_root is not None else None
    if explicit_root is not None and not explicit_root.is_dir():
        raise InputError(f'TeX project root is not a directory: {explicit_root}')
    sources = []
    provenance = {'schema_version': 2, 'sources': [], 'files': [], 'warnings': [],
                  'resources': [], 'preparation_findings': []}
    for number, supplied in enumerate(paths, 1):
        if not supplied.exists():
            raise InputError(f'Source does not exist: {supplied}')
        if not supplied.is_file() and not supplied.is_dir():
            raise InputError(f'Source must be a regular file or a TeX project directory: {supplied}')
        source_id = f'S{number:03}'
        is_tex = supplied.is_dir() or supplied.suffix.lower() == '.tex'
        root = explicit_root if is_tex and explicit_root else (supplied if supplied.is_dir() else supplied.parent)
        path = _tex_main(supplied, root, main_tex) if is_tex else supplied
        suffix = path.suffix.lower()
        source_warnings = []
        findings = []
        resources = _Resources(source_id, path, prepared_root)
        if suffix in ('.txt', '.md'):
            text = _utf8(path)
            extraction = {'engine': 'utf-8-sig', 'newlines': 'preserved', 'bom_stripped': path.read_bytes().startswith(b'\xef\xbb\xbf')}
            files = [_file_record(path, root, source_id, 'source')]
        elif suffix == '.tex':
            project = _TexProject(root, source_id)
            text = project.expand(path, main=True)
            # A supplied compiled bibliography is a useful local fallback when
            # BibTeX/Biber is unavailable. Retain only this main project's
            # conventional sibling, never discover arbitrary build files.
            if re.search(r'\\(?:bibliography|addbibresource|printbibliography)(?![A-Za-z])', _tex_mask(text)):
                supplied_bbl = _inside(path.with_suffix('.bbl'), root)
                if supplied_bbl.is_file():
                    project.add_file(supplied_bbl, 'tex-bibliography-output')
                    project.references.append({'command': 'supplied-bibliography-output',
                        'requested': path.with_suffix('.bbl').name,
                        'status': 'local', 'path': str(supplied_bbl)})
            source_warnings = project.warnings
            extraction = {'engine': 'bounded-literal-tex', 'compiled': False,
                          'references': project.references,
                          'limitations': 'Literal input/include/import/subimport are expanded; macros and conditionals are not evaluated; assets are retained without interpreting their contents'}
            source_warnings.append('TeX was read without execution; macro expansion, conditionals, bibliography rendering and figure contents require review')
            files = list(project.files.values())
            findings.append(_finding(source_id, 'tex_markdown_preparation_required',
                '当前仅合并了 TeX 源码，尚未生成并核对可编辑 Markdown。',
                '主代理用 LaTeX/文档工具转换正文、公式和表格，核对图表与文献后再确认准备完成。'))
        elif suffix == '.pdf':
            text, extraction, source_warnings = _pdf(path, resources)
            files = [_file_record(path, root, source_id, 'source')]
            findings.append(_finding(source_id, 'pdf_editable_fidelity_unknown',
                'PDF 原始提取不能证明公式、表格、阅读顺序和图文关联完整；公式和表格是否存在仍为未知。',
                '主代理逐页视觉核对，并将发现的公式和表格恢复为可编辑 Markdown/LaTeX/表格。不得使用公式或表格截图作为替代。',
                page_count=extraction['page_count'], formula_presence='unknown', table_presence='unknown'))
        elif suffix == '.docx':
            text, extraction, source_warnings = _docx(path, resources, findings)
            if docx_engine == 'pandoc':
                text = _pandoc_docx(path, resources, findings, extraction, pandoc_bin)
            files = [_file_record(path, root, source_id, 'source')]
        elif suffix == '.doc':
            raise InputError('Legacy .doc input is unsupported; save/convert it locally to .docx before running this skill')
        else:
            raise InputError(f'Unsupported source format {suffix!r}; use .txt, .md, .tex, .pdf or .docx')
        if not text.strip():
            raise InputError(f'Source has no extractable text: {path}')
        check_no_credentials(text, 'source input')
        findings.extend(resources.findings)
        sources.append({'source_id': source_id, 'name': path.name, 'text': text})
        provenance['sources'].append({'source_id': source_id, 'path': str(path),
            'format': suffix[1:], 'project_root': str(root), 'extraction': extraction,
            'warnings': source_warnings, 'preparation_findings': findings})
        provenance['files'].extend(files)
        provenance['files'].extend(resources.files)
        provenance['resources'].extend(resources.resources)
        provenance['preparation_findings'].extend(findings)
        provenance['warnings'].extend(f'{source_id}: {notice}' for notice in source_warnings)
    provenance['preparation'] = {
        'status': 'requires_main_agent_preparation',
        'editable_markdown_verified': False,
        'visual_review_required': any(source['format'] in ('tex', 'pdf', 'docx') for source in provenance['sources']),
        'unresolved_finding_count': sum(finding['status'] == 'unresolved' for finding in provenance['preparation_findings']),
        'equation_table_screenshots_allowed': False,
        'review_owner': 'main-agent',
        'message': '这些内容是准备草稿，只有主代理完成可编辑内容恢复、视觉核对并确认后，才能交给 A/B/Writer。'}
    return sources, provenance
