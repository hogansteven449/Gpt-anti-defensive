"""Deterministic, local output adapters. No writing or model calls occur here.

The UTF-8 writer responses are always saved separately, byte for byte. TeX
assembly inserts a fragment immediately before the real ``end{document}``;
removing that insertion reconstructs the original response exactly.
"""
from __future__ import annotations

import base64
from collections import Counter
from copy import deepcopy
import hashlib
import html
import importlib.util
from io import BytesIO
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Any
from urllib.parse import unquote
import zipfile
import xml.etree.ElementTree as ET

from .util import ValidationError, json_write

FORMATS = ("markdown", "latex", "docx", "pdf")
NUMERIC_CSL = '''<?xml version="1.0" encoding="utf-8"?>
<style xmlns="http://purl.org/net/xbiblio/csl" version="1.0" class="in-text">
<info><title>Reader aware numeric citations</title><id>urn:readeraware:numeric</id><updated>2026-10-26T00:00:00+00:00</updated></info>
<citation collapse="citation-number"><sort><key variable="citation-number"/></sort><layout prefix="[" suffix="]" delimiter=", "><text variable="citation-number"/></layout></citation>
<bibliography entry-spacing="0"><layout suffix="."><text variable="citation-number" prefix="[" suffix="] "/><names variable="author"><name and="text" initialize-with=". "/></names><text variable="title" prefix=". "/><group prefix=". " delimiter=", "><text variable="container-title"/><date variable="issued"><date-part name="year"/></date><text variable="page"/></group></layout></bibliography>
</style>
'''
VERBATIM_ENVIRONMENTS = {"verbatim", "verbatim*", "Verbatim", "BVerbatim", "LVerbatim",
                         "lstlisting", "minted", "comment"}
# This conservative guard complements a no-shell-escape engine invocation. It
# deliberately refuses expansion primitives which could conceal these commands.
# It is not a replacement for an OS sandbox for arbitrary hostile TeX programs.
UNSAFE_COMMANDS = {"write", "write18", "openout", "openin", "read", "readline", "closeout",
    "closein", "immediate", "special", "directlua", "latelua", "luafunction", "luacode",
    "ShellEscape", "shellescape", "sys_shell_now", "sys_shell_shipout", "pdfshellescape",
    "pdffiledump", "pdffilesize", "pdffilemoddate", "pdfmdfivesum", "pdfobj", "pdfextension",
    "pdfannot", "pdfcatalog", "pdfnames", "pdftrailer", "pdfxform", "pdfrefxform",
    "catcode", "csname", "endcsname", "expandafter", "noexpand", "scantokens", "everyjob",
    "everyeof", "loop", "repeat", "endinput", "inputlineno", "font", "newread", "newwrite",
    "def", "edef", "gdef", "xdef", "let", "futurelet", "escapechar", "endlinechar",
    "CatchFileDef", "CatchFileEdef", "verbatiminput", "VerbatimInput", "lstinputlisting",
    "inputminted", "py", "pyc", "python", "pythonc", "Rcode", "sys_get",
    "sys_get_shell", "ior_open", "iow_open", "ior_get", "iow_now"}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def _exact_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _masked_tex(text: str) -> str:
    """Mask comments and verbatim content without changing character offsets."""
    chars = list(text)
    i = 0
    while i < len(text):
        if text[i] == "%":
            end = text.find("\n", i)
            end = len(text) if end < 0 else end
            chars[i:end] = " " * (end - i)
            i = end
        elif text[i] == "\\":
            # A control symbol such as \\% consumes its second character.
            if i + 1 < len(text) and not text[i + 1].isalpha():
                i += 2
                continue
            match = re.match(r"\\([A-Za-z]+)(\*)?", text[i:])
            if not match:
                i += 1
                continue
            name = match[1]
            end = i + len(match[0])
            if name in {"verb", "Verb"} and end < len(text):
                delimiter = text[end]
                stop = text.find(delimiter, end + 1)
                _require(stop >= 0 and "\n" not in text[end:stop], "Unclosed TeX verbatim command")
                stop += 1
                chars[i:stop] = " " * (stop - i)
                i = stop
            elif name == "begin":
                env = re.match(r"\s*\{([^{}]+)\}", text[end:])
                if env and env[1] in VERBATIM_ENVIRONMENTS:
                    close = re.search(r"\\end\s*\{" + re.escape(env[1]) + r"\}", text[end + len(env[0]):])
                    _require(close is not None, "Unclosed TeX verbatim environment: " + env[1])
                    stop = end + len(env[0]) + close.end()
                    # Keep environment boundaries visible so the safety guard
                    # can reject executable environments such as minted.
                    content_start = end + len(env[0])
                    content_end = content_start + close.start()
                    chars[content_start:content_end] = " " * (content_end - content_start)
                    i = stop
                else:
                    i = end
            else:
                i = end
        else:
            i += 1
    return "".join(chars)


def _commands(masked: str):
    i = 0
    while i < len(masked):
        if masked[i] != "\\":
            i += 1
            continue
        if i + 1 < len(masked) and not masked[i + 1].isalpha():
            i += 2
            continue
        match = re.match(r"\\([A-Za-z]+)", masked[i:])
        if not match:
            i += 1
            continue
        end = i + len(match[0])
        yield match[1], i, end
        i = end


def _document_tokens(text: str) -> tuple[str, dict[str, list[tuple[int, int]]]]:
    masked = _masked_tex(text)
    tokens: dict[str, list[tuple[int, int]]] = {"class": [], "begin": [], "end": []}
    for name, start, end in _commands(masked):
        if name == "documentclass":
            arg = re.match(r"\s*(?:\[[^\]]*\]\s*)?\{[^{}]+\}", masked[end:])
            _require(arg is not None, "Malformed TeX documentclass")
            tokens["class"].append((start, end + len(arg[0])))
        elif name in {"begin", "end"}:
            arg = re.match(r"\s*\{\s*document\s*\}", masked[end:])
            if arg:
                tokens[name].append((start, end + len(arg[0])))
    return masked, tokens


def validate_latex_safety(text: str) -> dict:
    """Reject direct I/O, code execution, concealed commands and remote assets."""
    masked = _masked_tex(text)
    _require("^^" not in masked, "TeX ^^ character escapes are unsafe for local compilation")
    for name, _, end in _commands(masked):
        _require(name not in UNSAFE_COMMANDS, "Unsafe or unsupported TeX command: \\" + name)
        if name in {"input", "include", "includegraphics", "bibliography", "addbibresource",
                    "documentclass", "usepackage", "RequirePackage", "bibliographystyle",
                    "IfFileExists", "InputIfFileExists"}:
            arg = re.match(r"\s*(?:\[[^\]]*\]\s*)?\{([^{}]+)\}", masked[end:])
            _require(arg is not None, "TeX file references must use literal braced local paths")
            for item in arg[1].split(","):
                path = item.strip()
                _require(not re.search(r"[\\|~$#]|(?:^|[ /])\.\.(?:[/ ]|$)|^[A-Za-z]:|^/|[a-z]+://", path, re.I),
                         "Unsafe or nonliteral TeX file reference: " + path)
    _require(not re.search(r"\\begin\s*\{(?:luacode\*?|filecontents\*?|pythontex|pycode|minted)\}", masked),
             "TeX executable or file-writing environments are unsupported")
    return {"safe_static_subset": True, "shell_escape": False, "network_downloads": False,
            "sandbox_note": "Conservative static guard and restricted native engine; use an OS sandbox for hostile TeX"}


def validate_latex_document(text: str) -> dict:
    _require(isinstance(text, str) and bool(text.strip()), "Empty LaTeX document")
    masked, tokens = _document_tokens(text)
    for kind in ("class", "begin", "end"):
        _require(len(tokens[kind]) == 1, "LaTeX requires exactly one real documentclass, begin{document}, and end{document}")
    class_start, class_end = tokens["class"][0]
    begin_start, begin_end = tokens["begin"][0]
    end_start, end_end = tokens["end"][0]
    _require(class_end <= begin_start < begin_end <= end_start, "LaTeX document boundaries are out of order")
    _require(not masked[end_end:].strip(), "Active content after end{document} would not be rendered")
    safety = validate_latex_safety(text)
    return {"valid": True, "documentclass_offset": class_start, "body_start": begin_end,
            "body_end": end_start, "end_document_start": end_start,
            "end_document_end": end_end, **safety}


