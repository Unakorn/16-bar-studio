"""Source-aware plan validation and mocked Responses API acceptance checks."""
from copy import deepcopy
from pathlib import Path
import io
import json
import math
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

import mido

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from eightbar.ai_arranger import AISettings, DEFAULT_MODEL
from eightbar import dual_ai as ai
from eightbar.dual_master import StudioError, export_dual


def fixture(path, bars=8, drums=True):
    midi = mido.MidiFile(type=1, ticks_per_beat=480)
    end = bars * 4 * 480
    for index, name in enumerate(('Conductor', 'Lead', 'Chords', 'Drums',
                                  'Mixed pad', 'Protected texture', 'Countermelody')):
        track = mido.MidiTrack([mido.MetaMessage('track_name', name=name)])
        if index == 0:
            track.append(mido.MetaMessage('set_tempo', tempo=500000))
            last = 0
        else:
            channel = 9 if index == 3 else index - 1
            if index == 4:
                track.append(mido.Message('control_change', channel=9, control=10, value=64))
            if index == 3 and not drums:
                last = 0
            else:
                pitches = [60, 64, 67] if index == 2 else [36 if index == 3 else 60 + index]
                for pitch in pitches:
                    track.append(mido.Message('note_on', channel=channel, note=pitch, velocity=100))
                for i, pitch in enumerate(pitches):
                    track.append(mido.Message('note_off', channel=channel, note=pitch,
                                              velocity=21, time=120 if i == 0 else 0))
                last = 120
        track.append(mido.MetaMessage('end_of_track', time=end - last))
        midi.tracks.append(track)
    midi.save(path)


