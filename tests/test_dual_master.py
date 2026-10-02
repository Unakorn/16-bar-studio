from pathlib import Path
import hashlib
import sys
import tempfile
import unittest

import mido

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from eightbar.dual_master import inspect_midi, export_dual, StudioError


def lane(name, events, end):
    track = mido.MidiTrack([mido.MetaMessage('track_name', name=name)])
    tick = 0
    for when, msg in sorted(events, key=lambda item: item[0]):
        track.append(msg.copy(time=when-tick))
        tick = when
    track.append(mido.MetaMessage('end_of_track', time=end-tick))
    return track


def note(events, start, end, pitch, channel, velocity=83, off_velocity=17):
    events.append((start, mido.Message('note_on', channel=channel, note=pitch, velocity=velocity)))
    events.append((end, mido.Message('note_off', channel=channel, note=pitch, velocity=off_velocity)))


def fixture(path, contrast=False, ppq=960):
    midi = mido.MidiFile(type=1, ticks_per_beat=ppq)
    duration = ppq*8
    midi.tracks.append(lane('Conductor', [(0, mido.MetaMessage('set_tempo', tempo=500000))], duration))
    melody = [(0, mido.Message('program_change', channel=0, program=80)),
              (0, mido.Message('control_change', channel=0, control=11, value=90)),
              (ppq//2, mido.Message('pitchwheel', channel=0, pitch=111)),
              (ppq*2, mido.Message('control_change', channel=0, control=11, value=101))]
    note(melody, ppq//2 if contrast else 0, ppq*2, 72 if contrast else 64, 0)
    note(melody, ppq*3, ppq*4, 67 if contrast else 72, 0, 97, 41)
    midi.tracks.append(lane('Lead', melody, duration))
    chords = [(0, mido.Message('program_change', channel=1, program=48))]
    for pitch in (60,64,67): note(chords, 0, duration, pitch, 1)
    midi.tracks.append(lane('Chords', chords, duration))
    drums = [(0, mido.Message('control_change', channel=9, control=10, value=40))]
    note(drums, ppq//8, ppq//4, 36, 9, 76, 55)
    note(drums, ppq*2, ppq*2+ppq//8, 42, 9, 63, 30)
    midi.tracks.append(lane('Kit', drums, duration))
    layer = [(0,mido.Message('program_change', channel=3, program=1))]
    note(layer, ppq//3, ppq, 60, 3, 57, 8)
    midi.tracks.append(lane('Layer', layer, duration))
    midi.tracks.append(lane('My kick', [], duration))
    midi.tracks.append(lane('Odd custom sound', [], duration))
    kept = []
    note(kept, ppq, ppq*2, 78, 4)
    midi.tracks.append(lane('Unchanged texture', kept, duration))
    midi.save(path)


def progression(path):
    midi = mido.MidiFile(type=1, ticks_per_beat=480)
    events=[]
    for pitch in (62,65,69): note(events,0,480,pitch,0,88,33)
    for pitch in (67,71,74): note(events,960,1440,pitch,0,91,34)
    midi.tracks.append(lane('Chords',events,1920))
    # This must never join the replacement chords.
    drums=[]
    note(drums,0,120,36,9)
    midi.tracks.append(lane('Kit',drums,1920))
    midi.save(path)


def events(track):
    absolute=0
    result=[]
    for msg in track:
        absolute+=msg.time
        data=msg.dict()
        data.pop('time')
        result.append((absolute,data))
    return result


class DualTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.a=self.root/'input-a.mid';self.b=self.root/'input-b.mid';self.c=self.root/'input-c.mid'
        fixture(self.a);fixture(self.b,contrast=True);progression(self.c)
        self.roles={7:'Keep'}

    def tearDown(self): self.temp.cleanup()

    def export(self):
        return export_dual(self.a,self.b,self.c,self.roles,self.roles,self.root/'results')

    def test_four_patterns_preserve_sources_and_drums_and_controls(self):
        original={p:p.read_bytes() for p in (self.a,self.b,self.c)}
        info=inspect_midi(self.a)
        self.assertEqual(info['bars'],2)
        self.assertEqual(info['tracks'][4]['role'],'Drums')
        result=self.export();out=Path(result['folder'])
        self.assertEqual((out/'A.mid').read_bytes(),original[self.a])
        self.assertEqual((out/'B.mid').read_bytes(),original[self.b])
        for path,value in original.items(): self.assertEqual(path.read_bytes(),value)
        for master,label in ((self.a,'AC'),(self.b,'BC')):
            before=mido.MidiFile(master);after=mido.MidiFile(out/f'{label}.mid')
            for index in (0,3,4,5,6,7): self.assertEqual(events(before.tracks[index]),events(after.tracks[index]))
            bn=[(t,d) for t,d in events(before.tracks[1]) if d['type'] not in ('note_on','note_off')]
            an=[(t,d) for t,d in events(after.tracks[1]) if d['type'] not in ('note_on','note_off')]
            self.assertEqual(bn,an)
            bn=[(t,{k:v for k,v in d.items() if k!='note'}) for t,d in events(before.tracks[1]) if d['type'] in ('note_on','note_off')]
            an=[(t,{k:v for k,v in d.items() if k!='note'}) for t,d in events(after.tracks[1]) if d['type'] in ('note_on','note_off')]
            self.assertEqual(bn,an)
        self.assertNotEqual(events(mido.MidiFile(out/'AC.mid').tracks[1]),events(mido.MidiFile(out/'BC.mid').tracks[1]))
        self.assertTrue((out/'Song.mid').is_file())

    def test_chord_replacement_repeats_ppq_and_preserves_rests(self):
        out=Path(self.export()['folder']);ac=mido.MidiFile(out/'AC.mid')
        notes=[(t,d) for t,d in events(ac.tracks[2]) if d['type'] in ('note_on','note_off')]
        attacks=[(t,d['note']) for t,d in notes if d['type']=='note_on']
        self.assertEqual(attacks,[(t,p) for t,pitches in ((0,(62,65,69)),(1920,(67,71,74)),(3840,(62,65,69)),(5760,(67,71,74))) for p in pitches])
        self.assertTrue(all(d['channel']==1 for _,d in notes))
        self.assertTrue(all(d['velocity'] in (33,34) for _,d in notes if d['type']=='note_off'))
        self.assertNotIn(36,[d['note'] for _,d in notes])

    def test_channel_ten_in_mixed_instrument_track_is_protected(self):
        midi=mido.MidiFile(self.a)
        mixed=[]
        note(mixed,0,1000,36,9,83,21)
        note(mixed,0,1000,64,0,91,30)
        midi.tracks[1]=lane('Lead',mixed,7680);midi.save(self.a)
        out=Path(self.export()['folder'])
        expected=[(t,d) for t,d in events(midi.tracks[1]) if d.get('channel')==9]
        actual=[(t,d) for t,d in events(mido.MidiFile(out/'AC.mid').tracks[1]) if d.get('channel')==9]
        self.assertEqual(expected,actual)

    def test_overlapping_same_pitch_notes_get_matching_note_offs(self):
        midi=mido.MidiFile(self.a);melody=[]
        note(melody,0,2000,64,0,80,41);note(melody,500,2500,64,0,90,51)
        midi.tracks[1]=lane('Lead',melody,7680);midi.save(self.a)
        out=Path(self.export()['folder']);active={}
        for tick,data in events(mido.MidiFile(out/'AC.mid').tracks[1]):
            if data['type']=='note_on':
                self.assertNotIn(data['note'],active);active[data['note']]=(tick,data['velocity'])
            elif data['type']=='note_off':
                self.assertIn(data['note'],active);active.pop(data['note'])
        self.assertEqual(active,{})

    def test_missing_chords_and_type_two_rejected(self):
        with self.assertRaisesRegex(StudioError,'Master A'):
            export_dual(self.a,self.b,self.c,{2:'Instrument'},self.roles,self.root/'bad')
        midi=mido.MidiFile(self.a);midi.type=2;midi.save(self.a)
        with self.assertRaisesRegex(StudioError,'format 2'): inspect_midi(self.a)

    def test_different_master_names_keep_core_and_song_exports(self):
        midi=mido.MidiFile(self.b);midi.tracks[1].name='Different lead';midi.save(self.b)
        result=self.export();folder=Path(result['folder'])
        self.assertTrue((folder/'Song.mid').exists())
        self.assertTrue(all((folder/f'{label}.mid').exists() for label in ('A','B','AC','BC')))
        self.assertTrue(any('colliding instrument' in warning for warning in result['warnings']))

    def test_shared_pitched_channels_get_safe_song_routes(self):
        for path in (self.a,self.b):
            midi=mido.MidiFile(path)
            for message in midi.tracks[2]:
                if hasattr(message,'channel'):message.channel=0
            midi.save(path)
        result=self.export();folder=Path(result['folder'])
        self.assertTrue((folder/'Song.mid').exists())
        self.assertTrue((folder/'AC.mid').exists())
        self.assertTrue(any('colliding instrument' in warning for warning in result['warnings']))

    def test_explicit_port_routing_preserved_in_patterns(self):
        midi=mido.MidiFile(self.a)
        midi.tracks[1].insert(1,mido.MetaMessage('midi_port',port=3,time=0))
        midi.save(self.a)
        result=self.export();folder=Path(result['folder'])
        self.assertTrue((folder/'Song.mid').exists())
        after=mido.MidiFile(folder/'AC.mid')
        self.assertEqual(next(message.port for message in after.tracks[1] if message.type=='midi_port'),3)
        self.assertTrue(any(message.type=='midi_port' and message.port==3
                            for track in mido.MidiFile(folder/'Song.mid').tracks for message in track))

    def test_common_non_gm_drum_names_are_detected(self):
        for name in ('Perc','Kick1','Snare02','OH','CHH','OHH','BD','Closed_Hat'):
            midi=mido.MidiFile(self.a);midi.tracks[4].name=name;midi.save(self.a)
            self.assertEqual(inspect_midi(self.a)['tracks'][4]['role'],'Drums',name)


if __name__=='__main__':unittest.main(verbosity=2)
