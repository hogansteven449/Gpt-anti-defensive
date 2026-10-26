from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import time
from pathlib import Path

from .codex import CodexBackend, inspect_cli, local_selection
from .util import (ValidationError, check_no_credentials, digest, json_write,
                   read_json, redact, text_write, utc_now, validate_schema)

REFERENCES = Path(__file__).resolve().parents[2] / 'references'
STAGES = ('review', 'probabilities', 'selection', 'body', 'future')
PLACEHOLDER = re.compile(r'\{\{([A-Z_]+)\}\}')


def render(template: str, values: dict) -> str:
    """One pass: inserted source/task strings are never parsed as templates."""
    def replace(match):
        if match[1] not in values:
            raise ValidationError('Missing template input: ' + match[1])
        return str(values[match[1]])
    return PLACEHOLDER.sub(replace, template)


def json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)


def require(condition, message):
    if not condition:
        raise ValidationError(message)


def validate_evidence(refs, sources):
    texts = {s['source_id']: s['text'] for s in sources}
    for ref in refs:
        require(ref['source_id'] in texts, 'Unknown evidence source_id')
        require(bool(ref['quote']) and ref['quote'] in texts[ref['source_id']],
                'Evidence quote must be an exact nonempty source substring')


def validate_review(result, sources):
    ids = [p['id'] for p in result['points']]
    require(len(ids) == len(set(ids)), 'Review IDs must be unique')
    for p in result['points']:
        validate_evidence(p['source_basis'], sources)


def project_review(review):
    return [{k: p[k] for k in ('id', 'question', 'source_basis', 'answer_basis')}
            for p in review['points']]


def validate_probabilities(result, review):
    require([p['id'] for p in result['estimates']] == [p['id'] for p in review['points']],
            'B1 must score every review point once in original order')


def validate_selection(result, review, estimates, sources, threshold_override=None):
    require(set(result) == {'audit', 'body_points', 'future_points'}, 'B2 top-level shape changed')
    require(result['audit']['records'] == estimates['estimates'],
            'Frozen B1 records changed, reordered, or omitted')
    threshold = result['audit']['threshold']
    require(type(threshold) in (int, float) and math.isfinite(threshold)
            and .33 <= threshold <= .67, 'Invalid selection threshold')
    if threshold_override is not None:
        require(threshold == threshold_override, 'B2 changed explicit threshold override')
    points = {p['id']: p for p in review['points']}
    for name, probability in [('body_points', 'p1'), ('future_points', 'p2')]:
        expected = sorted((e for e in estimates['estimates'] if e[probability] >= threshold),
                          key=lambda e: -e[probability])
        require([p['id'] for p in result[name]] == [e['id'] for e in expected],
                name + ' must contain exactly qualifying IDs in stable descending order')
        for item, frozen in zip(result[name], expected):
            require(item['probability'] == frozen[probability], 'Selected probability changed')
            require(item['source_refs'] == points[item['id']]['source_basis'],
                    'Selected source_refs changed')
            validate_evidence(item['source_refs'], sources)


def validate_plain_document(text, allow_empty=False):
    require(isinstance(text, str) and (allow_empty or bool(text.strip())), 'Empty document')
    require(not re.search(r'\\(?:documentclass|begin\{document\})|^%PDF-', text),
            'Only plaintext/Markdown output is supported; no PDF or standalone LaTeX export')


def future_is_enabled(mode, task):
    if mode == 'off':
        return False, 'disabled by --future-work off'
    if mode == 'on':
        return True, 'enabled by --future-work on'
    # Only unambiguous literal opt-outs are recognized; use the flag for others.
    patterns = (r'\bno\s+future[- ]work\b',
                r'\b(?:do not|don\x27t|never)\s+(?:include|add|write|provide)\s+(?:a\s+|any\s+)?future[- ]work\b',
                r'(?:不要|无需|不需要|禁止)(?:添加|包括|撰写|写)?(?:任何)?(?:未来工作|后续研究)')
    if any(re.search(p, task, re.I) for p in patterns):
        return False, 'explicit future-work opt-out recognized in task'
    return True, 'auto: append approved future points unless disabled'


