"""Independent integration review for the two-master MIDI workflow."""
from pathlib import Path
import json
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import mido

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from eightbar import dual_master as engine

RUN_UI = '--ui' in sys.argv
if RUN_UI:
    sys.argv.remove('--ui')


def track(name, events, end=3840):
    result = mido.MidiTrack([mido.MetaMessage('track_name', name=name)])
    previous = 0
    for tick, msg in sorted(events, key=lambda item: item[0]):
        result.append(msg.copy(time=tick - previous))
        previous = tick
    result.append(mido.MetaMessage('end_of_track', time=end - previous))
    return result


def notes(pitches, channel, start=0, end=960, velocity=80):
    return ([(start, mido.Message('note_on', channel=channel, note=p, velocity=velocity)) for p in pitches]
            + [(end, mido.Message('note_off', channel=channel, note=p, velocity=42)) for p in pitches])


def events(midi, lane):
    result = []
    absolute = 0
    for msg in midi.tracks[lane]:
        absolute += msg.time
        result.append((absolute, msg.copy(time=0)))
    return result


class Review(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.paths = []
        for label in ('A', 'B'):
            midi = mido.MidiFile(ticks_per_beat=480)
            midi.tracks.append(track('Timing', [(0, mido.MetaMessage('set_tempo', tempo=500000))]))
            midi.tracks.append(track('Chords Piano', notes([60, 64, 67], 0, end=3840)))
            synth = notes([72 if label == 'A' else 76], 1, start=120, end=600)
            synth += [(0, mido.Message('control_change', channel=1, control=7, value=99)),
                      (150, mido.Message('control_change', channel=1, control=1, value=35)),
                      (160, mido.Message('pitchwheel', channel=1, pitch=100)),
                      (180, mido.Message('polytouch', channel=1, note=72 if label == 'A' else 76, value=55))]
            if label == 'A':
                synth.append((200, mido.Message('aftertouch', channel=1, value=127)))
            midi.tracks.append(track('Lead Synth', synth))
            midi.tracks.append(track('Layer', notes([60, 62], 2, start=30, end=80)
                                     + [(60, mido.Message('pitchwheel', channel=2, pitch=123))]))
            midi.tracks.append(track('Kick', []))
            midi.tracks.append(track('Snare', []))
            midi.tracks.append(track('Mixed Instrument', notes([80], 3, start=900, end=1100)
                                     + notes([42], 9, start=910, end=950)))
            path = self.base / f'{label}.mid'
            midi.save(path)
            self.paths.append(path)
        progression = mido.MidiFile(ticks_per_beat=96)
        progression.tracks.append(track('New harmony', notes([62, 65, 69], 0, start=48, end=288), end=384))
        path = self.base / 'C.mid'
        progression.save(path)
        self.paths.append(path)

    def tearDown(self):
        self.temp.cleanup()

    def export(self):
        return engine.export_dual(*self.paths, {}, {}, self.base / 'results')

    def test_original_bytes_drums_controls_and_chord_replacement(self):
        result = self.export()
        folder = Path(result['folder'])
        for label, source_path in zip(('A', 'B'), self.paths):
            self.assertEqual(source_path.read_bytes(), (folder / f'{label}.mid').read_bytes())
            source = mido.MidiFile(source_path)
            variation = mido.MidiFile(folder / f'{label}C.mid')
            for index in (3, 4, 5):
                self.assertEqual(events(source, index), events(variation, index))
            for index in (1, 2, 6):
                before = [(tick, msg) for tick, msg in events(source, index)
                          if msg.type not in ('note_on', 'note_off', 'polytouch', 'end_of_track')]
                after = [(tick, msg) for tick, msg in events(variation, index)
                         if msg.type not in ('note_on', 'note_off', 'polytouch', 'end_of_track')]
                self.assertEqual(before, after)
            gm_before = [(tick, msg) for tick, msg in events(source, 6) if getattr(msg, 'channel', None) == 9]
            gm_after = [(tick, msg) for tick, msg in events(variation, 6) if getattr(msg, 'channel', None) == 9]
            self.assertEqual(gm_before, gm_after)
            for index in (2, 6):
                before = [(tick, msg.type, msg.velocity, msg.channel)
                          for tick, msg in events(source, index) if msg.type in ('note_on', 'note_off')]
                after = [(tick, msg.type, msg.velocity, msg.channel)
                         for tick, msg in events(variation, index) if msg.type in ('note_on', 'note_off')]
                self.assertEqual(before, after)
            replacement_on = [(tick, msg.note, msg.velocity) for tick, msg in events(variation, 1)
                              if msg.type == 'note_on' and msg.velocity]
            self.assertEqual(replacement_on, [(tick, pitch, 80) for tick in (240, 2160) for pitch in (62, 65, 69)])
            retuned = [(tick, msg.note) for tick, msg in events(variation, 2) if msg.type == 'note_on' and msg.velocity]
            pressure = [(tick, msg.note) for tick, msg in events(variation, 2) if msg.type == 'polytouch']
            self.assertEqual(retuned[0][1], pressure[0][1])

    def test_song_resets_channel_pressure_at_section_boundary(self):
        folder = Path(self.export()['folder'])
        song = mido.MidiFile(folder / 'Song.mid')
        boundary = 3840
        pressure_at_boundary = [msg.value for index in range(len(song.tracks))
                                for tick, msg in events(song, index)
                                if tick == boundary and msg.type == 'aftertouch' and msg.channel == 1]
        self.assertIn(0, pressure_at_boundary,
                      'A channel pressure leaks into B unless reset before B notes.')

    def test_song_does_not_kill_new_shared_channel_note_at_boundary(self):
        for label, path in zip(('A', 'B'), self.paths):
            midi = mido.MidiFile(ticks_per_beat=480)
            midi.tracks.append(track('Chords', notes([60, 64, 67] if label == 'A' else [62, 65, 69], 0, end=3840)))
            midi.tracks.append(track('Lead', notes([62] if label == 'A' else [64], 0, end=3840)))
            midi.save(path)
        result = engine.export_dual(*self.paths, {}, {}, self.base / 'shared-results', sequence='A B')
        song_path = Path(result['folder']) / 'Song.mid'
        self.assertTrue(song_path.exists(), 'A complete Song is required for shared source channels.')
        song = mido.MidiFile(song_path)
        relevant = []
        for lane_index in range(len(song.tracks)):
            port = 0
            for tick, msg in events(song, lane_index):
                if msg.type == 'midi_port':
                    port = msg.port
                if tick == 3840 and msg.type in ('note_on', 'note_off') and msg.note == 62:
                    relevant.append((port, msg.channel, msg.type))
        self.assertEqual(sorted(item[2] for item in relevant), ['note_off', 'note_on'])
        self.assertNotEqual(relevant[0][:2], relevant[1][:2],
                            'Separate instruments sharing a source channel need independent Song routes.')

    def test_non_gm_numbered_drums_and_perc_are_detected(self):
        midi = mido.MidiFile(ticks_per_beat=480)
        for name in ('Kick1', 'Snare02', 'Perc', 'CHH', 'OHH'):
            midi.tracks.append(track(name, notes([60], 0, end=120)))
        path = self.base / 'Numbered drums.mid'
        midi.save(path)
        info = engine.inspect_midi(path)
        self.assertEqual([lane['role'] for lane in info['tracks']], ['Drums'] * 5)

    def test_song_preserves_explicit_port_routing(self):
        for path in self.paths[:2]:
            midi = mido.MidiFile(path)
            midi.tracks[2].insert(0, mido.MetaMessage('midi_port', port=3))
            midi.save(path)
        result = self.export()
        song_path = Path(result['folder']) / 'Song.mid'
        self.assertTrue(song_path.exists(), 'A complete Song is required for explicit routing.')
        song = mido.MidiFile(song_path)
        synth = next(lane for lane in song.tracks if lane.name == 'Lead Synth')
        self.assertTrue(any(msg.type == 'midi_port' and msg.port == 3 for msg in synth),
                        'The combined MIDI silently discards the instrument output port.')

    @unittest.skipUnless(RUN_UI, 'Run with --ui to exercise Tk widgets.')
    def test_ui_loading_export_and_minimum_geometry(self):
        import tkinter as tk
        from eightbar.dual_ui import DualStudio
        root = tk.Tk()
        root.withdraw()
        try:
            studio = DualStudio(root)
            results = []
            errors = []
            studio.show_result = results.append
            studio.destination.set(str(self.base / 'ui-results'))
            with patch('eightbar.dual_ui.messagebox.showerror', side_effect=lambda *a, **k: errors.append(a)):
                for label, path in zip(('A', 'B', 'C'), self.paths):
                    self.assertTrue(studio.load_file(label, path))
                self.assertEqual(studio.roles['A'][3], 'Drums')
                self.assertNotIn('disabled', studio.export_button.state())
                root.geometry('940x780')
                root.attributes('-alpha', 0)
                root.deiconify()
                root.update()
                for key, widget in [('Export', studio.export_button), ('Results', studio.open_button),
                                    ('Master A', studio.tables['A']), ('Master B', studio.tables['B'])]:
                    right = widget.winfo_rootx() - root.winfo_rootx() + widget.winfo_width()
                    bottom = widget.winfo_rooty() - root.winfo_rooty() + widget.winfo_height()
                    self.assertLessEqual(right, 940, key)
                    self.assertLessEqual(bottom, 780, key)
                    self.assertGreater(widget.winfo_width(), 10, key)
                    self.assertGreater(widget.winfo_height(), 10, key)
                studio.export()
                self.assertTrue(studio.busy)
                deadline = time.monotonic() + 20
                while studio.busy and time.monotonic() < deadline:
                    root.update()
                    time.sleep(0.01)
                self.assertFalse(studio.busy)
                self.assertFalse(errors)
                self.assertEqual(len(results), 1)
                self.assertTrue((studio.latest / 'AC.mid').is_file())
                self.assertNotIn('disabled', studio.export_button.state())
                self.assertNotIn('disabled', studio.open_button.state())
        finally:
            root.destroy()


if __name__ == '__main__':
    unittest.main(verbosity=2)
