"""Independent dynamic-envelope and rendered full-song acceptance checks."""
from __future__ import annotations

from collections import Counter, defaultdict, deque
from fractions import Fraction
from pathlib import Path
import hashlib
import json
import sys
import tempfile
import unittest

import mido

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from eightbar.dual_dynamics import shape_events, value_at


def _note_records(document):
    records = []
    for track_index, track in enumerate(document.tracks):
        pending = defaultdict(deque)
        tick = 0
        port = 0
        for message in track:
            tick += message.time
            if message.type == 'midi_port':
                port = message.port
            if message.type == 'note_on' and message.velocity:
                pending[message.channel, message.note].append((tick, port, message.velocity))
            elif message.type in ('note_on', 'note_off'):
                key = message.channel, message.note
                if not pending[key]:
                    continue
                start, original_port, velocity = pending[key].popleft()
                records.append((track_index, original_port, message.channel,
                                start, tick, message.note, velocity, message.velocity))
        assert not any(pending.values()), f'Stuck musical note in {track.name}'
    return records


def _rounded_ratio(tick, numerator, denominator):
    ratio = Fraction(tick * numerator, denominator)
    return (ratio.numerator * 2 + ratio.denominator) // (ratio.denominator * 2)


def _independent_envelope(tick, hits, length, dynamics, ppq):
    # This acceptance formula does not call the production dynamics module.
    baseline = dynamics['velocity_start'] + (dynamics['velocity_end'] - dynamics['velocity_start']) * tick / length
    recovery = dynamics['recovery_beats'] * ppq
    remaining = [dynamics['drum_reaction'] * velocity / 127 * (1 - (tick - hit) / recovery)
                 for hit, velocity in hits if 0 <= tick - hit < recovery]
    return max(1, min(127, baseline * (1 - max(remaining, default=0))))


def _drum_note_removed(plan, section, record, repeat, source_ppq, block_beats, roles):
    if plan['version'] < 2:
        return False
    track, _, channel, start, _, pitch, _, _ = record
    if roles[track] == 'Keep' or (roles[track] != 'Drums' and channel != 9):
        return False
    onset = repeat * block_beats + Fraction(start, source_ppq)
    return any(cut['track_index'] == track
               and Fraction(str(cut['start_beat'])) <= onset < Fraction(str(cut['end_beat']))
               and (not cut['pitches'] or pitch in cut['pitches'])
               for cut in section['drum_cuts'])


def _instrument_segments(plan, section, track, channel, start, finish, repeat,
                         block_length, ppq, roles):
    """Independent interval subtraction for v3 note-span cuts."""
    if plan['version'] < 3 or roles[track] in ('Keep', 'Drums') or channel == 9:
        return [(start, finish)]
    offset = repeat * block_length
    fragments = [(start + offset, finish + offset)]
    for cut in sorted(section.get('instrument_cuts', []), key=lambda item: item['start_beat']):
        if cut['track_index'] != track:
            continue
        left = int(Fraction(str(cut['start_beat'])) * ppq + Fraction(1, 2))
        right = int(Fraction(str(cut['end_beat'])) * ppq + Fraction(1, 2))
        kept = []
        for a, b in fragments:
            if right <= a or left >= b:
                kept.append((a, b))
            else:
                if a < left:
                    kept.append((a, left))
                if right < b:
                    kept.append((right, b))
        fragments = kept
    return [(a - offset, b - offset) for a, b in fragments]