def validate_latex_fragment(text: str) -> dict:
    _require(isinstance(text, str), "LaTeX fragment must be a string")
    _, tokens = _document_tokens(text)
    _require(not any(tokens.values()), "Future LaTeX must be a fragment without documentclass or document boundaries")
    masked = _masked_tex(text)
    for name, _, _ in _commands(masked):
        _require(name not in {"usepackage", "RequirePackage", "PassOptionsToPackage"},
                 "Future LaTeX must not change the preamble")
    return {"valid": True, "fragment": True, **validate_latex_safety(text)}


def assemble_latex(main: str, future: str) -> str:
    info = validate_latex_document(main)
    validate_latex_fragment(future)
    if not future:
        return main
    offset = info["end_document_start"]
    insertion = "\n\n" + future + "\n"
    result = main[:offset] + insertion + main[offset:]
    _require(result[:offset] + result[offset + len(insertion):] == main,
             "Frozen LaTeX body reconstruction failed")
    validate_latex_document(result)
    return result


def _find_pandoc() -> str | None:
    found = shutil.which("pandoc")
    if found:
        return found
    try:
        import pypandoc
        # Lookup only: never call download_pandoc or any installer.
        return str(pypandoc.get_pandoc_path())
    except (ImportError, OSError, RuntimeError):
        return None


def _find_compiler(compiler=None) -> str | None:
    if compiler in (False, "off"):
        return None
    if compiler not in (None, True, "auto", "required"):
        candidate = str(compiler)
        found = shutil.which(candidate) or (candidate if Path(candidate).is_file() else None)
        _require(found is not None, "Requested local TeX compiler is unavailable: " + candidate)
        _require(Path(found).stem.lower() in {"xelatex", "pdflatex"},
                 "Use a local xelatex or pdflatex executable; wrappers and Lua/network engines are unsupported")
        return str(found)
    return shutil.which("xelatex") or shutil.which("pdflatex")


def preflight_outputs(output_formats, content_format="markdown", compiler=None) -> dict:
    formats = list(dict.fromkeys(output_formats or [content_format]))
    _require(content_format in {"markdown", "latex"}, "Content format must be markdown or latex")
    _require(bool(formats) and all(f in FORMATS for f in formats), "Unknown output format")
    python_docx = importlib.util.find_spec("docx") is not None
    reportlab = importlib.util.find_spec("reportlab") is not None
    # The default Markdown fixture path remains stdlib-only and never launches
    # a native tool. Probe only dependencies relevant to requested exports.
    need_pandoc = (content_format == "latex" and any(f in {"markdown", "docx", "pdf"} for f in formats)) or ("docx" in formats and not python_docx)
    pandoc = _find_pandoc() if need_pandoc else None
    need_engine = content_format == "latex" or "latex" in formats or ("pdf" in formats and not reportlab) or compiler not in (None, True, False, "auto", "off")
    engine = _find_compiler(compiler) if need_engine else None
    compiler_error = None
    if engine:
        try:
            probe = subprocess.run([engine, "--version"], text=True, encoding="utf-8", errors="replace",
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, check=False, shell=False)
            if probe.returncode:
                compiler_error = _normal(probe.stderr or probe.stdout)[-1500:]
                engine = None
        except (OSError, subprocess.TimeoutExpired) as exc:
            compiler_error = str(exc)
            engine = None
    if compiler not in (None, True, False, "auto", "off"):
        _require(engine is not None, "Requested native TeX compiler is not ready: " + str(compiler_error or "not installed"))
    dependencies = {"pandoc": pandoc, "latex_compiler": engine,
                    "latex_compiler_error": compiler_error,
                    "python_docx": python_docx, "reportlab": reportlab,
                    "pypdf": importlib.util.find_spec("pypdf") is not None}
    if content_format == "latex" and any(f in {"markdown", "docx"} for f in formats):
        _require(pandoc is not None, "LaTeX to Markdown/DOCX requires local Pandoc (or pypandoc_binary); no formula-dropping fallback is used")
    if "docx" in formats and not pandoc:
        _require(dependencies["python_docx"], "DOCX export requires python-docx or local Pandoc")
    if "pdf" in formats:
        _require(dependencies["pypdf"], "PDF text verification requires pypdf")
        if content_format == "latex":
            _require(engine is not None, "LaTeX PDF export requires a local xelatex or pdflatex installation that is ready to run. " + str(compiler_error or ""))
        else:
            _require(dependencies["reportlab"] or engine is not None,
                     "Markdown PDF export requires ReportLab, or a local TeX compiler")
    if compiler == "required":
        _require(engine is not None, "Required LaTeX compilation needs a local xelatex or pdflatex installation")
    return {"output_formats": formats, "content_format": content_format,
            "dependencies": dependencies, "capabilities": {"local_only": True,
            "latex_native_pdf": engine is not None, "math_docx": pandoc is not None,
            "semantic_rewriting": False}}


