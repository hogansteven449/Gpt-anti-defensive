#!/usr/bin/env python3
"""Run a fresh five-stage reader-aware writing process with native Codex."""
import argparse
import json
import sys
from pathlib import Path

from readeraware.codex import inspect_cli
from readeraware.pipeline import run_pipeline
from readeraware.util import redact
from readeraware.outputs import export_outputs, preflight_outputs
from readeraware.preparation import prepare_inputs, inspect_delivery


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    commands = p.add_subparsers(dest='command', required=True)
    doctor = commands.add_parser('doctor', help='Inspect native CLI isolation flags without model calls')
    doctor.add_argument('--codex-bin')
    doctor.add_argument('--auth', action='store_true', help='Also inspect login status, never stored credentials')
    doctor.add_argument('--formats', action='store_true', help='Also inspect optional document-format capabilities')
    run = commands.add_parser('run', help='Create a new output directory; no automatic retries')
    run.add_argument('--source', action='append', required=True, help='Repeatable main-agent-prepared UTF-8 .txt/.md file')
    run.add_argument('--task-file', required=True, help='UTF-8 writing request; kept unchanged')
    run.add_argument('--out', required=True, help='New directory, must not already exist')
    run.add_argument('--model', help='Exact model ID; defaults only to the local configured model')
    run.add_argument('--effort', choices=['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'])
    run.add_argument('--codex-bin', help='Native executable, not a .cmd/.bat wrapper')
    run.add_argument('--backend', choices=['codex', 'fixture'], default='codex')
    run.add_argument('--future-work', choices=['auto', 'on', 'off'], default='auto')
    run.add_argument('--word-budget', type=int, help='Total body+future word target, separate from unchanged task')
    run.add_argument('--future-words', type=int, help='Future reservation; for 500 total defaults to 80')
    run.add_argument('--threshold', type=float, help='Explicit T override in [0.33, 0.67]')
    run.add_argument('--timeout', type=float, default=2400, help='Seconds per stage; failure stops with logs')
    prepare = commands.add_parser('prepare', help='Main-agent preliminary extraction; requires subsequent fidelity verification')
    prepare.add_argument('--source', action='append', required=True)
    prepare.add_argument('--out', required=True, help='New preparation directory')
    prepare.add_argument('--project-root')
    prepare.add_argument('--main-tex')
    prepare.add_argument('--docx-engine', choices=['python-docx','pandoc'], default='python-docx')
    prepare.add_argument('--pandoc-bin')
    export = commands.add_parser('export', help='Convert saved writer text without any model calls')
    export.add_argument('--main-file', required=True)
    export.add_argument('--future-file')
    export.add_argument('--content-format', choices=['markdown','latex'], default='markdown')
    export.add_argument('--output-format', action='append', required=True, choices=['markdown','latex','docx','pdf'])
    export.add_argument('--out', required=True, help='New export directory')
    export.add_argument('--provenance-file', help='Saved inputs/provenance.json for local TeX assets')
    export.add_argument('--latex-compiler')
    export.add_argument('--latex-check', choices=['auto','required','off'], default='auto')
    verify = commands.add_parser('verify', help='Reopen saved deliverables and inspect encoding/containers without model calls')
    verify.add_argument('--file', action='append', required=True)
    verify.add_argument('--report', required=True, help='New JSON readback report; visual/application checks remain with main agent')
    return p


def main(argv=None):
    # Avoid legacy Windows console/redirection codepages corrupting Chinese JSON.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'): stream.reconfigure(encoding='utf-8', errors='strict')
    args = parser().parse_args(argv)
    try:
        if args.command == 'doctor':
            result = inspect_cli(args.codex_bin, auth=args.auth)
            if args.formats:
                import importlib.util
                result['format_dependencies'] = {name: importlib.util.find_spec(name) is not None for name in ('pypdf','docx','reportlab','pypandoc')}
                result['basic_text_formats'] = preflight_outputs(['markdown'], 'markdown', 'off')
        elif args.command == 'prepare':
            result = prepare_inputs(args.source, args.out, project_root=args.project_root,
                main_tex=args.main_tex, docx_engine=args.docx_engine, pandoc_bin=args.pandoc_bin)
        elif args.command == 'verify':
            report = Path(args.report).resolve()
            if report.exists(): raise ValueError('Verification report already exists; use a new file')
            result = inspect_delivery(args.file)
            from readeraware.util import json_write
            json_write(report, result)
        elif args.command == 'export':
            target = Path(args.out).resolve()
            if target.exists(): raise ValueError('Export output directory already exists; use a new directory')
            main_text = Path(args.main_file).read_bytes().decode('utf-8-sig')
            future_text = Path(args.future_file).read_bytes().decode('utf-8-sig') if args.future_file else ''
            from readeraware.util import check_no_credentials
            check_no_credentials(main_text, 'saved main document')
            check_no_credentials(future_text, 'saved future section')
            provenance = json.loads(Path(args.provenance_file).read_text(encoding='utf-8-sig')) if args.provenance_file else None
            compiler = 'off' if args.latex_check == 'off' else (args.latex_compiler or ('required' if args.latex_check == 'required' else None))
            result = export_outputs(target, main_text, future_text, content_format=args.content_format,
                output_formats=args.output_format, provenance=provenance, compiler=compiler,
                validate_latex=args.latex_check != 'off')
            from readeraware.util import json_write
            json_write(target / 'export_report.json', result)
        else:
            if args.timeout <= 0:
                raise ValueError('--timeout must be positive')
            result = run_pipeline(args)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'type': type(exc).__name__,
                          'message': redact(str(exc)), 'automatic_retry': False}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