def verify_project(folder):
    """Verify written MIDI against its persisted plan and unchanged patterns."""
    folder = Path(folder)
    manifest = json.loads((folder / 'Project.json').read_text(encoding='utf-8'))
    plan = json.loads((folder / 'AI Arrangement.json').read_text(encoding='utf-8'))
    assert manifest['arrangement_plan'] == plan
    assert manifest['arrangement_mode'] == 'ai_json'
    patterns = {token: mido.MidiFile(folder / f'{token}.mid') for token in ('A', 'B', 'AC', 'BC')}
    notes = {token: _note_records(document) for token, document in patterns.items()}
    for token in ('A', 'B'):
        raw = (folder / f'{token}.mid').read_bytes()
        assert hashlib.sha256(raw).hexdigest() == manifest['source_files'][token]['sha256']
        assert raw == Path(manifest['source_files'][token]['path']).read_bytes()
    song = mido.MidiFile(folder / 'Song.mid')
    ppq = song.ticks_per_beat
    end = max(sum(message.time for message in track) for track in song.tracks)
    assert end == plan['total_bars'] * ppq * 4
    assert manifest['song_bars'] == plan['total_bars']
    route_data = json.loads((folder / 'Song routing.json').read_text(encoding='utf-8'))
    routes = {(label, source['track_index'], source['port'], source['channel']): route
              for route in route_data for label, source in route['sources'].items()}
    expected = Counter()
    protected_expected = Counter()
    expected_markers = []
    source_melodic_muted = 0
    changed_velocities = 0
    removed_drums = 0
    retained_drums = 0
    drum_lines = defaultdict(lambda: {'source_hits': 0, 'removed_hits': 0, 'retained_hits': 0})
    removed_instruments = shortened_instruments = retriggered_instruments = 0
    offset = 0
    for section_number, section in enumerate(plan['sections'], 1):
        token = section['pattern']
        label = token[0]
        document = patterns[token]
        roles = manifest['roles_a' if label == 'A' else 'roles_b']
        block_length = manifest['source_files'][label]['bars'] * ppq * 4
        section_length = block_length * section['repeats']
        protected = {index for index, role in enumerate(roles) if role in ('Drums', 'Keep')}
        protected |= {index for index, track in enumerate(patterns[label].tracks)
                      if any(getattr(message, 'channel', None) == 9 for message in track)}
        hits = [(repeat * block_length + _rounded_ratio(record[3], ppq, patterns[label].ticks_per_beat), record[6])
                for repeat in range(section['repeats']) for record in notes[label]
                if (roles[record[0]] == 'Drums' or record[2] == 9)
                and not _drum_note_removed(plan, section, record, repeat,
                                           patterns[label].ticks_per_beat,
                                           Fraction(block_length, ppq), roles)]
        dynamics_by_track = {item['track_index']: item for item in section['dynamics']}
        for repeat in range(section['repeats']):
            marker = f"{section['name']} | {token} | {repeat + 1}/{section['repeats']}"
            marker = marker.encode(song.charset, errors='replace').decode(song.charset)
            expected_markers.append((offset, marker))
            for track, port, channel, start, finish, pitch, velocity, off_velocity in notes[token]:
                record = (track, port, channel, start, finish, pitch, velocity, off_velocity)
                is_drum = roles[track] == 'Drums' or channel == 9
                if is_drum:
                    drum_line = drum_lines[section_number, label, track, pitch]
                    drum_line['source_hits'] += 1
                if _drum_note_removed(plan, section, record, repeat, document.ticks_per_beat,
                                      Fraction(block_length, ppq), roles):
                    removed_drums += 1
                    drum_line['removed_hits'] += 1
                    continue
                if is_drum:
                    drum_line['retained_hits'] += 1
                if track not in section['active_tracks']:
                    assert track not in protected, 'Protected track may never be muted'
                    source_melodic_muted += 1
                    continue
                route = routes[label, track, port, channel]
                local = _rounded_ratio(start, ppq, document.ticks_per_beat)
                finish = _rounded_ratio(finish, ppq, document.ticks_per_beat)
                new_velocity = velocity
                if track not in protected:
                    dynamics = dynamics_by_track[track]
                    envelope = _independent_envelope(repeat * block_length + local,
                                                     hits, section_length, dynamics, ppq)
                    new_velocity = max(1, min(127, int(velocity * envelope / 127 + 0.5)))
                    changed_velocities += new_velocity != velocity
                segments = _instrument_segments(plan, section, track, channel, local, finish,
                                                repeat, block_length, ppq, roles)
                if not segments:
                    removed_instruments += 1
                elif segments != [(local, finish)]:
                    shortened_instruments += 1
                    retriggered_instruments += sum(a > local for a, b in segments)
                for segment_start, segment_end in segments:
                    item = (route['track_name'], route['port'], route['channel'],
                            offset + segment_start, offset + segment_end, pitch, new_velocity, off_velocity)
                    expected[item] += 1
                    if track in protected:
                        protected_expected[item] += 1
                    if roles[track] == 'Drums' or channel == 9:
                        retained_drums += 1
            offset += block_length
    actual = Counter((song.tracks[track].name, port, channel, start, end, pitch, velocity, off_velocity)
                     for track, port, channel, start, end, pitch, velocity, off_velocity in _note_records(song))
    assert actual == expected, {'missing': list((expected - actual).items())[:6],
                                'unexpected': list((actual - expected).items())[:6]}
    actual_markers = []
    for track in song.tracks:
        tick = 0
        for message in track:
            tick += message.time
            if message.type == 'marker':
                actual_markers.append((tick, message.text))
    assert actual_markers == expected_markers
    automation = json.loads((folder / 'Automation.json').read_text(encoding='utf-8'))
    pulses = 0
    for item in automation['files']:
        control = mido.MidiFile(folder / item['file'])
        assert max(sum(message.time for message in track) for track in control.tracks) == end
        note_ons = []
        for track in control.tracks:
            sounding = False
            tick = 0
            for message in track:
                tick += message.time
                if message.type in ('note_on', 'note_off'):
                    assert message.note == 60 and message.channel == 0
                    if message.type == 'note_on' and message.velocity:
                        assert not sounding, f'Overlapping control note: {item["file"]}'
                        sounding = True
                        note_ons.append((tick, message.velocity))
                    else:
                        assert sounding, f'Orphan control note-off: {item["file"]}'
                        sounding = False
            assert not sounding
        assert note_ons and note_ons[-1] == (end - 1, 127), 'Last control pulse must restore neutral'
        assert len({velocity for _, velocity in note_ons}) > 1
        pulses += len(note_ons)
    assert source_melodic_muted > 0, 'Acceptance song should actually use musical part rests'
    assert changed_velocities > 0, 'Acceptance song should actually apply dynamics'
    handoff_verified = False
    handoff_path = folder / 'Song Structure.json'
    if plan['version'] >= 2 or handoff_path.exists():
        structure = json.loads(handoff_path.read_text(encoding='utf-8'))
        assert structure['format'] == '16bar.song_structure' and structure['version'] == 1
        assert structure['total_bars'] == plan['total_bars']
        assert structure['time_signature'] == [4, 4]
        assert len(structure['sections']) == len(plan['sections'])
        next_bar = 1
        next_seconds = 0.0
        for number, (actual_section, planned) in enumerate(zip(structure['sections'], plan['sections'])):
            source = manifest['source_files'][planned['pattern'][0]]
            bars = source['bars'] * planned['repeats']
            seconds = bars * 240.0 / source['bpm']
            assert actual_section['id'] == f'section_{number + 1:02d}'
            assert actual_section['name'] == planned['name']
            assert actual_section['kind'] == planned['kind']
            assert actual_section['pattern'] == planned['pattern']
            assert actual_section['bars'] == bars
            assert actual_section['start_bar'] == next_bar
            assert actual_section['end_bar'] == next_bar + bars - 1
            assert abs(actual_section['start_seconds'] - next_seconds) < 0.001
            assert abs(actual_section['end_seconds'] - (next_seconds + seconds)) < 0.001
            assert abs(actual_section['bpm'] - source['bpm']) < 0.001
            if planned['kind'] in ('intro', 'outro'):
                assert actual_section['vocal_mode'] == 'instrumental' and actual_section['suggested_lines'] == 0
            else:
                assert actual_section['vocal_mode'] == 'lyrics' and actual_section['suggested_lines'] > 0
            next_bar += bars
            next_seconds += seconds
        assert next_bar == plan['total_bars'] + 1
        assert abs(structure['duration_seconds'] - next_seconds) < 0.001
        assert abs(structure['duration_seconds'] - manifest['song_seconds']) < 0.001
        handoff_verified = True
    drum_report_verified = False
    drum_report_path = folder / 'Drum Removal Report.json'
    if drum_report_path.exists():
        drum_report = json.loads(drum_report_path.read_text(encoding='utf-8'))
        assert drum_report['version'] == 1
        assert len(drum_report['sections']) == len(plan['sections'])
        expected_totals = {'source_drum_notes': removed_drums + retained_drums,
                           'drum_notes_trimmed': removed_drums,
                           'drum_notes_retained': retained_drums}
        assert drum_report['totals'] == expected_totals, drum_report['totals']
        actual_line_keys = set()
        for ordinal, (reported, planned) in enumerate(zip(drum_report['sections'], plan['sections'])):
            assert reported['section'] == ordinal
            assert reported['name'] == planned['name'] and reported['pattern'] == planned['pattern']
            master = planned['pattern'][0]
            selected_roles = manifest['roles_a' if master == 'A' else 'roles_b']
            section_totals = {key: 0 for key in expected_totals}
            for line in reported['line_stats']:
                key = (ordinal + 1, master, line['track_index'], line['pitch'])
                assert key in drum_lines and key not in actual_line_keys, key
                actual_line_keys.add(key)
                expected_line = drum_lines[key]
                assert line['source_drum_notes'] == expected_line['source_hits'], key
                assert line['drum_notes_trimmed'] == expected_line['removed_hits'], key
                assert line['drum_notes_retained'] == expected_line['retained_hits'], key
                assert line['protected'] == (selected_roles[line['track_index']] == 'Keep'), key
                assigned = manifest.get('drum_note_map', {}).get(master, {}).get(str(line['track_index']), {}).get(str(line['pitch']))
                if assigned is not None:
                    assert line['label'] == assigned, (line, assigned)
                for total_key in expected_totals:
                    section_totals[total_key] += line[total_key]
            for total_key, value in section_totals.items():
                assert reported[total_key] == value, (ordinal, total_key)
        assert actual_line_keys == set(drum_lines), 'Every source line must appear once per section'
        drum_report_verified = True
    return {'bars': plan['total_bars'], 'sections': len(plan['sections']),
            'expanded_blocks': len(expected_markers), 'musical_notes': sum(actual.values()),
            'protected_notes_unchanged': sum(protected_expected.values()),
            'melodic_notes_intentionally_muted': source_melodic_muted,
            'melodic_velocities_changed': changed_velocities,
            'drum_notes_intentionally_removed': removed_drums,
            'retained_drum_notes_unchanged': retained_drums,
            'drum_removal_only_verified': True,
            'drum_report_verified': drum_report_verified,
            'drum_lines': [{'section': section, 'master': master, 'track_index': track, 'pitch': pitch, **counts}
                           for (section, master, track, pitch), counts in sorted(drum_lines.items())],
            'instrument_notes_removed': removed_instruments,
            'instrument_notes_shortened': shortened_instruments,
            'instrument_notes_retriggered': retriggered_instruments,
            'automation_files': len(automation['files']), 'control_pulses': pulses,
            'balanced_notes': True, 'all_note_events_match_plan': True,
            'song_structure_handoff_verified': handoff_verified,
            'persisted_json_and_markers_match': True}