def _blocks(markdown: str) -> list[dict]:
    """A bounded Markdown parser shared by the basic native adapters."""
    lines = markdown.splitlines()
    blocks = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        fence = re.match(r"^\s*(`{3,}|~{3,})(.*)$", line)
        if fence:
            j = i + 1
            while j < len(lines) and not re.match(r"^\s*" + re.escape(fence[1][0]) + "{" + str(len(fence[1])) + r",}\s*$", lines[j]):
                j += 1
            _require(j < len(lines), "Unclosed Markdown code fence")
            blocks.append({"kind": "code", "text": "\n".join(lines[i + 1:j])})
            i = j + 1
            continue
        if i + 1 < len(lines) and "|" in line and re.match(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$", lines[i + 1]):
            def cells(row):
                return [c.strip() for c in row.strip().strip("|").split("|")]
            rows = [cells(line)]
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(cells(lines[i])); i += 1
            _require(all(len(row) == len(rows[0]) for row in rows), "Markdown table has inconsistent column counts")
            blocks.append({"kind": "table", "rows": rows})
            continue
        heading = re.match(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line)
        item = re.match(r"^\s*(?:([-+*])|([0-9]+)[.)])\s+(.+)$", line)
        if heading:
            blocks.append({"kind": "heading", "level": len(heading[1]), "text": heading[2]})
        elif item:
            blocks.append({"kind": "ordered" if item[2] else "bullet", "text": item[3]})
        elif line.lstrip().startswith(">"):
            blocks.append({"kind": "quote", "text": re.sub(r"^\s*>\s?", "", line)})
        elif line.strip() == "$$":
            j = i + 1
            while j < len(lines) and lines[j].strip() != "$$":
                j += 1
            _require(j < len(lines), "Unclosed Markdown display math")
            blocks.append({"kind": "math", "text": "\n".join(lines[i + 1:j])})
            i = j
        else:
            parts = [line]
            while i + 1 < len(lines) and lines[i + 1].strip() and not re.match(r"^\s*(?:#{1,6}\s|[-+*]\s|\d+[.)]\s|```|~~~|>|\$\$)", lines[i + 1]):
                if i + 2 < len(lines) and "|" in lines[i + 1] and re.match(r"^[ |:\-]+$", lines[i + 2]):
                    break
                i += 1; parts.append(lines[i])
            blocks.append({"kind": "paragraph", "text": "\n".join(parts)})
        i += 1
    return blocks


INLINE = re.compile(r"(`+)([^`]*?)\1|\*\*(.+?)\*\*|__(.+?)__|(?<!\*)\*([^*]+)\*|(?<!_)_([^_]+)_|!\[([^\]]*)\]\(([^)]+)\)|\[([^\]]+)\]\(([^)]+)\)|\$\$([\s\S]+?)\$\$|(?<!\\)\$([^\n$]+?)(?<!\\)\$|\\\(([\s\S]+?)\\\)|\\\[([\s\S]+?)\\\]")


def _inline(text: str):
    start = 0
    for match in INLINE.finditer(text):
        if match.start() > start:
            yield "text", text[start:match.start()]
        if match[1] is not None: yield "code", match[2]
        elif match[3] is not None or match[4] is not None: yield "bold", match[3] or match[4]
        elif match[5] is not None or match[6] is not None: yield "italic", match[5] or match[6]
        elif match[7] is not None: yield "image", (match[7], match[8])
        elif match[9] is not None: yield "link", (match[9], match[10])
        elif match[11] is not None: yield "display_math", match[11]
        else: yield "math", next(x for x in (match[12], match[13], match[14]) if x is not None)
        start = match.end()
    if start < len(text): yield "text", text[start:]


def _has_math(text: str) -> bool:
    return any(block["kind"] != "code" and (block["kind"] == "math" or any(kind in {"math", "display_math"} for kind, _ in _inline(block.get("text", "")))
               or any(any(kind in {"math", "display_math"} for kind, _ in _inline(cell)) for row in block.get("rows", []) for cell in row)
               ) for block in _blocks(text))


def _has_structured_citations(text: str) -> bool:
    for block in _blocks(text):
        if block["kind"] == "code": continue
        texts = [cell for row in block["rows"] for cell in row] if block["kind"] == "table" else [block.get("text", "")]
        if any(re.search(r"\[[^\]\n]*@[A-Za-z0-9_][^\]\n]*\]", value)
               for item in texts for kind, value in _inline(item) if kind == "text"):
            return True
    return False


def _guard_bounded_markdown_tables(markdown: str) -> None:
    """Reject table syntaxes the bounded renderer cannot make editable.

    Markdown-only storage remains valid. Code examples and HTML comments are
    not document tables and may be retained as literal source examples.
    """
    visible = []
    for block in _blocks(markdown):
        if block["kind"] == "code": continue
        texts = [cell for row in block["rows"] for cell in row] if block["kind"] == "table" else [block.get("text", "")]
        for text in texts:
            segments = []
            for kind, value in _inline(text):
                if kind in {"text", "bold", "italic"}: segments.append(value)
                elif kind == "link": segments.append(value[0])
            visible.append("".join(segments))
    active = re.sub(r"<!--[\s\S]*?(?:-->|$)", "", "\n".join(visible))
    if re.search(r"<\s*/?\s*(?:table|thead|tbody|tfoot|tr|td|th|caption|colgroup|col)\b[^>]*>", active, re.I):
        raise ValidationError("Semantic HTML tables require main-agent editable structural conversion with document tools; the bounded exporter will not print raw HTML or replace merged cells with screenshots")
    if re.search(r"^\s*\+(?:[-=]{2,}\+)+\s*$", active, re.M) or re.search(r"^\s*-{3,}(?:[ \t]+-{3,})+[ \t]*$", active, re.M):
        raise ValidationError("Pandoc grid/multiline tables require main-agent editable structural conversion with document tools; the bounded exporter supports pipe tables and will not print table borders as prose or use screenshots")


def _tex_escape(text: str) -> str:
    replacements = {"\\": r"\textbackslash{}", "{": r"\{", "}": r"\}", "&": r"\&", "%": r"\%",
                    "$": r"\$", "#": r"\#", "_": r"\_", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}
    return "".join(replacements.get(char, char) for char in text)


def _tex_inline(text: str) -> str:
    parts = []
    for kind, value in _inline(text):
        if kind in {"math", "display_math"}:
            parts.append((r"\[" if kind == "display_math" else r"\(") + value + (r"\]" if kind == "display_math" else r"\)"))
        elif kind in {"bold", "italic", "code"}:
            command = {"bold": "textbf", "italic": "emph", "code": "texttt"}[kind]
            parts.append("\\" + command + "{" + _tex_escape(value) + "}")
        elif kind == "link":
            parts.append(_tex_escape(value[0] + " (" + value[1] + ")"))
        elif kind == "image":
            target = unquote(value[1].strip().strip("<>"))
            _require(not re.search(r"[\\{}|~$#%]|(?:^|/)\.\.(?:/|$)|^[A-Za-z]:|^/|[a-z]+://", target, re.I),
                     "Images must use safe relative local asset paths")
            parts.append(r"\includegraphics[width=0.9\linewidth,keepaspectratio]{" + target + "}")
        else:
            parts.append(_tex_escape(value.replace("\n", " ")))
    return "".join(parts)


def markdown_to_latex(markdown: str, *, fragment: bool = False, preamble_text: str | None = None) -> str:
    """Convert the supported Markdown subset without changing prose or math."""
    _guard_bounded_markdown_tables(markdown)
    _require(not _has_structured_citations(markdown),
             "Resolve structured [@key] citations and bibliography metadata to visible numbered citations/references with local document tools before direct Markdown-to-LaTeX/PDF export; source Markdown remains preserved")
    parts = []
    blocks = _blocks(markdown)
    active_list = None
    for block in blocks:
        kind = block["kind"]
        listing = "enumerate" if kind == "ordered" else "itemize" if kind == "bullet" else None
        if listing != active_list:
            if active_list: parts.append(r"\end{" + active_list + "}")
            if listing: parts.append(r"\begin{" + listing + "}")
            active_list = listing
        if listing:
            parts.append(r"\item " + _tex_inline(block["text"]))
        elif kind == "heading":
            command = {1: "section", 2: "subsection", 3: "subsubsection"}.get(block["level"], "paragraph")
            parts.append("\\" + command + "*{" + _tex_inline(block["text"]) + "}")
        elif kind == "code":
            _require(r"\end{verbatim}" not in block["text"], "Code block contains a TeX verbatim terminator")
            parts.append("\\begin{verbatim}\n" + block["text"] + "\n\\end{verbatim}")
        elif kind == "math":
            parts.append("\\[\n" + block["text"] + "\n\\]")
        elif kind == "table":
            columns = len(block["rows"][0])
            width = 0.92 / max(1, columns)
            parts.append(r"\begin{longtable}{" + "|" + "|".join("p{" + f"{width:.4f}" + r"\linewidth}" for _ in range(columns)) + "|}\n\\hline")
            for row in block["rows"]:
                parts.append(" & ".join(_tex_inline(cell) for cell in row) + " \\\\\n\\hline")
            parts.append(r"\end{longtable}")
        elif kind == "quote":
            parts.append("\\begin{quote}\n" + _tex_inline(block["text"]) + "\n\\end{quote}")
        else:
            parts.append(_tex_inline(block["text"]))
    if active_list: parts.append(r"\end{" + active_list + "}")
    body = "\n\n".join(parts)
    if fragment:
        validate_latex_fragment(body)
        return body
    unicode_preamble = ""
    font_text = markdown if preamble_text is None else preamble_text
    if any(ord(char) > 127 for char in font_text):
        if re.search(r"[\u2e80-\u9fff\uf900-\ufaff]", font_text):
            # Fontspec plus XeTeX's native line breaker handles editable CJK
            # without requiring an image fallback or an automatic package fetch.
            unicode_preamble = ("\\usepackage{iftex}\n\\ifPDFTeX\n"
                "\\PackageError{readeraware}{CJK output requires XeLaTeX}{Use a local XeLaTeX installation}\n\\else\n"
                "\\usepackage{fontspec}\n"
                "\\IfFontExistsTF{SimSun}{\\setmainfont{SimSun}\\setmonofont{SimSun}}"
                "{\\IfFontExistsTF{Noto Serif CJK SC}{\\setmainfont{Noto Serif CJK SC}\\setmonofont{Noto Serif CJK SC}}"
                "{\\PackageError{readeraware}{A local CJK font is required}{Install SimSun or Noto Serif CJK SC separately}}}\n"
                "\\XeTeXlinebreaklocale \"zh\"\n\\XeTeXlinebreakskip=0pt plus 1pt\n\\fi\n")
        else:
            unicode_preamble = ("\\usepackage{iftex}\n\\ifPDFTeX\\usepackage[utf8]{inputenc}\\else\n"
                "\\usepackage{fontspec}\n\\IfFontExistsTF{Arial}{\\setmainfont{Arial}}{\\setmainfont{Latin Modern Roman}}\n\\fi\n")
    result = ("\\documentclass[11pt]{article}\n\\usepackage{amsmath,amssymb,longtable,graphicx}\n"
              "\\usepackage[margin=1in]{geometry}\n" + unicode_preamble + "\\begin{document}\n" + body + "\n\\end{document}\n")
    validate_latex_document(result)
    return result


def _normal(text: str) -> str:
    text = " ".join(text.split())
    return re.sub(r"(?<=[\u2e80-\u9fff\uf900-\ufaff])\s+(?=[\u2e80-\u9fff\uf900-\ufaff])", "", text)


def _ordered_text_check(expected: list[str], actual: str, label: str) -> dict:
    actual = _normal(actual)
    offset = 0
    for fragment in expected:
        fragment = _normal(fragment)
        if not fragment: continue
        location = actual.find(fragment, offset)
        _require(location >= 0, label + " full-text validation failed near: " + fragment[:100])
        offset = location + len(fragment)
    return {"full_text_verified": True, "expected_fragments": len(expected),
            "extracted_characters": len(actual), "visual_review_required": True}


def _plain_inline(text: str) -> str:
    parts = []
    for kind, value in _inline(text):
        if kind == "image": continue  # Image occurrence and bytes are independently checked.
        _require(kind not in {"math", "display_math"}, "Math requires a formula-preserving converter")
        parts.append(value[0] + " (" + value[1] + ")" if kind == "link" else value)
    return "".join(parts)


def _markdown_fragments(markdown: str) -> list[str]:
    fragments = []
    for block in _blocks(markdown):
        texts = [cell for row in block["rows"] for cell in row] if block["kind"] == "table" else [block["text"]]
        if block["kind"] == "code":
            fragments.extend(texts); continue
        for text in texts:
            segment = []
            for kind, value in _inline(text):
                if kind == "image":
                    if segment: fragments.append("".join(segment)); segment = []
                else:
                    _require(kind not in {"math", "display_math"}, "Math requires a formula-preserving converter")
                    segment.append(value[0] + " (" + value[1] + ")" if kind == "link" else value)
            if segment: fragments.append("".join(segment))
    return fragments


def _image_path(target: str, directory: Path) -> Path:
    value = unquote(target.strip().strip("<>"))
    _require(bool(value) and not re.search(r"[\\{}|~$#]|(?:^|/)\.\.(?:/|$)|^[A-Za-z]:|^/|[a-z]+://", value, re.I),
             "Images must use safe relative local asset paths")
    resolved = (directory / value).resolve()
    if not resolved.suffix and not resolved.is_file():
        for suffix in (".png", ".jpg", ".jpeg", ".pdf", ".gif", ".tif", ".tiff", ".bmp", ".svg"):
            candidate = resolved.with_suffix(suffix)
            if candidate.is_file(): resolved = candidate; break
    _require(resolved.is_relative_to(directory.resolve()) and resolved.is_file(),
             "Image resource must be an existing safe local output asset: " + value)
    return resolved


def _markdown_structure(markdown: str, directory: Path) -> dict:
    images, tables = [], []
    for block in _blocks(markdown):
        if block["kind"] == "code": continue
        if block["kind"] == "table":
            tables.append({"rows": len(block["rows"]), "columns": len(block["rows"][0])})
            texts = [cell for row in block["rows"] for cell in row]
        else: texts = [block.get("text", "")]
        for text in texts:
            images.extend(str(_image_path(value[1], directory)) for kind, value in _inline(text) if kind == "image")
    return {"images": images, "tables": tables}


def _ast_structure(ast: dict, directory: Path) -> dict:
    images, tables = [], []
    def walk(value):
        if isinstance(value, dict):
            kind = value.get("t")
            if kind == "Image": images.append(str(_image_path(value["c"][-1][0], directory)))
            if kind == "Table":
                c = value["c"]
                if len(c) == 6:  # Current Pandoc table AST, including row/column spans.
                    rows = list(c[3][1])
                    for body in c[4]: rows.extend(body[2]); rows.extend(body[3])
                    rows.extend(c[5][1])
                    tables.append({"rows": len(rows), "columns": len(c[2])})
                else:
                    raise ValidationError("Unsupported Pandoc table schema; refusing a potentially lossy table conversion")
            for child in value.values(): walk(child)
        elif isinstance(value, list):
            for child in value: walk(child)
    walk(ast.get("blocks", []))
    return {"images": images, "tables": tables}


def _docx_structure_check(path: Path, expected: dict) -> dict:
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
          "a": "http://schemas.openxmlformats.org/drawingml/2006/main"}
    with zipfile.ZipFile(path) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
        media = {hashlib.sha256(archive.read(name)).hexdigest() for name in archive.namelist() if name.startswith("word/media/")}
    shapes = []
    for table in root.findall(".//w:tbl", ns):
        rows = table.findall("w:tr", ns)
        grid = table.find("w:tblGrid", ns)
        columns = len(grid.findall("w:gridCol", ns)) if grid is not None else max((len(row.findall("w:tc", ns)) for row in rows), default=0)
        shapes.append({"rows": len(rows), "columns": columns})
    _require(shapes == expected["tables"], "DOCX native table row/column structure changed during conversion")
    drawings = root.findall(".//a:blip", ns)
    _require(len(drawings) == len(expected["images"]), "DOCX embedded image occurrence count changed during conversion")
    for filename in expected["images"]:
        _require(hashlib.sha256(Path(filename).read_bytes()).hexdigest() in media,
                 "DOCX failed to embed an original image resource: " + Path(filename).name)
    return {"table_shapes_verified": True, "table_shapes": shapes, "embedded_images_verified": True,
            "image_occurrences": len(drawings), "image_bytes_preserved": True}


