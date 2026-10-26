"""Synthetic plumbing, explicitly not evidence of model behavior."""
from copy import deepcopy


class FixtureBackend:
    name = 'fixture'
    info = {'backend': 'fixture', 'real_model_call': False,
            'note': 'Synthetic deterministic fixture; no efficacy claim'}

    def run(self, stage, prompt, schema, stage_dir, context):
        source = context['source'][0]
        quote = source['text'][:min(60, len(source['text']))]
        refs = [{'source_id': source['source_id'], 'quote': quote}]
        if stage == 'review':
            result = {'points': [{'id': f'R{i}', 'question': f'Synthetic fixture concern {i}?',
                       'source_basis': refs, 'answer_basis': 'partly_supplied',
                       'relevance': 'Fixture-only routing demonstration'} for i in range(1, 4)]}
        elif stage == 'probabilities':
            result = {'estimates': [{'id': p['id'], 'p1_event': 'Synthetic challenge event',
                'p1': .8 if i == 0 else .1, 'p1_reason': 'Synthetic value',
                'p2_event': 'Synthetic constructive follow-up event',
                'p2': .8 if i == 1 else .1, 'p2_reason': 'Synthetic value'}
                for i, p in enumerate(context['review_points'])]}
        elif stage == 'selection':
            records = context['frozen_estimates']['estimates']
            threshold = context['threshold_override'] if context['threshold_override'] is not None else .5
            points = {p['id']: p for p in context['review_points']}
            result = {'audit': {'threshold': threshold, 'threshold_reason': 'Synthetic fixture threshold',
                              'records': deepcopy(records)}}
            for name, key in [('body_points', 'p1'), ('future_points', 'p2')]:
                selected = sorted((r for r in records if r[key] >= threshold), key=lambda r: -r[key])
                result[name] = [{'id': r['id'], 'probability': r[key],
                    'writing_objective': 'Explain supplied evidence' if key == 'p1' else 'Explore a prospective next step',
                    'source_refs': deepcopy(points[r['id']]['source_basis'])} for r in selected]
        elif stage == 'body':
            result = {'document': '[SYNTHETIC FIXTURE — NOT A MODEL RESULT]\n\n' + source['text']}
        elif stage == 'future':
            result = {'appendix': '## Future Work\n\n[SYNTHETIC FIXTURE] Explore the approved prospective objective.'}
        else:
            raise ValueError('Unknown fixture stage')
        return result, {'backend': 'fixture', 'real_model_call': False,
                        'elapsed_seconds': 0.0, 'usage': None, 'fresh_session': True}