class DrumEnvelopeTests(unittest.TestCase):
    ppq = 960
    dynamics = {'track_index': 3, 'velocity_start': 127, 'velocity_end': 127,
                'drum_reaction': 0.5, 'recovery_beats': 1}

    def test_actual_hit_dip_and_recovery_without_invented_beats(self):
        # The sole imported hit falls on an irregular tick. Empty beats have no
        # dip, and the curve recovers from the real hit at its exact endpoint.
        hits = [(333, 127)]
        self.assertEqual(value_at(0, hits, 960, 7680, self.dynamics), 127)
        self.assertEqual(value_at(332, hits, 960, 7680, self.dynamics), 127)
        self.assertEqual(value_at(333, hits, 960, 7680, self.dynamics), 63.5)
        self.assertEqual(value_at(813, hits, 960, 7680, self.dynamics), 95.25)
        self.assertEqual(value_at(1293, hits, 960, 7680, self.dynamics), 127)
        self.assertEqual(value_at(1920, hits, 960, 7680, self.dynamics), 127)

    def test_hit_strength_and_strongest_recovery_wins(self):
        weak = value_at(400, [(400, 64)], 960, 7680, self.dynamics)
        strong = value_at(400, [(400, 127)], 960, 7680, self.dynamics)
        self.assertGreater(weak, strong)
        simultaneous = value_at(400, [(400, 127), (400, 64)], 960, 7680, self.dynamics)
        self.assertEqual(simultaneous, strong)
        self.assertEqual(value_at(880, [(400, 127), (880, 20)], 960, 7680, self.dynamics), 95.25)

    def test_envelope_continues_across_repeats(self):
        dynamics = dict(self.dynamics, velocity_start=40, velocity_end=120,
                        drum_reaction=0)
        self.assertEqual(value_at(3840, [], 960, 7680, dynamics), 80)
        self.assertEqual(value_at(7680, [], 960, 7680, dynamics), 120)
        event = mido.Message('note_on', note=64, velocity=127, channel=3)
        shaped, control, stats = shape_events([(0, 1, event)], [], 960, 3840,
                                               7680, dynamics, 3,
                                               block_length_ticks=3840)
        on = next(message for _, _, message in shaped if message.type == 'note_on')
        self.assertEqual(on.velocity, 80)
        self.assertEqual(event.velocity, 127, 'Source event must stay unchanged')

    def test_sample_exact_drum_ticks_and_balanced_control_notes(self):
        source = [(0, 0, mido.Message('program_change', channel=3, program=81)),
                  (0, 1, mido.Message('control_change', channel=3, control=11, value=127)),
                  (0, 2, mido.Message('control_change', channel=3, control=74, value=70)),
                  (333, 3, mido.Message('note_on', channel=3, note=64, velocity=100)),
                  (1333, 4, mido.Message('note_off', channel=3, note=64, velocity=23))]
        before = [(tick, order, msg.dict()) for tick, order, msg in source]
        shaped, controls, stats = shape_events(source, [(333, 127)], 960, 0,
                                               3840, self.dynamics, 3,
                                               block_length_ticks=3840)
        self.assertEqual([(t, o, msg.dict()) for t, o, msg in source], before)
        note_on = next(msg for _, _, msg in shaped if msg.type == 'note_on')
        self.assertEqual(note_on.velocity, 50)
        self.assertIn(source[0], shaped)
        self.assertIn(source[2], shaped)
        self.assertIn(source[4], shaped)
        cc11 = [(tick, msg.value) for tick, _, msg in shaped
                if msg.type == 'control_change' and msg.control == 11]
        self.assertIn((333, 64), cc11)
        self.assertIn((1293, 127), cc11)
        self.assertEqual(stats['cc11_replaced'], 1)
        self.assertEqual(stats['velocity_changes'], 1)
        active = 0
        for tick, order, message in controls:
            if message.type == 'note_on' and message.velocity:
                self.assertEqual(active, 0, 'No overlapping controller notes')
                active += 1
            else:
                active -= 1
                self.assertEqual(active, 0, 'No orphan note-off')
        self.assertEqual(active, 0)
        self.assertEqual(controls[-1][0], 3840)

    def test_expression_opt_out_retains_original_cc11(self):
        event = (0, 2, mido.Message('control_change', channel=3, control=11, value=88))
        shaped, controls, stats = shape_events([event], [], 960, 0, 3840,
                                               self.dynamics, 3, expression=False,
                                               block_length_ticks=3840)
        self.assertEqual(shaped, [event])
        self.assertEqual(stats['cc11_generated'], 0)
        self.assertTrue(controls)

    def test_imported_reset_cannot_erase_expression_curve(self):
        events = [(0, 13, mido.Message('control_change', channel=3, control=121, value=0)),
                  (0, 14, mido.Message('note_on', channel=3, note=64, velocity=127)),
                  (960, 15, mido.Message('note_off', channel=3, note=64, velocity=0))]
        shaped, _, _ = shape_events(events, [(0, 127)], 960, 0, 3840,
                                    self.dynamics, 3, block_length_ticks=3840)
        controls = [message for tick, _, message in shaped if tick == 0 and message.type == 'control_change']
        self.assertEqual([message.control for message in controls], [121, 11])
        self.assertEqual(controls[-1].value, 64)