def _docx_reopen_check(path: Path, expected: dict) -> dict:
    if importlib.util.find_spec("docx") is None:
        return {"reopened_roundtrip_verified": False, "reopen_limitation": "python-docx is unavailable; OOXML structure was independently checked"}
    from docx import Document
    before_text, before_math = _docx_text_and_math(path)
    reopened = Document(str(path))
    buffer = BytesIO()
    reopened.save(buffer)
    buffer.seek(0)
    after_text, after_math = _docx_text_and_math(buffer)
    _require(before_text == after_text and before_math == after_math, "DOCX reopening changed text or editable equations")
    buffer.seek(0)
    _docx_structure_check(buffer, expected)
    return {"reopened_roundtrip_verified": True, "reopen_method": "python-docx in-memory roundtrip",
            "cjk_text_roundtrip_verified": bool(re.search(r"[\u2e80-\u9fff\uf900-\ufaff]", before_text)),
            "editable_math_roundtrip_count": after_math}


def _pdf_images_check(path: Path, expected: dict) -> dict:
    from pypdf import PdfReader
    if not expected["images"]:
        return {"embedded_images_verified": True, "source_image_occurrences": 0,
                "source_table_shapes": expected["tables"], "table_content_verified": True}
    from PIL import Image
    def pixels(image):
        rgb = image.convert("RGB")
        return (rgb.size, hashlib.sha256(rgb.tobytes()).hexdigest())
    required = set()
    for filename in expected["images"]:
        try:
            with Image.open(filename) as original: required.add(pixels(original))
        except Exception as exc:
            raise ValidationError("PDF image verification requires a raster source; convert vector graphics with a local renderer: " + Path(filename).name) from exc
    actual = set()
    for page in PdfReader(str(path)).pages:
        for image in page.images:
            actual.add(pixels(image.image))
    _require(required.issubset(actual), "PDF image verification found a missing or altered raster image")
    return {"embedded_images_verified": True, "source_image_occurrences": len(expected["images"]),
            "unique_source_images": len(required), "decoded_image_pixels_preserved": True,
            "source_table_shapes": expected["tables"], "table_content_verified": True,
            "table_geometry_verification": "Native table renderer and page visual review"}


def _docx_text_and_math(path: Path) -> tuple[str, int]:
    with zipfile.ZipFile(path) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    text = []
    for element in root.iter():
        if element.tag == "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t":
            text.append(element.text or "")
        elif element.tag.endswith("}p") or element.tag.endswith("}tc"):
            text.append(" ")
    math_count = sum(e.tag == "{http://schemas.openxmlformats.org/officeDocument/2006/math}oMath" for e in root.iter())
    return "".join(text), math_count


