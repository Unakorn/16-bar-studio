"""Drum-onset deletion must preserve retained performances and actual triggers."""
from __future__ import annotations

from copy import deepcopy
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
from eightbar.dual_ai import build_context, validate_plan
from eightbar.dual_master import export_dual, StudioError
from test_full_song import _note_records, verify_project


def fixture(path):
    ppq = 480
    duration = ppq * 32
    midi = mido.MidiFile(type=1, ticks_per_beat=ppq)
    names = ('Conductor', 'Lead', 'Chords', 'Kit', 'Mixed', 'Preserved Kit', 'Counterline')
    for index, name in enumerate(names):
        events = []
        def note(beat, finish, pitch, channel, velocity=127):
            events.extend([(round(beat * ppq), mido.Message('note_on', note=pitch, channel=channel, velocity=velocity)),
                           (round(finish * ppq), mido.Message('note_off', note=pitch, channel=channel, velocity=19))])
        if index == 0:
            events.append((0, mido.MetaMessage('set_tempo', tempo=500000)))
        elif index == 1:
            for beat in (0, 12, 13, 31.75):
                note(beat, min(beat + 0.125, 32), 72, 0)
        elif index == 2:
            for pitch in (60, 64, 67):
                note(0, 32, pitch, 1)
        elif index == 3:
            note(0, 0.125, 36, 5)
            note(12, 12.125, 38, 5)
            note(13, 13.125, 42, 5)
            note(11.75, 13.25, 46, 5)
            note(31.5, 32, 49, 5)
            note(31.75, 32, 37, 5)
        elif index == 4:
            note(8, 8.5, 40, 9)
            note(8, 8.5, 75, 3)
        elif index == 5:
            note(4, 4.5, 44, 9)
        else:
            note(2, 3, 79, 4)
        track = mido.MidiTrack([mido.MetaMessage('track_name', name=name)])
        tick = 0
        for when, message in sorted(events, key=lambda item: item[0]):
            track.append(message.copy(time=when - tick))
            tick = when
        track.append(mido.MetaMessage('end_of_track', time=duration - tick))
        midi.tracks.append(track)
    midi.save(path)


def plan_for(context, cuts):
    kinds = ('intro', 'verse', 'chorus', 'verse', 'chorus', 'outro')
    patterns = ('A', 'A', 'B', 'AC', 'BC', 'A')
    sections = []
    for number, (kind, token) in enumerate(zip(kinds, patterns)):
        master = context['masters'][token[0]]
        active = [t['index'] for t in master['tracks'] if t['index'] != (6 if number in (0, 5) else -1)]
        sections.append({'name': f'{kind.title()} {number + 1}', 'kind': kind,
                         'pattern': token, 'repeats': 2 if number == 0 else 1,
                         'active_tracks': active, 'reason': 'Remove specified drum onsets while retaining every other event.',
                         'dynamics': [{'track_index': t['index'], 'velocity_start': 127,
                                       'velocity_end': 127, 'drum_reaction': 0.5,
                                       'recovery_beats': 0.5}
                                      for t in master['tracks']
                                      if t['index'] in active and not t['protected'] and t['pitched_note_count']],
                         'drum_cuts': deepcopy(cuts) if number == 0 else []})
    return {'version': 2, 'title': 'Drum cut regression song',
            'summary': 'Full song with exact onset-based percussion trims.',
            'total_bars': 56, 'sections': sections}


