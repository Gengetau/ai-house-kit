"""Cross-package contract tests; run with world-catalog on PYTHONPATH."""
import copy
import json
import unittest
from pathlib import Path
from worldcatalog.catalog import load
from worldcatalog.fixtures import fixture_vocabulary, PROFILE, VERSION

CORPUS = json.loads(Path(__file__).with_name('cases.json').read_text())


def alignment_errors(corpus):
    errors = []
    for case in corpus['cases']:
        init = case['initial']
        if init.get('vocab_version') != VERSION or init.get('fixture_profile') != PROFILE:
            errors.append(case['id'] + ': profile/version mismatch')
        try:
            v = fixture_vocabulary(init['fixture_profile'], world_id=init['world_id'],
                synthetic=init.get('synthetic'), fake_channels=init.get('fake_channels'),
                paid_calls=init['policy'].get('paid_calls'))
        except ValueError:
            errors.append(case['id'] + ': unsafe profile'); continue
        for step in case['steps']:
            command = step.get('plan', step)
            action = command.get('action')
            if action not in {'eat', 'cook', 'move'}:
                continue
            if any(k in command for k in ('duration', 'resource', 'destination')):
                errors.append(case['id'] + ': noncanonical command')
            actor = init['actors'][step.get('actor', 'nell')]
            reasons = v.guard(action, command.get('args'), dict(actor, actor_id=step.get('actor', 'nell')), init['resource_rows'])
            errors.extend(case['id'] + ': ' + r for r in reasons)
    return errors


class AlignmentTests(unittest.TestCase):
    def test_all_cases_match_registered_catalog(self):
        self.assertEqual(CORPUS['casepack_version'], 2)
        self.assertEqual(len(CORPUS['cases']), 21)
        self.assertEqual(alignment_errors(CORPUS), [])

    def test_raw_food_and_bad_recipe_regressions_fail(self):
        for action, args in [('eat', {'item': 'egg'}), ('eat', {'item': 'rice'}),
                             ('cook', {'resource': 'rice'}), ('cook', {'recipe': 'unknown'})]:
            data = copy.deepcopy(CORPUS)
            data['cases'][0]['steps'].insert(0, dict(at=0, op='command', actor='nell', action=action, args=args))
            self.assertTrue(alignment_errors(data))

    def test_unregistered_duration_or_profile_fail(self):
        for change in ('duration', 'version', 'fake'):
            data = copy.deepcopy(CORPUS)
            c = next(c for c in data['cases'] if c['id'] == 'R01')
            if change == 'duration': c['steps'][0]['duration'] = 2
            if change == 'version': c['initial']['vocab_version'] = load()['vocab_version']
            if change == 'fake': c['initial']['fake_channels'] = False
            self.assertTrue(alignment_errors(data))

    def test_i01_uses_registered_phone_and_recipe(self):
        c = next(c for c in CORPUS['cases'] if c['id'] == 'I01')
        self.assertEqual(c['steps'][0]['args'], {'recipe': 'rice'})
        read = next(s for s in c['steps'] if s.get('choice') == 'read_phone')
        self.assertEqual(read['args'], {'message_id': 'dm1'})
        self.assertGreaterEqual(read['at'], 10)
        self.assertGreaterEqual(c['steps'][-1]['at'], read['at'] + 1)

if __name__ == '__main__': unittest.main()
