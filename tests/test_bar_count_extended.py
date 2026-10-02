"""Longer FL setup tails must not become a musical ninth bar."""
from pathlib import Path
import sys
import unittest

import mido

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import test_bar_count
from test_bar_count import fixture, setup, track, note_events
from eightbar.dual_master import _read, _absolute, export_dual
from eightbar.midi_io import _source_duration


class ExtendedTailTests(unittest.TestCase):
    setUp = test_bar_count.BarCountTests.setUp
    tearDown = test_bar_count.BarCountTests.tearDown

    def assert_bars(self, expected):
        raw = self.path.read_bytes()
        midi = mido.MidiFile(self.path)
        doc = _read(self.path)
        self.assertEqual(doc.bars, expected)
        classic, adjusted = _source_duration(midi, set(), [])
        self.assertEqual((classic, adjusted), (8, True) if expected == 8 else (16, False))
        for original, working in zip(mido.MidiFile(self.path).tracks, doc.midi.tracks):
            notes = lambda lane: [(t, m.copy(time=0)) for t, _, m in _absolute(lane)
                                  if m.type in ('note_on', 'note_off')]
            self.assertEqual(notes(original), notes(working))
        self.assertEqual(self.path.read_bytes(), raw)

    def rewrite(self, transform, end=3107):
        midi = mido.MidiFile(self.path)
        rows = [(t, m) for t, _, m in _absolute(midi.tracks[1]) if m.type not in ('track_name', 'end_of_track')]
        midi.tracks[1] = track('Chords', transform(rows), end)
        midi.save(self.path)

    def test_real_tail_proportions_at_multiple_resolutions(self):
        for ppq in (96, 480, 960):
            for overrun in (ppq * 35 // 96, ppq):
                with self.subTest(ppq=ppq, overrun=overrun):
                    fixture(self.path, ppq=ppq, overrun=overrun)
                    self.assert_bars(8)

    def test_several_exact_packets_are_normalized(self):
        fixture(self.path, overrun=35)
        self.rewrite(lambda rows: rows + [(3090, m) for m in setup()])
        self.assert_bars(8)

    def test_late_musical_events_are_retained(self):
        events = [mido.Message('control_change', control=7, value=99),
                  mido.Message('control_change', control=11, value=127),
                  mido.Message('control_change', control=64, value=0),
                  mido.Message('control_change', control=66, value=0),
                  mido.Message('control_change', control=96, value=0),
                  mido.Message('control_change', control=121, value=0),
                  mido.Message('pitchwheel', pitch=100),
                  mido.Message('program_change', program=3),
                  mido.Message('aftertouch', value=20),
                  mido.Message('sysex', data=(1, 2, 3)),
                  mido.MetaMessage('marker', text='Real ending'),
                  mido.MetaMessage('midi_port', port=1)]
        for message in events:
            with self.subTest(message=message):
                fixture(self.path, overrun=35, extra=[(16, message)])
                self.assert_bars(9)

    def test_notes_and_note_tails_in_ninth_bar_are_retained(self):
        for channel in (0, 9):
            fixture(self.path, overrun=35, extra=[
                (16, mido.Message('note_on', channel=channel, note=50, velocity=90)),
                (25, mido.Message('note_off', channel=channel, note=50, velocity=20))])
            self.assert_bars(9)
        fixture(self.path, overrun=35)
        self.rewrite(lambda rows: [(3088 if m.type == 'note_off' else t, m) for t, m in rows])
        self.assert_bars(9)

    def test_held_sustain_and_sostenuto_preserve_ending(self):
        for control in (64, 66):
            fixture(self.path, overrun=35)
            self.rewrite(lambda rows: rows + [(96, mido.Message('control_change', control=control, value=127))])
            self.assert_bars(9)

    def test_released_pedal_allows_verified_tail(self):
        fixture(self.path, overrun=35)
        self.rewrite(lambda rows: rows + [
            (96, mido.Message('control_change', control=64, value=127)),
            (3070, mido.Message('control_change', control=64, value=0))])
        self.assert_bars(8)

    def test_only_identical_complete_boundary_packets_qualify(self):
        transforms = [
            lambda rows: [(t, m) for t, m in rows if t != 3072],
            lambda rows: [(t, m) for t, m in rows if not (t == 3107 and m.type == 'pitchwheel')],
            lambda rows: [(t, m.copy(value=99) if t == 3107 and m.type == 'control_change' and m.control == 7 else m) for t, m in rows],
            lambda rows: rows + [(3090, mido.MetaMessage('midi_port', port=1))],
        ]
        for transform in transforms:
            fixture(self.path, overrun=35)
            self.rewrite(transform)
            self.assert_bars(9)

    def test_longer_end_marker_is_not_setup_evidence(self):
        fixture(self.path, overrun=0)
        midi = mido.MidiFile(self.path)
        midi.tracks[0][-1].time += 35
        midi.save(self.path)
        self.assert_bars(9)

    def test_rest_after_last_setup_packet_is_preserved(self):
        fixture(self.path, overrun=35)
        midi = mido.MidiFile(self.path)
        midi.tracks[0][-1].time += 1
        midi.save(self.path)
        self.assert_bars(9)

    def test_over_one_beat_and_explicit_whole_bar_rest_are_preserved(self):
        for extra in (97, 384):
            fixture(self.path, overrun=extra)
            self.assert_bars(9)

    def test_32_bar_export_has_no_phantom_gaps(self):
        paths = [fixture(self.base / f'{label}.mid', overrun=35) for label in ('A', 'B', 'C')]
        raw = [p.read_bytes() for p in paths]
        result = export_dual(*paths, {}, {}, self.base / 'export', sequence='A B AC BC')
        folder = Path(result['folder'])
        self.assertEqual(result['song_bars'], 32)
        for name, original in zip(('A', 'B', 'Chord progression C'), raw):
            self.assertEqual((folder / f'{name}.mid').read_bytes(), original)
        for name in ('AC', 'BC'):
            variation = mido.MidiFile(folder / f'{name}.mid')
            self.assertEqual(max(sum(m.time for m in lane) for lane in variation.tracks), 3072)
            self.assertEqual(note_events(mido.MidiFile(paths[0]), 2), note_events(variation, 2))
        song = mido.MidiFile(folder / 'Song.mid')
        self.assertEqual(max(sum(m.time for m in lane) for lane in song.tracks), 12288)
        self.assertEqual([t for lane in song.tracks for t, _, m in _absolute(lane) if m.type == 'marker'],
                         [0, 3072, 6144, 9216])
        self.assertEqual([p.read_bytes() for p in paths], raw)


if __name__ == '__main__':
    unittest.main()