class DrumTrims(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.a, self.b, self.c = [self.folder / f'{label}.mid' for label in 'ABC']
        for path in (self.a, self.b, self.c):
            fixture(path)
        self.roles = {3: 'Drums', 5: 'Keep'}
        self.context = build_context(self.a, self.b, self.c, self.roles, self.roles,
                                     target_bars=56, drum_trimming=True)

    def export(self, plan):
        return export_dual(self.a, self.b, self.c, self.roles, self.roles,
                           self.folder / 'exports', arrangement_plan=plan, target_bars=56)

    def test_cut_boundaries_tails_repeats_mixed_channels_and_sources(self):
        cuts = [{'track_index': 3, 'start_beat': 12, 'end_beat': 13, 'pitches': []},
                {'track_index': 3, 'start_beat': 31.75, 'end_beat': 32.25, 'pitches': []},
                {'track_index': 4, 'start_beat': 8, 'end_beat': 8.5, 'pitches': []}]
        plan = plan_for(self.context, cuts)
        result = self.export(plan)
        report = verify_project(result['folder'])
        self.assertEqual(report['drum_notes_intentionally_removed'], 4)
        song = mido.MidiFile(Path(result['folder']) / 'Song.mid')
        records = _note_records(song)
        named = [(song.tracks[n[0]].name, *n[1:]) for n in records]
        ppq = song.ticks_per_beat
        self.assertIn(('Kit', 0, 5, round(11.75 * ppq), round(13.25 * ppq), 46, 127, 19), named)
        self.assertIn(('Kit', 0, 5, 13 * ppq, round(13.125 * ppq), 42, 127, 19), named)
        self.assertFalse(any(n[0] == 'Kit' and n[3] in (12 * ppq, round(31.75 * ppq), 32 * ppq) for n in named))
        # Only channel 10 of the mixed lane is eligible; its pitched voice stays.
        mixed = [n for n in named if n[0].startswith('Mixed') and n[3] == 8 * ppq]
        self.assertEqual(len(mixed), 1)
        self.assertEqual(mixed[0][5:7], (75, 127))
        # Compare all four reusable pattern bytes with the legacy plan path.
        v1 = deepcopy(plan)
        v1['version'] = 1
        for section in v1['sections']:
            section.pop('drum_cuts')
        previous = self.export(v1)
        for token in ('A', 'B', 'AC', 'BC'):
            self.assertEqual((Path(result['folder']) / f'{token}.mid').read_bytes(),
                             (Path(previous['folder']) / f'{token}.mid').read_bytes())

    def test_removed_drum_hit_does_not_drive_velocity_or_control_dip(self):
        cut = {'track_index': 3, 'start_beat': 0, 'end_beat': 0.25, 'pitches': [36]}
        plan = plan_for(self.context, [cut])
        result = self.export(plan)
        song = mido.MidiFile(Path(result['folder']) / 'Song.mid')
        lead = [n for n in _note_records(song) if song.tracks[n[0]].name == 'Lead']
        self.assertEqual(next(n[6] for n in lead if n[3] == 0), 127)
        self.assertEqual(next(n[6] for n in lead if n[3] == 32 * song.ticks_per_beat), 64)
        automation = json.loads((Path(result['folder']) / 'Automation.json').read_text())
        path = next(item['file'] for item in automation['files'] if item['instrument'] == 'Lead')
        control = mido.MidiFile(Path(result['folder']) / path)
        onsets = {}
        for track in control.tracks:
            tick = 0
            for message in track:
                tick += message.time
                if message.type == 'note_on' and message.velocity:
                    onsets[tick] = message.velocity
        self.assertEqual(onsets[0], 127)
        self.assertEqual(onsets[32 * control.ticks_per_beat], 64)

    def test_keep_role_vetoes_cuts_before_export(self):
        plan = plan_for(self.context, [{'track_index': 5, 'start_beat': 4, 'end_beat': 5, 'pitches': []}])
        with self.assertRaises(StudioError):
            self.export(plan)
        self.assertFalse((self.folder / 'exports').exists())

    def test_no_hit_cut_cannot_report_success(self):
        plan = plan_for(self.context, [{'track_index': 3, 'start_beat': 2, 'end_beat': 2.25, 'pitches': [36]}])
        with self.assertRaisesRegex(StudioError, 'did not remove|no drum|No drum|did not match|no matching|remove any'):
            self.export(plan)
        self.assertFalse((self.folder / 'exports').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
