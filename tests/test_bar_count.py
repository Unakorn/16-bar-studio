"""Regression coverage for FL Studio's four-tick duplicate export tail."""
from pathlib import Path
import sys
import tempfile
import unittest

import mido

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from eightbar.dual_master import _absolute, _read, export_dual, inspect_midi, StudioError


def setup(channel=0):
    return [mido.Message('control_change', channel=channel, control=c, value=v)
            for c, v in ((101, 0), (100, 0), (6, 12), (10, 64), (7, 100))] + [
        mido.Message('pitchwheel', channel=channel, pitch=0),
        mido.Message('program_change', channel=channel, program=0)]


def track(name, rows, end):
    result = mido.MidiTrack([mido.MetaMessage('track_name', name=name)])
    last = 0
    for tick, message in sorted(rows, key=lambda row: row[0]):
        result.append(message.copy(time=tick - last))
        last = tick
    result.append(mido.MetaMessage('end_of_track', time=end - last))
    return result


def fixture(path, bars=8, ppq=96, overrun=4, extra=()):
    boundary = bars * ppq * 4
    end = boundary + overrun
    midi = mido.MidiFile(type=1, ticks_per_beat=ppq)
    midi.tracks.append(track('Timing', [(0, mido.MetaMessage('set_tempo', tempo=500000))], end))
    for name, channel, pitches in [('Chords', 0, (60, 64, 67)), ('Drums', 9, (36,))]:
        rows = [(0, message) for message in setup(channel)]
        for pitch in pitches:
            rows += [(0, mido.Message('note_on', channel=channel, note=pitch, velocity=81)),
                     (boundary - 1, mido.Message('note_off', channel=channel, note=pitch, velocity=17))]
        rows += [(boundary, message) for message in setup(channel)]
        rows += [(end, message) for message in setup(channel)]
        if channel == 0:
            rows += [(boundary + offset, message) for offset, message in extra]
        midi.tracks.append(track(name, rows, end))
    midi.save(path)
    return path


def note_events(midi, index):
    return [(tick, message.copy(time=0)) for tick, _, message in _absolute(midi.tracks[index])
            if message.type in ('note_on', 'note_off')]


class BarCountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.path = self.base / 'A.mid'

    def tearDown(self):
        self.temp.cleanup()

    def test_duplicate_fl_tail_is_eight_bars_without_note_or_source_changes(self):
        fixture(self.path)
        raw = self.path.read_bytes()
        source = mido.MidiFile(self.path)
        doc = _read(self.path)
        self.assertEqual(doc.bars, 8)
        self.assertEqual(doc.duration, 3072)
        self.assertTrue(doc.warnings)
        self.assertEqual(doc.raw, raw)
        self.assertEqual(self.path.read_bytes(), raw)
        for index in (1, 2):
            self.assertEqual(note_events(source, index), note_events(doc.midi, index))
            self.assertEqual(sum(m.time for m in doc.midi.tracks[index]), 3072)

    def test_arbitrary_bar_counts_and_ppq_use_same_rule(self):
        for bars, ppq, extra in ((1, 8, 1), (7, 96, 4), (16, 480, 60), (32, 960, 4), (512, 96, 4)):
            with self.subTest(bars=bars, ppq=ppq):
                fixture(self.path, bars, ppq, extra)
                self.assertEqual(inspect_midi(self.path)['bars'], bars)

    def test_meaningful_note_past_boundary_is_not_trimmed(self):
        for channel in (0, 9):
            with self.subTest(channel=channel):
                fixture(self.path, extra=[(1, mido.Message('note_on', channel=channel, note=50, velocity=90)),
                                         (2, mido.Message('note_off', channel=channel, note=50, velocity=20))])
                doc = _read(self.path)
                self.assertEqual(doc.bars, 9)
                self.assertIn((3073, 3074), [(n.start, n.end) for n in doc.notes])

    def test_sustained_note_crossing_boundary_is_not_trimmed(self):
        fixture(self.path)
        midi = mido.MidiFile(self.path)
        rows = [(3074 if message.type == 'note_off' else tick, message)
                for tick, _, message in _absolute(midi.tracks[1]) if message.type != 'end_of_track']
        midi.tracks[1] = track('Chords', rows, 3076)
        midi.save(self.path)
        self.assertEqual(inspect_midi(self.path)['bars'], 9)

    def test_late_changes_and_expression_are_not_trimmed(self):
        messages = [mido.Message('control_change', channel=0, control=7, value=99),
                    mido.Message('control_change', channel=0, control=11, value=127),
                    mido.Message('control_change', channel=0, control=64, value=0),
                    mido.Message('control_change', channel=0, control=96, value=0),
                    mido.Message('pitchwheel', channel=0, pitch=100),
                    mido.Message('program_change', channel=0, program=3),
                    mido.Message('sysex', data=(1, 2, 3)),
                    mido.MetaMessage('marker', text='Deliberate ending')]
        for message in messages:
            with self.subTest(message=message):
                fixture(self.path, extra=[(2, message)])
                self.assertEqual(inspect_midi(self.path)['bars'], 9)

    def test_explicit_full_bar_rest_is_preserved(self):
        fixture(self.path, overrun=384)
        self.assertEqual(inspect_midi(self.path)['bars'], 9)

    def test_larger_verified_setup_tail_is_eight_bars(self):
        fixture(self.path, overrun=13)
        self.assertEqual(inspect_midi(self.path)['bars'], 8)

    def test_tiny_end_marker_only_tail_is_normalized(self):
        fixture(self.path, overrun=0)
        midi = mido.MidiFile(self.path)
        midi.tracks[0][-1].time += 4
        midi.save(self.path)
        self.assertEqual(inspect_midi(self.path)['bars'], 8)

    def test_setup_on_other_port_does_not_prove_duplicate(self):
        fixture(self.path, overrun=0)
        midi = mido.MidiFile(self.path)
        midi.tracks.append(track('Other port', [(0, mido.MetaMessage('midi_port', port=1)),
                                (3076, mido.Message('program_change', channel=0, program=0))], 3076))
        midi.save(self.path)
        self.assertEqual(inspect_midi(self.path)['bars'], 9)

    def test_reset_before_tail_invalidates_prior_controller_state(self):
        fixture(self.path, extra=[(0, mido.Message('control_change', channel=0, control=121, value=0))])
        self.assertEqual(inspect_midi(self.path)['bars'], 9)

    def test_meaningful_content_over_limit_still_fails(self):
        fixture(self.path, bars=512, extra=[(2, mido.MetaMessage('marker', text='extra'))])
        with self.assertRaisesRegex(StudioError, '512 bars'):
            _read(self.path)

    def test_export_has_32_bars_and_raw_originals_with_normalized_variations(self):
        paths = [fixture(self.base / f'{label}.mid') for label in ('A', 'B', 'C')]
        originals = [path.read_bytes() for path in paths]
        result = export_dual(*paths, {}, {}, self.base / 'result')
        out = Path(result['folder'])
        self.assertEqual(result['song_bars'], 32)
        for label, raw in zip(('A', 'B', 'Chord progression C'), originals):
            self.assertEqual((out / f'{label}.mid').read_bytes(), raw)
        for label in ('AC', 'BC'):
            midi = mido.MidiFile(out / f'{label}.mid')
            self.assertEqual(max(sum(m.time for m in t) for t in midi.tracks), 3072)
            self.assertEqual(note_events(mido.MidiFile(paths[0]), 2), note_events(midi, 2))
        song = mido.MidiFile(out / 'Song.mid')
        self.assertEqual(max(sum(m.time for m in t) for t in song.tracks), 12288)
        markers = [tick for t in song.tracks for tick, _, m in _absolute(t) if m.type == 'marker']
        self.assertEqual(markers, [0, 3072, 6144, 9216])
        self.assertEqual([path.read_bytes() for path in paths], originals)


if __name__ == '__main__':
    unittest.main()
