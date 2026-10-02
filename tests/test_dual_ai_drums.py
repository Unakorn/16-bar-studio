"""Versioned drum-cut plans, scoped source metadata, and legacy compatibility."""
from copy import deepcopy
import io
import json
import math
import unittest
from unittest.mock import patch

import mido

import test_dual_ai as legacy
from test_dual_ai import ai, fixture, good_plan, AISettings, StudioError


class DrumPlanTests(unittest.TestCase):
    setUp = legacy.PlanTests.setUp

    def version2(self):
        plan = deepcopy(self.plan)
        plan['version'] = 2
        for section in plan['sections']:
            section['drum_cuts'] = []
        plan['sections'][0]['drum_cuts'] = [
            {'track_index': 3, 'start_beat': 0, 'end_beat': 4, 'pitches': [36]}]
        return plan

    def reject(self, mutate, context=None):
        plan = self.version2()
        mutate(plan)
        with self.assertRaises(StudioError):
            ai.validate_plan(plan, context or self.context)

    def test_v2_schema_and_valid_normalized_cut(self):
        plan = self.version2()
        result = ai.validate_plan(plan, self.context)
        self.assertEqual(result['version'], 2)
        self.assertEqual(result['sections'][0]['drum_cuts'], plan['sections'][0]['drum_cuts'])
        self.assertIsNot(result['sections'][0]['drum_cuts'], plan['sections'][0]['drum_cuts'])
        self.assertEqual(ai.PLAN_SCHEMA['properties']['version']['enum'], [2])
        self.assertIn('drum_cuts', ai.PLAN_SCHEMA['properties']['sections']['items']['required'])
        self.assertEqual(ai.parse_plan(json.dumps(plan), self.context), result)

    def test_legacy_v1_stays_unchanged_with_trimming_enabled_or_disabled(self):
        for enabled in (True, False):
            context = ai.build_context(self.a, self.b, self.c, self.roles, self.roles,
                                       drum_trimming=enabled)
            result = ai.validate_plan(self.plan, context)
            self.assertEqual(result, self.plan)
            self.assertTrue(all('drum_cuts' not in section for section in result['sections']))
        plan = self.version2()
        plan['version'] = 1
        with self.assertRaises(StudioError):
            ai.validate_plan(plan, self.context)
        for bad_version in (True, 1.0, 3):
            self.reject(lambda p: p.update(version=bad_version))

    def test_enabled_requires_cut_and_disabled_rejects_cut(self):
        self.reject(lambda p: p['sections'][0].update(drum_cuts=[]))
        self.reject(lambda p: p['sections'][0].pop('drum_cuts'))
        context = ai.build_context(self.a, self.b, self.c, self.roles, self.roles,
                                   drum_trimming=False)
        self.reject(lambda p: None, context)
        plan = self.version2()
        plan['sections'][0]['drum_cuts'] = []
        self.assertEqual(ai.validate_plan(plan, context)['version'], 2)
        for value in (0, 1, None, 'false'):
            with self.assertRaises(StudioError):
                ai.build_context(self.a, self.b, self.c, self.roles, self.roles,
                                 drum_trimming=value)

    def test_track_metadata_uses_actual_drum_pitches_and_keep_overrides(self):
        master = self.context['masters']['A']
        self.assertEqual(master['editable_drum_tracks'], [3])
        self.assertEqual(master['tracks'][3]['drum_pitches'], [36])
        self.assertEqual(master['tracks'][3]['drum_note_count'], 1)
        self.assertEqual(master['tracks'][3]['drum_pitch_details'], [
            {'pitch': 36, 'note_count': 1,
             'onsets': [{'source_beat': 0.0, 'velocity': 100}], 'onsets_sampled': False}])
        self.assertEqual(master['tracks'][4]['drum_pitches'], [], 'Channel 9 controls are not drum hits')
        keep_roles = dict(self.roles, **{'3': 'Keep'})
        context = ai.build_context(self.a, self.b, self.c, keep_roles, keep_roles)
        self.assertEqual(context['masters']['A']['editable_drum_tracks'], [])
        self.assertEqual(context['masters']['A']['drum_note_count'], 1, 'Kept drums remain reactive triggers')
        self.reject(lambda p: None, context)
        plan = self.version2()
        plan['sections'][0]['drum_cuts'] = []
        self.assertEqual(ai.validate_plan(plan, context)['version'], 2)

    def test_custom_drums_all_pitches_and_mixed_tracks_only_channel_9_pitches(self):
        midi = mido.MidiFile(self.a)
        track = midi.tracks[4]
        end = track.pop()
        for pitch in (42, 64):
            track.append(mido.Message('note_on', channel=9, note=pitch, velocity=90))
            track.append(mido.Message('note_off', channel=9, note=pitch, velocity=5, time=120))
            end.time -= 120
        track.append(end)
        midi.save(self.a)
        roles = dict(self.roles, **{'6': 'Drums'})
        context = ai.build_context(self.a, self.b, self.c, roles, self.roles)
        tracks = context['masters']['A']['tracks']
        self.assertEqual(tracks[4]['drum_pitches'], [42, 64])
        self.assertEqual(tracks[4]['pitched_note_count'], 1)
        self.assertEqual(tracks[6]['drum_pitches'], [66], 'Custom drums need no GM mapping')
        self.assertEqual(context['masters']['A']['editable_drum_tracks'], [3, 4, 6])
        plan = good_plan(context)
        plan['version'] = 2
        for section in plan['sections']:
            section['drum_cuts'] = []
            if section['pattern'][0] == 'A' and 6 not in section['active_tracks']:
                section['active_tracks'].append(6)
        plan['sections'][0]['drum_cuts'] = [{'track_index': 4, 'start_beat': 0,
                                           'end_beat': 4, 'pitches': [64, 42]}]
        result = ai.validate_plan(plan, context)
        self.assertEqual(result['sections'][0]['drum_cuts'][0]['pitches'], [42, 64])
        plan['sections'][0]['drum_cuts'][0]['pitches'] = [60]
        with self.assertRaises(StudioError):
            ai.validate_plan(plan, context)

    def test_cut_bounds_quarter_beat_grid_and_known_pitches(self):
        bad_fields = [('track_index', 1), ('track_index', 4), ('track_index', 999),
                      ('track_index', True), ('start_beat', -0.25), ('start_beat', 0.1),
                      ('start_beat', True), ('start_beat', math.nan),
                      ('end_beat', math.inf), ('end_beat', 10**500),
                      ('end_beat', 32.25), ('end_beat', 0), ('end_beat', 0.125),
                      ('pitches', [True]), ('pitches', [36, 36]), ('pitches', [35]),
                      ('pitches', [128]), ('pitches', '36')]
        for key, value in bad_fields:
            with self.subTest(key=key, value=str(value)[:20]):
                self.reject(lambda p: p['sections'][0]['drum_cuts'][0].update({key: value}))
        self.reject(lambda p: p['sections'][0]['drum_cuts'][0].update(extra=True))
        self.reject(lambda p: p['sections'][0].update(drum_cuts=[p['sections'][0]['drum_cuts'][0]] * 129))
        for start, end, pitches in ((0, 0.25, []), (31.75, 32, [36]), (0.25, 4.5, [])):
            plan = self.version2()
            plan['sections'][0]['drum_cuts'][0].update(start_beat=start, end_beat=end, pitches=pitches)
            self.assertEqual(ai.validate_plan(plan, self.context)['version'], 2)

    def test_cuts_use_full_section_clock_across_repeats(self):
        plan = self.version2()
        plan['sections'][0]['repeats'] = 2
        # Remove an additional verse so the total remains eight source blocks.
        del plan['sections'][4]
        plan['sections'][0]['drum_cuts'][0].update(start_beat=32, end_beat=36)
        self.assertEqual(ai.validate_plan(plan, self.context)['total_bars'], 64)
        plan['sections'][0]['drum_cuts'][0]['end_beat'] = 64.25
        with self.assertRaises(StudioError):
            ai.validate_plan(plan, self.context)

    def test_no_drums_permits_empty_v2_cuts_and_no_forced_drum_reaction(self):
        fixture(self.a, drums=False)
        fixture(self.b, drums=False)
        context = ai.build_context(self.a, self.b, self.c, self.roles, self.roles)
        plan = good_plan(context)
        plan['version'] = 2
        for section in plan['sections']:
            section['drum_cuts'] = []
            for dynamics in section['dynamics']:
                dynamics['drum_reaction'] = 0
        self.assertEqual(ai.validate_plan(plan, context)['version'], 2)

    def test_request_uses_new_schema_and_returns_v2_plan(self):
        plan = self.version2()
        body = json.dumps({'status': 'completed', 'output': [{'type': 'message', 'content': [
            {'type': 'output_text', 'text': json.dumps(plan)}]}]}).encode()
        with patch.object(ai.urllib.request, 'build_opener') as factory:
            factory.return_value.open.return_value = io.BytesIO(body)
            result = ai.request_plan(self.context, AISettings('sk-test-key123456'))
        request = factory.return_value.open.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(payload['text']['format']['schema']['properties']['version']['enum'], [2])
        self.assertIn('retained', payload['instructions'])
        self.assertIn('Keep ALWAYS', payload['instructions'])
        self.assertEqual(result['version'], 2)

    def test_new_api_response_cannot_bypass_trimming_with_legacy_version(self):
        body = json.dumps({'status': 'completed', 'output': [{'type': 'message', 'content': [
            {'type': 'output_text', 'text': json.dumps(self.plan)}]}]}).encode()
        with patch.object(ai.urllib.request, 'build_opener') as factory:
            factory.return_value.open.return_value = io.BytesIO(body)
            with self.assertRaisesRegex(StudioError, 'new AI request must return version 2'):
                ai.request_plan(self.context, AISettings('sk-test-key123456'))


if __name__ == '__main__':
    unittest.main()