def length_budgets(total, future_words, enabled):
    if total is not None:
        require(total > 0, '--word-budget must be positive')
    if future_words is not None:
        require(future_words > 0, '--future-words must be positive')
    reserve = future_words
    if enabled and total is not None and reserve is None:
        reserve = min(80, max(1, round(total * .16)))
    if enabled and total is not None:
        require(reserve < total, 'Future budget must be smaller than total budget')
    return ((total - reserve if enabled and total is not None else total),
            reserve if enabled else 0)


def load_inputs(paths, task_file):
    sources = []
    for i, raw in enumerate(paths, 1):
        path = Path(raw)
        require(path.suffix.lower() in ('.txt', '.md'),
                'Child-agent inputs must be prepared UTF-8 .txt or .md; the main agent prepares PDF, TeX, or Word first')
        text = path.read_bytes().decode('utf-8-sig')
        require(bool(text.strip()), 'Source is empty')
        check_no_credentials(text, 'source input')
        sources.append({'source_id': f'S{i:03}', 'name': path.name, 'text': text})
    task = Path(task_file).read_bytes().decode('utf-8-sig')
    require(bool(task.strip()), 'Writing task is empty')
    check_no_credentials(task, 'writing task')
    return sources, task


def run_pipeline(args):
    out = Path(args.out).resolve()
    require(not out.exists(), 'Output directory already exists; use a new directory')
    sources, task = load_inputs(args.source, args.task_file)
    if args.threshold is not None:
        require(math.isfinite(args.threshold) and .33 <= args.threshold <= .67,
                '--threshold must be finite and within [0.33, 0.67]')
    enabled, future_reason = future_is_enabled(args.future_work, task)
    length_budgets(args.word_budget, args.future_words, enabled)
    assets = {}
    for name in STAGES:
        assets[name] = {'prompt': (REFERENCES / 'prompts' / (name + '.txt')).read_text(encoding='utf-8'),
                        'schema': read_json(REFERENCES / 'schemas' / (name + '.json'))}
    if args.backend == 'codex':
        local = local_selection()
        model = args.model or local['model']
        require(bool(model), 'Pass --model or configure a local Codex model; no model is guessed')
        effort = args.effort or local['reasoning_effort']
        backend = CodexBackend(model=model, reasoning_effort=effort,
                               timeout=args.timeout, executable=args.codex_bin)
    else:
        from .fixture import FixtureBackend
        backend = FixtureBackend()
        model, effort = None, None
    out.mkdir(parents=True, exist_ok=False)
    manifest = {'created_utc': utc_now(), 'status': 'running', 'backend': backend.name,
                'synthetic': backend.name == 'fixture', 'model_requested': model,
                'reasoning_effort': effort, 'backend_inspection': backend.info,
                'source_mode': 'Main-agent-prepared UTF-8 text/Markdown; document tools stay with the main agent',
                'output_format': 'Markdown only; main agent handles document export and delivery validation',
                'languages': 'Chinese and English; user-requested output language, otherwise source language',
                'task_unchanged': True, 'automatic_retries': 0, 'quality_resampling': False,
                'future_work_mode': args.future_work, 'future_work_reason': future_reason,
                'word_budget': args.word_budget, 'future_words': args.future_words,
                'length_enforcement': 'Prompt guidance and measured counts, not semantic enforcement',
                'threshold_override': args.threshold,
                'probability_interpretation': 'Subjective model estimates; not calibrated human frequencies'}
    json_write(out / 'manifest.json', manifest)
    json_write(out / 'inputs/sources.json', sources)
    json_write(out / 'inputs/prepared_manifest.json', [
        {'source_id': source['source_id'], 'name': source['name'],
         'loaded_utf8_sha256': hashlib.sha256(source['text'].encode('utf-8')).hexdigest(),
         'role': 'main-agent-prepared evidence'} for source in sources])
    text_write(out / 'inputs/task.txt', task)
    for name in STAGES:
        text_write(out / 'snapshots/prompts' / (name + '.txt'), assets[name]['prompt'])
        json_write(out / 'snapshots/schemas' / (name + '.json'), assets[name]['schema'])
    scripts = Path(__file__).resolve().parents[1]
    json_write(out / 'implementation_hashes.json', {
        str(p.relative_to(scripts)): digest(p) for p in sorted(scripts.rglob('*.py'))})
    receipts = []
    selection = None

    def stage(name, context, values, validate):
        # Opaque directory names avoid exposing pipeline labels in native cwd.
        directory = out / 'stages' / ('call-' + hashlib.sha256(name.encode()).hexdigest()[:16])
        directory.mkdir(parents=True, exist_ok=False)
        schema = assets[name]['schema']
        prompt = render(assets[name]['prompt'], values)
        check_no_credentials(prompt, 'rendered prompt')
        text_write(directory / 'request.txt', prompt)
        json_write(directory / 'input.json', context)
        json_write(directory / 'schema.json', schema)
        packet = {n: digest(directory / n) for n in ('request.txt', 'input.json', 'schema.json')}
        json_write(directory / 'packet_hashes.json', packet)
        json_write(directory / 'call_started.json', {'stage': name, 'started_utc': utc_now(),
                   'backend': backend.name, 'real_model_call': backend.name == 'codex'})
        try:
            result, metadata = backend.run(name, prompt, schema, directory, context)
            # Save the exact accepted JSON object before validating; invalid outputs
            # remain inspectable and never trigger a repair model call.
            check_no_credentials(json_text(result), 'model result')
            json_write(directory / 'response.json', result)
            validate_schema(result, schema)
            validate(result)
            require(all(digest(directory / n) == h for n, h in packet.items()), 'Stage packet mutated')
            receipt = {'status': 'completed', 'stage': name, **metadata,
                       'request_sha256': packet['request.txt'],
                       'input_sha256': packet['input.json'],
                       'response_sha256': digest(directory / 'response.json')}
            json_write(directory / 'receipt.json', receipt)
            json_write(directory / 'validation.json', {'valid': True,
                       'schema_and_mechanical_contracts': True,
                       'semantic_quality_guaranteed': False})
            receipts.append(receipt | {'directory': str(directory.relative_to(out))})
            return result, directory
        except BaseException as exc:
            json_write(directory / 'error.json', {'type': type(exc).__name__,
                       'message': redact(str(exc)), 'automatic_retry': False})
            raise

    source_value = json_text(sources)
    try:
        review, _ = stage('review', {'source': sources, 'user_prompt': task},
            {'SOURCE': source_value, 'USER_PROMPT': task}, lambda r: validate_review(r, sources))
        projected = project_review(review)
        estimates, estimates_dir = stage('probabilities', {'source': sources, 'review_points': projected},
            {'SOURCE': source_value, 'REVIEW_POINTS': json_text(projected)},
            lambda r: validate_probabilities(r, review))
        frozen_bytes = (estimates_dir / 'response.json').read_bytes()
        frozen_sha = digest(estimates_dir / 'response.json')
        json_write(out / 'frozen_estimates.json', estimates)
        selection, _ = stage('selection', {'source': sources, 'review_points': review['points'],
            'frozen_estimates': estimates, 'user_prompt': task, 'threshold_override': args.threshold},
            {'SOURCE': source_value, 'REVIEW_POINTS': json_text(review['points']),
             'FROZEN_ESTIMATES': json_text(estimates), 'USER_PROMPT': task,
             'THRESHOLD_OVERRIDE': 'not supplied' if args.threshold is None else args.threshold},
            lambda r: validate_selection(r, review, estimates, sources, args.threshold))
        require((estimates_dir / 'response.json').read_bytes() == frozen_bytes,
                'Original B1 response changed after selection')
        require(digest(out / 'frozen_estimates.json') == frozen_sha, 'Frozen B1 copy changed')
        json_write(out / 'selection_audit.json', selection['audit'])
        future_needed = enabled and bool(selection['future_points'])
        body_budget, future_budget = length_budgets(args.word_budget, args.future_words, future_needed)
        body_context = {'source': sources, 'user_prompt': task, 'body_points': selection['body_points'],
                        'body_length_budget': body_budget}
        body, body_dir = stage('body', body_context,
            {'SOURCE': source_value, 'USER_PROMPT': task, 'BODY_POINTS': json_text(selection['body_points']),
             'BODY_LENGTH_BUDGET': body_budget if body_budget is not None else 'not supplied'},
            lambda r: validate_plain_document(r['document']))
        main = body['document']
        main_bytes = main.encode('utf-8')
        prefix_sha = hashlib.sha256(main_bytes).hexdigest()
        text_write(out / 'outputs/main.md', main)
        json_write(out / 'frozen_body.json', {'sha256_utf8': prefix_sha,
                   'response_sha256': digest(body_dir / 'response.json'),
                   'append_policy': 'Exact frozen body followed by two newlines and appendix; no rewrite'})
        suffix = ''
        if future_needed:
            future_context = {'source': sources, 'user_prompt': task,
                'body_points': selection['body_points'], 'frozen_body': main,
                'future_points': selection['future_points'], 'future_length_budget': future_budget}
            appendix, _ = stage('future', future_context,
                {'SOURCE': source_value, 'USER_PROMPT': task, 'BODY_POINTS': json_text(selection['body_points']),
                 'FROZEN_BODY': main, 'FUTURE_POINTS': json_text(selection['future_points']),
                 'FUTURE_LENGTH_BUDGET': future_budget if future_budget is not None else 'not supplied'},
                lambda r: validate_plain_document(r['appendix'], allow_empty=True))
            suffix = appendix['appendix']
        else:
            json_write(out / 'future_skipped.json', {'skipped': True, 'model_call_issued': False,
                'reason': future_reason if not enabled else 'Approved future list is empty'})
        combined = main + ('\n\n' + suffix if suffix else '')
        text_write(out / 'outputs/future.md', suffix)
        text_write(out / 'outputs/document.md', combined)
        require((out / 'outputs/main.md').read_bytes() == main_bytes, 'Frozen main body changed')
        require((out / 'outputs/document.md').read_bytes()[:len(main_bytes)] == main_bytes,
                'Final document prefix differs from frozen main body')
        usage = {}
        for receipt in receipts:
            for name, value in (receipt.get('usage') or {}).items():
                if type(value) in (int, float): usage[name] = usage.get(name, 0) + value
        summary = {'status': 'completed', 'synthetic': backend.name == 'fixture',
            'model_requested': model, 'actual_backend_calls': len(receipts),
            'actual_real_model_calls': len(receipts) if backend.name == 'codex' else 0,
            'stages': receipts, 'usage': usage or None,
            'elapsed_seconds': sum(r.get('elapsed_seconds', 0) for r in receipts),
            'review_points': len(review['points']), 'threshold': selection['audit']['threshold'],
            'body_points': len(selection['body_points']), 'future_points': len(selection['future_points']),
            'future_call_skipped': not future_needed, 'body_words': len(main.split()),
            'future_words': len(suffix.split()), 'total_words': len(combined.split()),
            'body_length_budget': body_budget, 'future_length_budget': future_budget,
            'frozen_main_sha256_utf8': prefix_sha, 'final_prefix_verified': True,
            'writer_content_format': 'markdown',
            'output_formats': ['markdown'], 'frozen_body_preserved': True,
            'word_count_basis': 'Whitespace-separated tokens including markup; not a Chinese character/word limit validator',
            'b1_original_response_sha256': frozen_sha,
            'semantic_quality_guaranteed': False,
            'fixture_note': 'Synthetic plumbing only; not model evidence' if backend.name == 'fixture' else None}
        json_write(out / 'summary.json', summary)
        manifest['status'] = 'completed';manifest['finished_utc'] = utc_now()
        json_write(out / 'manifest.json', manifest)
        return summary
    except BaseException as exc:
        manifest['status'] = 'failed';manifest['finished_utc'] = utc_now()
        json_write(out / 'manifest.json', manifest)
        json_write(out / 'failure.json', {'type': type(exc).__name__, 'message': redact(str(exc)),
                   'completed_stages': receipts, 'automatic_retry': False})
        raise