def good_plan(context):
    kinds = ('intro', 'verse', 'pre_chorus', 'chorus', 'verse', 'chorus', 'bridge', 'outro')
    patterns = ('A', 'A', 'AC', 'B', 'AC', 'BC', 'B', 'A')
    sections = []
    for number, (kind, token) in enumerate(zip(kinds, patterns)):
        master = context['masters'][token[0]]
        active = [t['index'] for t in master['tracks'] if t['index'] != (6 if number % 2 else -1)]
        sections.append({'name': f'{kind.title()} {number + 1}', 'kind': kind,
                         'pattern': token, 'repeats': 1, 'active_tracks': active,
                         'reason': 'Keep the groove and bring melodic support in at arrivals.',
                         'dynamics': [{'track_index': t['index'], 'velocity_start': 80,
                                       'velocity_end': 115, 'drum_reaction': 0.25,
                                       'recovery_beats': 0.5}
                                      for t in master['tracks']
                                      if t['index'] in active and not t['protected'] and t['pitched_note_count']]})
    return {'version': 1, 'title': 'A full song', 'summary': 'Develop A into B with C-based harmonic contrast.',
            'total_bars': 64, 'sections': sections}


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.a, self.b, self.c = [Path(self.tmp.name) / f'{label}.mid' for label in 'ABC']
        for path in (self.a, self.b, self.c):
            fixture(path)
        self.roles = {3: 'Instrument', 5: 'Keep'}
        self.context = ai.build_context(self.a, self.b, self.c, self.roles, self.roles)
        self.plan = good_plan(self.context)

    def bad(self, mutate):
        candidate = deepcopy(self.plan)
        mutate(candidate)
        with self.assertRaises(StudioError):
            ai.validate_plan(candidate, self.context)

    def test_metadata_protects_role_and_every_channel_9_track(self):
        master = self.context['masters']['A']
        self.assertEqual(master['protected_tracks'], [0, 3, 4, 5])
        self.assertEqual(master['drum_note_count'], 1)
        self.assertEqual(master['drum_onsets'], [{'source_beat': 0.0, 'velocity': 100}])
        self.assertEqual(len(master['sha256']), 64)
        self.assertNotIn(str(self.a.parent), json.dumps(self.context))
        self.assertFalse(master['tracks'][2]['protected'])
        self.assertEqual(master['tracks'][2]['pitch_range'], [60, 67])

    def test_exact_length_feasibility_before_network(self):
        for target in (32, 41, 520, True, '64'):
            with self.subTest(target=target), self.assertRaises(StudioError):
                ai.build_context(self.a, self.b, self.c, self.roles, self.roles, target)
        fixture(self.b, bars=12)
        with self.assertRaises(StudioError):
            ai.build_context(self.a, self.b, self.c, self.roles, self.roles, 40)
        context = ai.build_context(self.a, self.b, self.c, self.roles, self.roles, 64)
        self.assertEqual(context['masters']['B']['bars'], 12)

    def test_valid_plan_normalizes_without_mutating(self):
        self.plan['sections'][0]['active_tracks'].reverse()
        original = deepcopy(self.plan)
        result = ai.validate_plan(self.plan, self.context)
        self.assertEqual(self.plan, original)
        self.assertEqual(result['sections'][0]['active_tracks'], list(range(7)))
        self.assertIsNot(result['sections'][0]['dynamics'][0], self.plan['sections'][0]['dynamics'][0])
        self.assertEqual(result['total_bars'], 64)

    def test_full_song_and_schema_requirements(self):
        invalid = [
            lambda p: p.update(extra=1),
            lambda p: p.update(version=True),
            lambda p: p.update(total_bars=63),
            lambda p: p.update(total_bars=64.0),
            lambda p: p.update(title=''),
            lambda p: p['sections'][0].update(repeats=2),
            lambda p: p['sections'][0].update(repeats=True),
            lambda p: p['sections'][0].update(kind='verse'),
            lambda p: p['sections'][-1].update(kind='verse'),
            lambda p: [s.update(kind='bridge') for s in p['sections'] if s['kind'] == 'verse'],
            lambda p: p['sections'][3].update(kind='verse'),
            lambda p: [s.update(pattern='A') for s in p['sections']],
            lambda p: p['sections'][0].update(pattern=['A']),
            lambda p: p['sections'][0].update(active_tracks=[0, 1, 2, 3, 4, 5, 999]),
            lambda p: p['sections'][0]['active_tracks'].append(1),
            lambda p: p['sections'][0]['active_tracks'].append(True),
            lambda p: p['sections'][0]['active_tracks'].remove(3),
            lambda p: p['sections'][0]['active_tracks'].remove(4),
            lambda p: p['sections'][0]['active_tracks'].remove(5),
            lambda p: p['sections'][0].update(dynamics=[]),
        ]
        for number, mutate in enumerate(invalid):
            with self.subTest(case=number):
                self.bad(mutate)

    def test_dynamics_reject_protected_muted_duplicate_and_bad_numbers(self):
        for key, value in (('track_index', 3), ('track_index', 4), ('track_index', True),
                           ('velocity_start', 0), ('velocity_start', True), ('velocity_end', 128),
                           ('drum_reaction', -0.1), ('drum_reaction', 0.81),
                           ('drum_reaction', math.nan), ('drum_reaction', math.inf),
                           ('drum_reaction', 10**500), ('recovery_beats', 0),
                           ('recovery_beats', 4.1), ('recovery_beats', False)):
            with self.subTest(key=key, value=str(value)[:20]):
                self.bad(lambda p: p['sections'][0]['dynamics'][0].update({key: value}))
        self.bad(lambda p: p['sections'][0]['dynamics'][1].update(track_index=1))
        self.bad(lambda p: p['sections'][1]['dynamics'][0].update(track_index=6))
        self.bad(lambda p: [d.update(drum_reaction=0) for s in p['sections'] for d in s['dynamics']])

    def test_no_drums_allows_nonreactive_velocity_curves(self):
        fixture(self.a, drums=False)
        fixture(self.b, drums=False)
        context = ai.build_context(self.a, self.b, self.c, self.roles, self.roles)
        plan = good_plan(context)
        for section in plan['sections']:
            for entry in section['dynamics']:
                entry['drum_reaction'] = 0
        self.assertEqual(ai.validate_plan(plan, context)['total_bars'], 64)

    def test_empty_pitched_section_rejected(self):
        context = deepcopy(self.context)
        master = context['masters']['A']
        master['pitched_tracks'] = [1, 2, 6]
        for track in (4, 5):
            master['tracks'][track]['pitched_note_count'] = 0
        self.plan['sections'][0].update(active_tracks=[0, 3, 4, 5], dynamics=[])
        with self.assertRaisesRegex(StudioError, 'pitched source track'):
            ai.validate_plan(self.plan, context)

    def test_parse_raw_one_fence_and_reject_corruption(self):
        raw = json.dumps(self.plan)
        self.assertEqual(ai.parse_plan(raw, self.context), ai.parse_plan('```json\n' + raw + '\n```', self.context))
        for text in ('Here is your plan:\n' + raw, '```json\n' + raw + '\n```\nExtra',
                     raw.replace('"version": 1', '"version": 1, "version": 1'),
                     raw.replace('"drum_reaction": 0.25', '"drum_reaction": NaN'),
                     '{}', '[]', raw[:-1]):
            with self.subTest(text=text[:40]), self.assertRaises(StudioError):
                ai.parse_plan(text, self.context)
        prompt = ai.prompt_for_context(self.context)
        self.assertIn('JSON SCHEMA:', prompt)
        self.assertIn('SOURCE METADATA:', prompt)
        self.assertIn('"drum_reaction"', prompt)
        self.assertIn('"target_bars": 64', prompt)

    def test_response_request_schema_redaction_and_validated_return(self):
        key = 'sk-test-secret-key123456'
        response_plan = deepcopy(self.plan)
        response_plan['version'] = 2
        for section in response_plan['sections']:
            section['drum_cuts'] = []
        response_plan['sections'][0]['drum_cuts'] = [
            {'track_index': 3, 'start_beat': 0, 'end_beat': 1, 'pitches': [36]}]
        response_plan['summary'] += ' ' + key
        body = json.dumps({'status': 'completed', 'output': [{'type': 'message', 'content': [
            {'type': 'output_text', 'text': json.dumps(response_plan)}]}]}).encode()
        with patch.object(ai.urllib.request, 'build_opener') as factory:
            factory.return_value.open.return_value = io.BytesIO(body)
            result = ai.request_plan(self.context, AISettings(key, direction='Gentle pump. ' + key))
        request = factory.return_value.open.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(payload['model'], DEFAULT_MODEL)
        self.assertFalse(payload['store'])
        self.assertEqual(payload['text']['format']['schema'], ai.PLAN_SCHEMA)
        self.assertTrue(payload['text']['format']['strict'])
        self.assertNotIn(key, request.data.decode())
        self.assertNotIn(key, json.dumps(result))
        self.assertEqual(request.get_header('Authorization'), 'Bearer ' + key)
        self.assertEqual(result['total_bars'], 64)

    def test_errors_fail_without_fallback_or_credential_leaks(self):
        key = 'sk-test-secret-key123456'
        errors = [urllib.error.HTTPError(ai.API_URL, code, key, {}, io.BytesIO(key.encode()))
                  for code in (400, 401, 403, 404, 408, 429, 503)]
        errors += [TimeoutError(key), urllib.error.URLError(key)]
        for error in errors:
            with self.subTest(error=type(error).__name__), patch.object(ai.urllib.request, 'build_opener') as factory:
                factory.return_value.open.side_effect = error
                with self.assertRaises(StudioError) as caught:
                    ai.request_plan(self.context, AISettings(key))
                self.assertNotIn(key, str(caught.exception))
                factory.return_value.open.assert_called_once()

    def test_incomplete_refused_malformed_and_invalid_plans_rejected(self):
        cases = [b'no json', b'[]', json.dumps({'status': 'incomplete'}).encode(),
                 json.dumps({'status': 'completed', 'output': []}).encode(),
                 json.dumps({'status': 'completed', 'output': [{'type': 'message', 'content': [
                     {'type': 'refusal', 'refusal': 'No'}]}]}).encode(),
                 json.dumps({'status': 'completed', 'output': [{'type': 'message', 'content': [
                     {'type': 'output_text', 'text': '{}'}]}]}).encode()]
        for body in cases:
            with self.subTest(body=body[:50]), patch.object(ai.urllib.request, 'build_opener') as factory:
                factory.return_value.open.return_value = io.BytesIO(body)
                with self.assertRaises(StudioError):
                    ai.request_plan(self.context, AISettings('sk-test-secret-key123456'))

    def test_invalid_credentials_stop_before_network(self):
        for settings in (None, AISettings(''), AISettings('key with space'),
                         AISettings('x', model='unsafe\nmodel'), AISettings('x', direction='a' * 4001)):
            with self.subTest(settings=repr(settings)), patch.object(ai.urllib.request, 'build_opener') as factory:
                with self.assertRaises(StudioError):
                    ai.request_plan(self.context, settings)
                factory.assert_not_called()

    def test_keep_controller_survives_its_destination_instrument_rest(self):
        midi = mido.MidiFile(self.a)
        midi.tracks[0][-1].time -= 77
        midi.tracks[0].insert(-1, mido.Message('control_change', channel=0,
                                             control=76, value=93, time=77))
        midi.save(self.a)
        context = ai.build_context(self.a, self.b, self.c, self.roles, self.roles)
        plan = good_plan(context)
        plan['sections'][0]['active_tracks'].remove(1)
        plan['sections'][0]['dynamics'] = [d for d in plan['sections'][0]['dynamics'] if d['track_index'] != 1]
        result = export_dual(self.a, self.b, self.c, self.roles, self.roles,
                             self.tmp.name, arrangement_plan=plan)
        song = mido.MidiFile(Path(result['folder']) / 'Song.mid')
        lead = next(track for track in song.tracks if track.name == 'Lead')
        tick, events = 0, []
        for message in lead:
            tick += message.time
            events.append((tick, message))
        self.assertTrue(any(tick == 77 and msg.type == 'control_change'
                            and msg.control == 76 and msg.value == 93 for tick, msg in events))
        self.assertFalse(any(tick < 8 * 4 * 480 and msg.type == 'note_on'
                             and msg.velocity for tick, msg in events))

    def test_foreign_keep_expression_disables_generated_cc11_across_both_sources(self):
        midi = mido.MidiFile(self.a)
        midi.tracks[0][-1].time -= 77
        midi.tracks[0].insert(-1, mido.Message('control_change', channel=0,
                                             control=11, value=93, time=77))
        midi.save(self.a)
        context = ai.build_context(self.a, self.b, self.c, self.roles, self.roles)
        plan = good_plan(context)
        plan['sections'][0]['active_tracks'].remove(1)
        plan['sections'][0]['dynamics'] = [d for d in plan['sections'][0]['dynamics'] if d['track_index'] != 1]
        result = export_dual(self.a, self.b, self.c, self.roles, self.roles,
                             self.tmp.name, arrangement_plan=plan, expression_automation=True)
        song = mido.MidiFile(Path(result['folder']) / 'Song.mid')
        lead = next(track for track in song.tracks if track.name == 'Lead')
        tick, expression, velocities = 0, [], []
        for message in lead:
            tick += message.time
            if message.type == 'control_change' and message.control == 11:
                expression.append((tick, message.value))
            if message.type == 'note_on' and message.velocity:
                velocities.append(message.velocity)
        self.assertEqual([tick for tick, value in expression if value == 93],
                         [i * 8 * 4 * 480 + 77 for i, s in enumerate(plan['sections']) if s['pattern'][0] == 'A'])
        report = json.loads((Path(result['folder']) / 'Automation.json').read_text())
        lead_stats = [item for item in report['stats'] if item['track_name'] == 'Lead']
        self.assertTrue(lead_stats)
        self.assertTrue(all(item['cc11_generated'] == item['cc11_replaced'] == 0 for item in lead_stats))
        self.assertTrue(all(value < 100 for value in velocities))
        self.assertTrue(any('Lead - Velocity.mid' in path for path in result['automation_files']))
        self.assertTrue(any('Lead: generated CC11 is off' in warning for warning in result['warnings']))
        self.assertNotIn((64 * 4 * 480, 127), expression, 'No generated neutral CC11 at song end')


if __name__ == '__main__':
    unittest.main()