def _basic_docx(markdown: str, path: Path) -> dict:
    _guard_bounded_markdown_tables(markdown)
    try:
        from docx import Document
        from docx.shared import Inches, Pt, RGBColor
    except ImportError as exc:
        raise ValidationError("DOCX export requires python-docx") from exc
    document = Document()
    for section in document.sections:
        section.top_margin = section.bottom_margin = Inches(0.8)
        section.left_margin = section.right_margin = Inches(0.85)
    for name in ("Normal", "Title", "Heading 1", "Heading 2", "Heading 3", "Heading 4", "Heading 5", "Heading 6"):
        style = document.styles[name]
        style.font.name = "Calibri"
        style.font.color.rgb = RGBColor(0, 0, 0)
    document.styles["Normal"].font.size = Pt(11)
    document.styles["Normal"].paragraph_format.space_after = Pt(7)
    def add_runs(paragraph, text, max_width=6.3):
        for kind, value in _inline(text):
            _require(kind not in {"math", "display_math"}, "DOCX math requires a supported local converter")
            if kind == "image":
                image_path = _image_path(value[1], path.parent)
                from PIL import Image
                with Image.open(image_path) as image: width, height = image.size
                width_inches = min(max_width, width / 96, 7.5 * width / max(1, height))
                picture = paragraph.add_run().add_picture(str(image_path), width=Inches(width_inches))
                picture._inline.docPr.set("descr", value[0])
                continue
            run = paragraph.add_run(value[0] + " (" + value[1] + ")" if kind == "link" else value)
            if kind == "bold": run.bold = True
            if kind == "italic": run.italic = True
            if kind == "code": run.font.name = "Consolas"
    for block in _blocks(markdown):
        kind = block["kind"]
        if kind == "table":
            rows = block["rows"]
            table = document.add_table(rows=0, cols=len(rows[0]))
            table.style = "Table Grid"
            for number, row in enumerate(rows):
                cells = table.add_row().cells
                for cell, text in zip(cells, row):
                    add_runs(cell.paragraphs[0], text, max_width=6.0 / len(row))
                    if number == 0:
                        for run in cell.paragraphs[0].runs: run.bold = True
        elif kind == "code":
            paragraph = document.add_paragraph()
            paragraph.add_run(block["text"]).font.name = "Consolas"
        else:
            style = "Heading " + str(block["level"]) if kind == "heading" else "List Bullet" if kind == "bullet" else "List Number" if kind == "ordered" else None
            add_runs(document.add_paragraph(style=style), block["text"])
    document.save(str(path))
    text, math_count = _docx_text_and_math(path)
    structure = _markdown_structure(markdown, path.parent)
    return {"converter": "python-docx", "math_count": math_count,
            **_docx_structure_check(path, structure), **_docx_reopen_check(path, structure),
            **_ordered_text_check(_markdown_fragments(markdown), text, "DOCX")}


def _pdf_font(text: str):
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    candidates = [os.environ.get("READERAWARE_PDF_FONT"), "C:/Windows/Fonts/arial.ttf",
                  "C:/Windows/Fonts/simsun.ttc", "C:/Windows/Fonts/msyh.ttc",
                  "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                  "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"]
    required = {ord(char) for char in text if not char.isspace()}
    for number, candidate in enumerate(candidates):
        if not candidate or not Path(candidate).is_file(): continue
        try:
            font = TTFont("ReaderAware" + str(number), candidate, subfontIndex=0)
            if required.issubset(font.face.charToGlyph):
                pdfmetrics.registerFont(font)
                return font.fontName, str(Path(candidate).resolve())
        except Exception:
            continue
    raise ValidationError("No local embeddable TrueType font covers every PDF character; set READERAWARE_PDF_FONT to a suitable local font")


def _pdf_text_check(path: Path, fragments: list[str], *, allow_float_reordering=False) -> dict:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ValidationError("PDF full-text validation requires pypdf") from exc
    reader = PdfReader(str(path))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    if allow_float_reordering:
        # TeX may place figures/tables elsewhere on the page. Match every
        # expected word occurrence, preserving duplicates, without assuming
        # that visual float order equals the source AST's serialization order.
        token_pattern = r"[A-Za-z0-9_]+|[\u2e80-\u9fff\uf900-\ufaff]|\w"
        wanted = Counter(re.findall(token_pattern, " ".join(fragments), re.UNICODE))
        found = Counter(re.findall(token_pattern, text, re.UNICODE))
        missing = wanted - found
        _require(not missing, "PDF full-text validation found missing source words: " + ", ".join(list(missing)[:12]))
        return {"pages": len(reader.pages), "full_text_verified": True, "expected_fragments": len(fragments),
                "source_text_tokens_verified": sum(wanted.values()), "tex_float_reordering_allowed": True,
                "extracted_characters": len(text), "visual_review_required": True}
    return {"pages": len(reader.pages), **_ordered_text_check(fragments, text, "PDF")}


def _basic_pdf(markdown: str, path: Path) -> dict:
    _guard_bounded_markdown_tables(markdown)
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, LongTable, TableStyle, Image as PDFImage
    except ImportError as exc:
        raise ValidationError("Markdown PDF export requires ReportLab") from exc
    fragments = _markdown_fragments(markdown)
    font, font_path = _pdf_font("\n".join(fragments) + "1234567890-()")
    normal = ParagraphStyle("Body", fontName=font, fontSize=10.5, leading=15, spaceAfter=8, alignment=TA_LEFT,
                            wordWrap="CJK")
    story = []
    def content(text, style=normal, width=A4[0] - 108):
        flowables, segment = [], []
        for kind, value in _inline(text):
            if kind == "image":
                if segment:
                    flowables.append(Paragraph(html.escape("".join(segment)).replace("\n", "<br/>"), style)); segment = []
                image_path = _image_path(value[1], path.parent)
                from PIL import Image
                with Image.open(image_path) as image: image_width, image_height = image.size
                scale = min(width / image_width, 480 / image_height, 0.75)
                flowables.append(PDFImage(str(image_path), width=image_width * scale, height=image_height * scale))
                flowables.append(Spacer(1, 8))
            else:
                _require(kind not in {"math", "display_math"}, "PDF math requires a formula-preserving converter")
                segment.append(value[0] + " (" + value[1] + ")" if kind == "link" else value)
        if segment: flowables.append(Paragraph(html.escape("".join(segment)).replace("\n", "<br/>"), style))
        return flowables or [Paragraph(" ", style)]
    def para(text, style=normal):
        # Escape source before feeding ReportLab's XML parser. No images/URLs
        # are passed to its resource loader.
        return Paragraph(html.escape(_plain_inline(text)).replace("\n", "<br/>"), style)
    for block in _blocks(markdown):
        kind = block["kind"]
        if kind == "table":
            columns = len(block["rows"][0])
            widths = [max(len(_plain_inline(row[i])) for row in block["rows"]) + 4 for i in range(columns)]
            weights = [min(50, max(10, width)) for width in widths]
            col_widths = [(A4[0] - 108) * w / sum(weights) for w in weights]
            rows = [[content(cell, width=col_widths[i] - 14) for i, cell in enumerate(row)] for row in block["rows"]]
            table = LongTable(rows, colWidths=col_widths, repeatRows=1)
            table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), .5, colors.HexColor("#D9D9D9")),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EEEEEE")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7), ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
            story.extend([table, Spacer(1, 9)])
        elif kind == "heading":
            size = {1: 18, 2: 14, 3: 12}.get(block["level"], 11)
            style = ParagraphStyle("Heading" + str(block["level"]), parent=normal, fontSize=size,
                                   leading=size + 4, spaceBefore=12, spaceAfter=8, keepWithNext=True)
            story.extend(content(block["text"], style))
        elif kind == "code":
            for line in block["text"].splitlines() or [""]:
                story.append(Paragraph(html.escape(line) or " ", normal))
        elif kind in {"bullet", "ordered"}:
            story.extend(content("- " + block["text"]))
        else:
            story.extend(content(block["text"]))
    if not story: story.append(Paragraph(" ", normal))
    SimpleDocTemplate(str(path), pagesize=A4, rightMargin=54, leftMargin=54,
                      topMargin=54, bottomMargin=54, title="", author="").build(story)
    return {"converter": "reportlab", "embedded_font": font_path, **_pdf_text_check(path, fragments),
            **_pdf_images_check(path, _markdown_structure(markdown, path.parent))}