class FullSongIntegrationTests(unittest.TestCase):
    def test_source_change_rejected_before_creating_exports(self):
        from test_dual_ai import fixture
        from eightbar.dual_master import export_dual, StudioError
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            a, b, c = [folder / f'{label}.mid' for label in 'ABC']
            for path in (a, b, c):
                fixture(path, bars=4)
            hashes = {label: hashlib.sha256(path.read_bytes()).hexdigest()
                      for label, path in zip('ABC', (a, b, c))}
            hashes['B'] = '0' * 64
            with self.assertRaisesRegex(StudioError, 'B changed'):
                export_dual(a, b, c, {5: 'Keep'}, {5: 'Keep'}, folder / 'exports',
                            expected_source_hashes=hashes)
            self.assertFalse((folder / 'exports').exists())

    def test_repeated_sections_and_protected_role_transition(self):
        from test_dual_ai import fixture, good_plan
        from eightbar.dual_ai import build_context
        from eightbar.dual_master import export_dual
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            a, b, c = [folder / f'{label}.mid' for label in 'ABC']
            for path in (a, b, c):
                fixture(path, bars=4)
            roles_a, roles_b = {5: 'Keep'}, {5: 'Keep', 6: 'Keep'}
            context = build_context(a, b, c, roles_a, roles_b, target_bars=64)
            plan = good_plan(context)
            for section in plan['sections']:
                section['repeats'] = 2
                if section['pattern'][0] == 'B' and 6 not in section['active_tracks']:
                    section['active_tracks'].append(6)
            result = export_dual(a, b, c, roles_a, roles_b, folder / 'exports',
                                 arrangement_plan=plan, target_bars=64)
            report = verify_project(result['folder'])
            self.assertEqual(report['expanded_blocks'], 16)
            song = mido.MidiFile(Path(result['folder']) / 'Song.mid')
            lane = next(track for track in song.tracks if track.name == 'Countermelody')
            tick = 0
            # Third section ends at bar24; B enters as Keep at bar25.
            transition = 24 * 4 * song.ticks_per_beat
            resets = []
            for message in lane:
                tick += message.time
                if tick == transition and message.type == 'control_change' and message.control == 11:
                    resets.append(message.value)
            self.assertTrue(resets)
            self.assertEqual(resets[-1], 127, 'Protected section must reset prior generated expression')


if __name__ == '__main__':
    unittest.main()