def _run(command: list[str], cwd: Path, *, input_text=None, env=None, timeout=120) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(command, cwd=str(cwd), input=input_text, text=True,
                                encoding="utf-8", errors="replace", stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=timeout, check=False, shell=False, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValidationError("Local converter failed or timed out: " + str(exc)) from exc
    _require(result.returncode == 0, "Local converter failed: " + (result.stderr or result.stdout)[-6000:])
    return result


def _pandoc_ast(pandoc: str, path: Path, content_format: str) -> dict:
    result = _run([pandoc, "--sandbox", "--fail-if-warnings", "--from=" + ("latex" if content_format == "latex" else "markdown+tex_math_dollars+tex_math_single_backslash"),
                   "--to=json", path.name], path.parent)
    ast = json.loads(result.stdout)
    def inspect(value):
        if isinstance(value, dict):
            if value.get("t") == "Cite":
                # Pandoc intentionally retains a raw LaTeX citation fallback
                # until citeproc resolves it. It is not arbitrary raw markup.
                for inline in value["c"][1]:
                    if inline.get("t") == "RawInline":
                        _require(bool(re.fullmatch(r"\\(?:[A-Za-z]*cite[A-Za-z]*|nocite)\*?(?:\[[^\]]*\])*\{[^{}]+\}", inline["c"][1])),
                                 "Unsupported raw markup inside a citation")
                    else: inspect(inline)
                return
            if value.get("t") in {"RawInline", "RawBlock"}:
                _require(not str(value.get("c", [None, ""])[1]).strip(),
                         "Pandoc found unsupported raw markup; refusing a potentially lossy conversion")
            if value.get("t") == "Image":
                target = value["c"][-1][0]
                resolved = _image_path(target, path.parent)
                value["c"][-1][0] = resolved.relative_to(path.parent).as_posix()
            for child in value.values(): inspect(child)
        elif isinstance(value, list):
            for child in value: inspect(child)
    inspect(ast)
    return ast


def _ast_fragments_and_math(ast: dict) -> tuple[list[str], int]:
    fragments = []
    math_count = 0
    def walk(value):
        nonlocal math_count
        if isinstance(value, dict):
            kind = value.get("t")
            if kind in {"Str", "Code", "CodeBlock"}:
                fragments.append(value["c"] if kind == "Str" else value["c"][-1])
                return
            if kind == "Math":
                math_count += 1
                return
            if kind == "Image": return
            if kind == "Cite": return  # Native citation rendering is checked by its bibliography toolchain.
            if kind == "Link":
                walk(value["c"][1]); return
            for child in value.values(): walk(child)
        elif isinstance(value, list):
            for child in value: walk(child)
    walk(ast.get("blocks", []))
    return fragments, math_count


def _pandoc_convert(source: Path, target: Path, content_format: str, pandoc: str) -> dict:
    ast = _pandoc_ast(pandoc, source, content_format)
    def has_citations(value):
        if isinstance(value, dict): return value.get("t") == "Cite" or any(has_citations(x) for x in value.values())
        if isinstance(value, list): return any(has_citations(x) for x in value)
        return False
    citations_resolved = has_citations(ast)
    citation_style = None
    if citations_resolved:
        bibliography_meta = ast.get("meta", {}).get("bibliography")
        _require(bibliography_meta is not None, "Citation conversion requires a supplied local bibliography")
        def meta_strings(value):
            if isinstance(value, dict):
                if value.get("t") in {"Str", "MetaString"}: return [value["c"]]
                result = []
                for child in value.values(): result.extend(meta_strings(child))
                return result
            if isinstance(value, list):
                result = []
                for child in value: result.extend(meta_strings(child))
                return result
            return []
        references = [_local_bibliography_path(value, source.parent, suffix=".bib") for value in meta_strings(bibliography_meta)]
        _require(bool(references), "Citation conversion requires a local bibliography file")
        command = [pandoc, "--sandbox", "--fail-if-warnings", "--from=json", "--to=json", "--citeproc"]
        command.extend("--bibliography=" + str(path.relative_to(source.parent)) for path in references)
        source_text = source.read_text(encoding="utf-8")
        if re.search(r"\\bibliographystyle\s*\{(?:plain|unsrt|abbrv|ieeetr|IEEEtran)\}", source_text) or re.search(r"style\s*=\s*(?:numeric|ieee|vancouver)", source_text):
            csl = target.parent / ".numeric-citations.csl"
            _exact_write(csl, NUMERIC_CSL)
            command.append("--csl=" + csl.name)
            citation_style = "numeric"
        ast = json.loads(_run(command, source.parent, input_text=json.dumps(ast)).stdout)
    fragments, math_count = _ast_fragments_and_math(ast)
    structure = _ast_structure(ast, source.parent)
    write_ast = deepcopy(ast)
    if target.suffix == ".docx":
        def embed_images(value):
            if isinstance(value, dict):
                if value.get("t") == "Image":
                    image_path = _image_path(value["c"][-1][0], source.parent)
                    mime = mimetypes.guess_type(image_path.name)[0]
                    _require(mime is not None and mime.startswith("image/"), "DOCX image conversion requires a supported raster or SVG image")
                    # A checked local file becomes an in-memory resource. The
                    # sandboxed writer cannot fetch a URL or read another file.
                    value["c"][-1][0] = "data:" + mime + ";base64," + base64.b64encode(image_path.read_bytes()).decode("ascii")
                for child in value.values(): embed_images(child)
            elif isinstance(value, list):
                for child in value: embed_images(child)
        embed_images(write_ast)
    # Work from the checked AST, so the writer cannot reread original TeX
    # includes or fetch remote references. No filters, templates or Lua execute.
    result = _run([pandoc, "--sandbox", "--fail-if-warnings", "--from=json",
                   "--to=" + ("gfm+tex_math_dollars" if target.suffix == ".md" else "docx"),
                   "--output=" + target.name], target.parent, input_text=json.dumps(write_ast))
    if target.suffix == ".docx":
        text, exported_math = _docx_text_and_math(target)
        _require(exported_math >= math_count, "DOCX math conversion lost equations")
        check = _ordered_text_check(fragments, text, "Pandoc DOCX")
        return {"converter": "pandoc", "math_count": math_count, "exported_math_count": exported_math,
                "citations_resolved": citations_resolved, "citation_style": citation_style,
                **check, **_docx_structure_check(target, structure), **_docx_reopen_check(target, structure)}
    exported = target.read_text(encoding="utf-8")
    _require(not math_count or _has_math(exported), "Markdown conversion lost mathematical source")
    return {"converter": "pandoc", "math_count": math_count, "source_text_verified": True,
            "source_image_occurrences": len(structure["images"]), "source_table_shapes": structure["tables"],
            "visual_review_required": False}


def _copy_assets(out_dir: Path, tex_sources, provenance) -> list[dict]:
    copied = []
    supplied_bbl = []
    if isinstance(tex_sources, dict) and provenance is None: provenance = tex_sources
    if not provenance:
        # A bare source path provides a project root but never authorizes a
        # recursive directory copy. Literal dependency manifests are preferred.
        return copied
    roots = {item.get("source_id"): Path(item["project_root"]).resolve()
             for item in provenance.get("sources", []) if item.get("project_root")}
    for item in provenance.get("files", []):
        if item.get("role") not in {"tex-graphic", "tex-bibliography", "tex-bibliography-output", "tex-bbl", "tex-style", "tex-include",
                                    "docx-image", "pdf-image", "pdf-page-crop", "markdown-image"}: continue
        root = roots.get(item.get("source_id"))
        source = Path(item["path"]).resolve()
        _require(source.is_file(), "TeX asset is missing")
        if item.get("output_relative_path") or item.get("relative_path"):
            raw_relative = str(item.get("output_relative_path") or item["relative_path"]).replace("\\", "/")
            parts = raw_relative.split("/")
            if not item.get("output_relative_path") and parts and parts[0] == item.get("source_id"): parts = parts[1:]
            _require(bool(parts) and all(part not in {"", ".", ".."} for part in parts)
                     and not re.match(r"(?:[A-Za-z]:|/)", raw_relative), "Unsafe TeX asset relative path")
            relative = Path(*parts)
            # Snapshotted files have a new absolute path while project_root
            # intentionally continues to identify the user's original project.
            # Their recorded digest, checked below, binds bytes to provenance.
            if not item.get("original_path") and root is not None and item.get("role", "").startswith("tex-"):
                _require(source.is_relative_to(root), "TeX asset escapes its declared project root")
        else:
            _require(root is not None and source.is_relative_to(root), "TeX asset escapes its declared project root")
            relative = source.relative_to(root)
        target = (out_dir / relative).resolve()
        _require(target.is_relative_to(out_dir.resolve()), "TeX asset output path escapes output directory")
        _require(relative.as_posix() not in {"main.tex", "future.tex", "document.tex", "main.md", "future.md", "document.md", "document.docx", "document.pdf", "export_checks.json"},
                 "TeX asset collides with a reserved output filename: " + relative.as_posix())
        data = source.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        _require(not item.get("sha256") or item["sha256"] == sha, "TeX asset changed after input provenance capture")
        if source.suffix.lower() in {".tex", ".sty", ".cls", ".bib", ".bst", ".bbl"}:
            # Bibliography is data; bst and styles are TeX programs and cannot
            # bypass safety checks through a dependency file.
            if source.suffix.lower() not in {".bib", ".bst"}:
                validate_latex_safety(data.decode("utf-8-sig"))
        if target.exists():
            _require(target.read_bytes() == data, "Conflicting TeX assets share the same relative path: " + relative.as_posix())
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        copied.append({"source_id": item.get("source_id"), "relative_path": relative.as_posix(),
                       "path": str(target), "sha256": sha})
        if source.suffix.lower() == ".bbl": supplied_bbl.append((item.get("source_id"), target, sha))
    # The generated job is named document.tex. A conventional source-stem.bbl
    # must therefore also be available as document.bbl; preserve the original
    # asset and alias only an unambiguous supplied bibliography.
    if len({sha for _, _, sha in supplied_bbl}) == 1:
        source_id, supplied, sha = supplied_bbl[0]
        alias = out_dir / "document.bbl"
        if supplied != alias:
            _require(not alias.exists() or alias.read_bytes() == supplied.read_bytes(), "Conflicting preprovided document.bbl")
            alias.write_bytes(supplied.read_bytes())
            copied.append({"source_id": source_id, "relative_path": "document.bbl", "path": str(alias),
                           "sha256": sha, "alias_of": str(supplied)})
    return copied


def _bibliography_tool(engine: str, name: str) -> str | None:
    directory = Path(engine).parent
    for candidate in (directory / (name + ".exe"), directory / name):
        if candidate.is_file(): return str(candidate)
    found = shutil.which(name)
    return found if found and Path(found).stem.lower() == name else None


def _local_bibliography_path(value: str, directory: Path, *, suffix="") -> Path:
    _require(bool(value) and not re.search(r"[\\|~$#*?]|(?:^|/)\.\.(?:/|$)|^[A-Za-z]:|^/|[a-z]+://", value, re.I),
             "Bibliography data sources must be literal local files")
    path = (directory / value).resolve()
    if suffix and not path.suffix: path = path.with_suffix(suffix)
    _require(path.is_relative_to(directory.resolve()) and path.is_file(),
             "Missing local bibliography data file: " + value)
    return path


def _run_bibliography(path: Path, engine: str, environment: dict, miktex: bool, first_pass_started: int) -> tuple[dict, str]:
    bcf = path.with_suffix(".bcf")
    aux = path.with_suffix(".aux")
    bbl = path.with_suffix(".bbl")
    active_bcf = bcf.is_file() and bcf.stat().st_mtime_ns >= first_pass_started
    aux_text = aux.read_text(encoding="utf-8", errors="replace") if aux.is_file() else ""
    bibdata = re.findall(r"\\bibdata\{([^{}]+)\}", aux_text)
    if not active_bcf and not bibdata:
        return {"required": False, "tool": None}, ""
    name = "biber" if active_bcf else "bibtex"
    tool = _bibliography_tool(engine, name)
    if not tool:
        _require(bbl.is_file() and bbl.stat().st_size > 0,
                 "This TeX document requires local " + name + " or a preprovided document.bbl; no bibliography is silently dropped")
        validate_latex_safety(bbl.read_text(encoding="utf-8-sig"))
        return {"required": True, "tool": "preprovided-bbl", "requested_tool": name, "bbl": str(bbl)}, ""
    if name == "biber":
        data = bcf.read_bytes()
        _require(b"<!DOCTYPE" not in data and b"<!ENTITY" not in data,
                 "Biber control files must not contain external entities")
        control = ET.fromstring(data)
        for element in control.iter():
            if element.tag.rsplit("}", 1)[-1] == "datasource":
                _require(element.get("type", "file") == "file" and element.get("datatype", "bibtex") == "bibtex",
                         "Only local BibTeX data sources are supported by Biber")
                _local_bibliography_path((element.text or "").strip(), path.parent, suffix=".bib")
        command = [tool, "--noconf", path.stem]
    else:
        for group in bibdata:
            for value in group.split(","):
                _local_bibliography_path(value.strip(), path.parent, suffix=".bib")
        command = [tool] + (["--disable-installer"] if miktex else []) + [path.stem]
    result = _run(command, path.parent, env=environment)
    _require(bbl.is_file() and bbl.stat().st_size > 0, "Local bibliography tool produced no .bbl")
    # Bibliography fields can contain TeX. Inspect generated markup before the
    # engine reads it on the next pass, using the same conservative guard.
    validate_latex_safety(bbl.read_text(encoding="utf-8-sig"))
    return {"required": True, "tool": name, "executable": tool, "bbl": str(bbl),
            "automatic_package_installation": False, "remote_data_sources": False}, result.stdout + result.stderr


def compile_latex(path: Path, compiler=None) -> dict:
    """Compile only with an installed native engine; never install packages."""
    engine = _find_compiler(compiler)
    _require(engine is not None, "LaTeX PDF export requires a local xelatex or pdflatex installation")
    validate_latex_document(path.read_bytes().decode("utf-8"))
    environment = os.environ.copy()
    environment.update({"openin_any": "p", "openout_any": "p", "shell_escape": "f",
                        "MIKTEX_ENABLE_INSTALLER": "0", "MIKTEX_AUTOINSTALL": "0"})
    version = _run([engine, "--version"], path.parent, env=environment, timeout=20).stdout
    command = [engine, "-no-shell-escape", "-interaction=nonstopmode", "-halt-on-error", "-file-line-error"]
    miktex = "miktex" in version.lower()
    if miktex: command.append("--disable-installer")
    command.append(path.name)
    logs = []
    try:
        first_pass_started = time.time_ns()
        run = _run(command, path.parent, env=environment)
        logs.append(run.stdout + run.stderr)
        bibliography, bibliography_log = _run_bibliography(path, engine, environment, miktex, first_pass_started)
        if bibliography_log: logs.append(bibliography_log)
        remaining_passes = 2 if bibliography["required"] else 1
        for _ in range(remaining_passes):
            run = _run(command, path.parent, env=environment)
            logs.append(run.stdout + run.stderr)
    except ValidationError as exc:
        _exact_write(path.with_suffix(".compile.log"), "\n".join(logs) + "\n" + str(exc))
        raise ValidationError("Local LaTeX compilation failed; missing packages/fonts/tools must be installed separately. " + str(exc)) from exc
    _exact_write(path.with_suffix(".compile.log"), "\n".join(logs))
    pdf = path.with_suffix(".pdf")
    _require(pdf.is_file() and pdf.stat().st_size > 0, "Native TeX compiler produced no PDF")
    combined_logs = "\n".join(logs)
    _require(not re.search(r"Missing character:|There is no .+ in font", combined_logs),
             "TeX compilation reported missing glyphs; choose a suitable font/engine")
    final_log = run.stdout + run.stderr
    _require(not re.search(r"(?:Citation|Reference) .+ undefined|There were undefined (?:references|citations)|Please \(re\)run Biber", final_log),
             "TeX output has unresolved references/citations; supply bibliography and dependencies")
    return {"compiled": True, "engine": engine, "passes": 1 + remaining_passes,
            "bibliography": bibliography, "shell_escape": False,
            "automatic_package_installation": False, "log": str(path.with_suffix(".compile.log")),
            "pdf": str(pdf), "visual_review_required": True}


def export_outputs(out_dir, main_text, future_text, *, content_format="markdown", output_formats=None,
                   tex_sources=None, provenance=None, compiler=None, validate_latex=True) -> dict:
    """Write frozen responses and requested combined formats, returning paths/checks.

    ``validate_latex`` controls optional native compilation, never structural or
    safety validation. PDF always requires successful local compilation for TeX.
    ``compiler='required'`` requires a native engine; ``'off'`` disables optional
    compilation. A provenance dependency manifest authorizes only named assets.
    """
    _require(isinstance(main_text, str) and bool(main_text.strip()), "Empty main document")
    _require(isinstance(future_text, str), "Future document must be a string")
    if content_format == "markdown" and any(fmt != "markdown" for fmt in (output_formats or [content_format])):
        _guard_bounded_markdown_tables(main_text + ("\n\n" + future_text if future_text else ""))
    preflight = preflight_outputs(output_formats, content_format, compiler)
    formats = preflight["output_formats"]
    dependencies = preflight["dependencies"]
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    main_bytes = main_text.encode("utf-8")
    checks: dict[str, Any] = {"frozen_main_sha256_utf8": hashlib.sha256(main_bytes).hexdigest(),
                             "frozen_body_preserved": True, "formats": {}}
    warnings = []
    source_extension = ".tex" if content_format == "latex" else ".md"
    main_path, future_path = out / ("main" + source_extension), out / ("future" + source_extension)
    _exact_write(main_path, main_text)
    _exact_write(future_path, future_text)
    assets = _copy_assets(out, tex_sources, provenance)
    artifacts: dict[str, dict[str, str]] = {}
    if content_format == "latex":
        combined = assemble_latex(main_text, future_text)
        source_combined = out / "document.tex"
        _exact_write(source_combined, combined)
        info = validate_latex_document(main_text)
        insertion_length = len(combined) - len(main_text)
        reconstructed = combined[:info["end_document_start"]] + combined[info["end_document_start"] + insertion_length:]
        _require(reconstructed.encode("utf-8") == main_bytes, "LaTeX assembly changed frozen body bytes")
        checks["final_prefix_verified"] = combined.startswith(main_text) if future_text else True
        checks["latex"] = {**validate_latex_document(combined), "frozen_body_reconstruction_verified": True,
                           "future_inserted_inside_document": bool(future_text), "compiled": False}
        if "latex" in formats:
            artifacts["latex"] = {"main": str(main_path), "future": str(future_path), "combined": str(source_combined)}
    else:
        combined = main_text + ("\n\n" + future_text if future_text else "")
        source_combined = out / "document.md"
        _exact_write(source_combined, combined)
        checks["final_prefix_verified"] = source_combined.read_bytes()[:len(main_bytes)] == main_bytes
        if "markdown" in formats:
            artifacts["markdown"] = {"main": str(main_path), "future": str(future_path), "combined": str(source_combined)}
    if "latex" in formats and content_format == "markdown":
        tex_main = markdown_to_latex(main_text, preamble_text=combined)
        tex_future = markdown_to_latex(future_text, fragment=True) if future_text else ""
        tex_document = assemble_latex(tex_main, tex_future)
        _exact_write(out / "main.tex", tex_main)
        _exact_write(out / "future.tex", tex_future)
        _exact_write(out / "document.tex", tex_document)
        artifacts["latex"] = {"main": str(out / "main.tex"), "future": str(out / "future.tex"), "combined": str(out / "document.tex")}
        checks["latex"] = {**validate_latex_document(tex_document), "compiled": False,
                           "conversion": "deterministic supported Markdown subset"}
    if "markdown" in formats and content_format == "latex":
        target = out / "document.md"
        checks["formats"]["markdown"] = _pandoc_convert(source_combined, target, "latex", dependencies["pandoc"])
        artifacts["markdown"] = {"combined": str(target)}
    if "docx" in formats:
        target = out / "document.docx"
        if content_format == "latex" or _has_math(combined) or _has_structured_citations(combined):
            dependencies["pandoc"] = dependencies["pandoc"] or _find_pandoc()
            _require(dependencies["pandoc"] is not None, "Mathematical DOCX or structured citations require local Pandoc (or pypandoc_binary); formulas/references are never flattened to plain text")
            checks["formats"]["docx"] = _pandoc_convert(source_combined, target, content_format, dependencies["pandoc"])
        elif dependencies["python_docx"]:
            checks["formats"]["docx"] = _basic_docx(combined, target)
        else:
            checks["formats"]["docx"] = _pandoc_convert(source_combined, target, content_format, dependencies["pandoc"])
        artifacts["docx"] = {"combined": str(target)}
    needs_tex_pdf = "pdf" in formats and (content_format == "latex" or _has_math(combined) or not dependencies["reportlab"])
    if needs_tex_pdf and not dependencies["latex_compiler"]:
        native_preflight = preflight_outputs(["pdf"], "latex", compiler)
        dependencies.update(native_preflight["dependencies"])
    tex_path = out / "document.tex"
    if needs_tex_pdf and not tex_path.exists():
        _exact_write(tex_path, markdown_to_latex(combined))
        checks["latex"] = {**validate_latex_document(tex_path.read_text(encoding="utf-8")), "compiled": False}
    if tex_path.exists() and (needs_tex_pdf or (validate_latex and dependencies["latex_compiler"] and compiler not in (False, "off"))):
        checks["latex"].update(compile_latex(tex_path, compiler))
    elif tex_path.exists():
        warnings.append("LaTeX structure checked; native compilation was not performed")
    if "pdf" in formats:
        target = out / "document.pdf"
        if needs_tex_pdf:
            _require(checks.get("latex", {}).get("compiled"), "PDF requires a successful native TeX compilation")
            if content_format == "markdown" and not _has_math(combined):
                checks["formats"]["pdf"] = _pdf_text_check(target, _markdown_fragments(combined))
            else:
                # Native TeX preserves original mathematical source. Full prose
                # verification is available when a strict Pandoc reader exists.
                if dependencies["pandoc"]:
                    ast = _pandoc_ast(dependencies["pandoc"], tex_path, "latex")
                    fragments, count = _ast_fragments_and_math(ast)
                    checks["formats"]["pdf"] = {"source_math_count": count, **_pdf_text_check(target, fragments, allow_float_reordering=True)}
                    checks["formats"]["pdf"].update(_pdf_images_check(target, _ast_structure(ast, tex_path.parent)))
                else:
                    from pypdf import PdfReader
                    reader = PdfReader(str(target))
                    _require(any((page.extract_text() or "").strip() for page in reader.pages), "Compiled PDF has no extractable text")
                    checks["formats"]["pdf"] = {"pages": len(reader.pages), "extractable_text": True,
                        "full_text_verified": False, "visual_review_required": True,
                        "limitation": "Install local Pandoc for independent TeX prose extraction checks"}
            checks["formats"]["pdf"]["converter"] = "native-tex"
        else:
            checks["formats"]["pdf"] = _basic_pdf(combined, target)
        artifacts["pdf"] = {"combined": str(target)}
    _require(main_path.read_bytes() == main_bytes, "Frozen main source changed during export")
    result = {"artifacts": artifacts, "source_artifacts": {"main": str(main_path), "future": str(future_path)},
              "checks": checks, "warnings": warnings, "assets": assets, "preflight": preflight}
    json_write(out / "export_checks.json", result)
    return result
