"""Strict, loss-aware eight/16-bar MIDI import and standard MIDI export.

Musical note data stays on one shared source clock. Automation is a
separate note/velocity protocol and deliberately contains no controller data.
"""
from __future__ import annotations

from collections import Counter, defaultdict, deque
import hashlib
import heapq
import math
from pathlib import Path
import re
from typing import Iterable

import mido

from .midi_tail import verified_fl_setup_tail

from .model import (Arrangement, BAR, ControlChange, DRUM_ROLES, Note, PitchBend, PPQ,
                    Source, StudioError, Track)

SOURCE_TICKS = 8 * BAR
MAX_SOURCE_TICKS = 16 * BAR
TAIL_TOLERANCE = PPQ // 8
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_EVENTS = 250_000
MAX_TRACKS = 256
NOTE_END_CONTROLS = frozenset((120, 123, 124, 125, 126, 127))
# These commands cannot be summarized as unordered sound settings. Their full
# ordered messages, including repeated RPN/NRPN selectors and data operations,
# always live in Track.control_changes.
PROCEDURAL_CONTROLS = frozenset((6, 38, 64, 65, 66, 67, 68, 69,
                               96, 97, 98, 99, 100, 101, *range(120, 128)))

# Prefixes describe musical roles, never inferred from General MIDI programs.
_ALIASES = {
    'kick': ('kick', 'bass drum', 'bd'),
    'snare': ('snare', 'sd', 'rimshot', 'rim shot', 'rim'),
    'clap': ('clap', 'handclap'),
    'closed_hat': ('closed hat', 'closed hats', 'closed hihat', 'closed hi hat',
                   'hi hat', 'hihat', 'hi hats', 'hats', 'hat', 'chh'),
    'open_hat': ('open hat', 'open hats', 'open hihat', 'open hi hat', 'ohh'),
    'cymbal': ('cymbal', 'cymbals', 'crash', 'ride'),
    'shaker': ('shaker', 'shakers'),
    'tom': ('tom', 'toms'),
    'percussion': ('percussion', 'perc', 'tambourine', 'conga', 'bongo'),
    'bass': ('bass',),
    '808': ('808', '808 bass', 'bass 808', 'sub 808'),
    'sub': ('sub', 'sub bass', 'subbass'),
    'lead': ('lead', 'melody'),
    'chords': ('chords', 'chord', 'harmony'),
    'pad': ('pad', 'pads'),
    'arp': ('arp', 'arpeggio', 'arpeggiator'),
    'pluck': ('pluck', 'plucks'),
    'stab': ('stab', 'stabs'),
    'screech': ('screech',),
    'fx': ('fx', 'sfx', 'effect', 'effects', 'impact', 'downlifter', 'sweep'),
    'riser': ('riser', 'uplifter'),
    'vocal': ('vocal', 'vocals', 'vox', 'voice'),
    'skip': ('skip', 'ignore', 'mute'),
}
_PREFIXES = sorted(((alias, role) for role, aliases in _ALIASES.items()
                    for alias in aliases), key=lambda item: len(item[0]), reverse=True)


def detect_role(name: str) -> str:
    """Recognize explicit role prefixes, including e.g. Lead 2 - Piano."""
    text = re.sub(r'[^a-z0-9]+', ' ', name.casefold()).strip()
    # A common useful spelling is Bass - 808 or Bass 1 - 808.
    if re.match(r'^(?:bass|sub)\s+(?:\d+\s+)?808(?:\s|$)', text):
        return '808'
    for prefix, role in _PREFIXES:
        if text == prefix or text.startswith(prefix + ' '):
            return role
        # Also accept Lead1 and Pluck02, but not arbitrary words like bassoon.
        if re.match(re.escape(prefix) + r'\d+(?:\s|$)', text):
            return role
    return 'unassigned'


def _ticks(value: int, source_ppq: int) -> int:
    scaled = value * PPQ
    if scaled % source_ppq:
        raise StudioError('This MIDI uses timing finer than the supported 960 ticks per beat. '
                          'Export it at 960 PPQ so the script can preserve your timing exactly.')
    return scaled // source_ppq


def _instrument_role(name: str, notes: list[Note]) -> str:
    """Offer editable roles for common instrument names without adding notes."""
    text = re.sub(r'[^a-z0-9]+', ' ', name.casefold()).strip()
    if re.match(r'^(?:steel drums?|steel ?pan)(?:\s|$)', text):
        return 'lead'
    if re.match(r'^(?:drums?|drum ?kit)(?:\s|$)', text):
        return 'percussion'
    if re.match(r'^strings?(?:\d+|\s|$)', text):
        return 'pad'
    if re.match(r'^(?:guit(?:ar)?|synth)(?:\d+|\s|$)', text):
        onsets = defaultdict(int)
        for note in notes:
            onsets[note.start] += 1
        if onsets and sum(count > 1 for count in onsets.values()) >= len(onsets) / 2:
            return 'chords'
        return 'lead'
    return 'unassigned'


def _metadata_text(text: str) -> str:
    # SMF does not declare a text encoding. Accept modern UTF-8 names and the
    # legacy Latin-1 names read by mido, without changing ordinary ASCII names.
    try:
        return text.encode('latin1').decode('utf-8')
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def _layer_maps(midi: mido.MidiFile, warnings: list[str]) -> tuple[dict, set[int]]:
    """Read the user's exported lane order, never infer keys from channels."""
    descriptions = []
    for track in midi.tracks:
        name = next((_metadata_text(m.name) for m in track if m.type == 'track_name' and m.name), '')
        instrument = next((_metadata_text(m.name) for m in track if m.type == 'instrument_name' and m.name), '')
        label = name or instrument
        # Even an unmatched note-off must reach ordinary note validation.
        has_notes = any(m.type in ('note_on', 'note_off') for m in track)
        is_layer = bool(re.search(r'\blayer\b', label, re.IGNORECASE))
        descriptions.append((label, has_notes, is_layer))
    maps, references = {}, set()
    for index, (name, has_notes, is_layer) in enumerate(descriptions):
        if not is_layer:
            continue
        roles, names = {}, {}
        for child, (label, child_notes, child_layer) in enumerate(descriptions[index + 1:], index + 1):
            if child_notes or child_layer:
                break
            if not label:
                continue
            pitch = 60 + len(roles)
            if pitch > 127:
                raise StudioError(f'{name}: too many empty reference lanes. The Layer map starts at MIDI note 60 '
                                  'and cannot extend above MIDI note 127.')
            role = detect_role(label)
            if role not in DRUM_ROLES:
                role = 'percussion'
                warnings.append(f'{name}: "{label}" at MIDI note {pitch} uses generic percussion '
                                'because its drum name was not recognized.')
            roles[pitch], names[pitch] = role, label
            references.add(child)
        if has_notes and not roles:
            raise StudioError(f'{name}: no empty drum reference lanes were found. Export the named empty '
                              'drum lanes immediately after this Layer, in key order starting at C5 '
                              '(MIDI note 60).')
        maps[index] = (roles, names)
    return maps, references


def _source_duration(midi: mido.MidiFile, references: set[int], warnings: list[str]) -> tuple[int, bool]:
    """Retain declared rests, allowing only a tiny provably unchanged setup tail."""
    timeline = []
    source_events = []
    for owner, track in enumerate(midi.tracks):
        absolute = 0
        lane = []
        for index, message in enumerate(track):
            if not isinstance(message.time, int) or message.time < 0:
                raise StudioError('This MIDI contains invalid event timing.')
            absolute += message.time
            tick = _ticks(absolute, midi.ticks_per_beat)
            if owner not in references or message.is_meta:
                timeline.append((tick, owner, index, message))
                lane.append((tick, index, message))
        source_events.append(lane)
    timeline.sort(key=lambda event: event[:3])
    max_tick = max((event[0] for event in timeline), default=0)

    def duplicate_tail(boundary: int) -> bool:
        state, parameter_kind = {}, {}
        for tick, _, _, message in timeline:
            key, value, harmless = None, None, False
            if message.is_meta:
                if message.type == 'end_of_track':
                    harmless = True
                elif message.type in ('set_tempo', 'time_signature'):
                    key = (message.type,)
                    value = tuple((k, v) for k, v in message.dict().items() if k != 'time')
                    harmless = state.get(key) == value
            elif message.type == 'program_change':
                key, value = (message.type, message.channel), message.program
                harmless = state.get(key) == value
            elif message.type == 'pitchwheel':
                key, value = (message.type, message.channel), message.pitch
                harmless = state.get(key) == value
            elif message.type == 'control_change':
                channel, control = message.channel, message.control
                key, value = ('cc', channel, control), message.value
                # Only stable level/pan and the familiar pitch-range setup are
                # cleanup. Pedals, resets, relative data operations and actual
                # expression changes are never shortened.
                harmless = control in (7, 10, 6, 38, 100, 101) and state.get(key) == value
                if control in (6, 38):
                    harmless = (harmless and parameter_kind.get(channel) == 'rpn'
                                and state.get(('cc', channel, 100)) == 0
                                and state.get(('cc', channel, 101)) == 0)
                if control in (100, 101):
                    parameter_kind[channel] = 'rpn'
                elif control in (98, 99):
                    parameter_kind[channel] = 'nrpn'
            if tick > boundary and not harmless:
                return False
            if key is not None:
                state[key] = value
        return True

    for bars in (8, 16):
        boundary = bars * BAR
        if max_tick <= boundary:
            if max_tick < boundary:
                warnings.append(f'The file is shorter than {bars} bars; remaining time stays silent '
                                f'in the shared {bars}-bar loop.')
            return bars, False
        tiny_tail = max_tick <= boundary + TAIL_TOLERANCE
        extended_tail = (not tiny_tail and max_tick <= boundary + PPQ
                         and verified_fl_setup_tail(source_events, boundary, max_tick, PPQ))
        if (tiny_tail or extended_tail) and duplicate_tail(boundary):
            detail = ('Verified repeated FL Studio setup packets within one beat after '
                      if extended_tail else 'Duplicate setup and end markers within one eighth of a beat after ')
            warnings.append(detail + f'bar {bars} were moved to the {bars}-bar boundary. '
                            'All musical notes were preserved.')
            return bars, True
    raise StudioError('This MIDI extends beyond sixteen bars. Export an eight- or sixteen-bar selection '
                      'including its complete notes and expression; no musical events can be trimmed.')


def load_source(path: str | Path) -> Source:
    path = Path(path)
    try:
        size = path.stat().st_size
        if not 14 <= size <= MAX_FILE_BYTES:
            raise StudioError('Choose a valid MIDI file smaller than 16 MB.')
        data = path.read_bytes()
        if len(data) > MAX_FILE_BYTES:
            raise StudioError('Choose a valid MIDI file smaller than 16 MB.')
        # Read the same bytes that are fingerprinted; keep parser allocation bounded.
        import io
        midi = mido.MidiFile(file=io.BytesIO(data), clip=False)
    except StudioError:
        raise
    except (OSError, ValueError, EOFError, KeyError, IndexError, TypeError) as exc:
        raise StudioError(f'Could not read this MIDI file: {exc}') from exc
    if midi.type not in (0, 1):
        raise StudioError('Use a format 0 or format 1 MIDI file; format 2 has independent timelines.')
    if midi.ticks_per_beat <= 0:
        raise StudioError('SMPTE MIDI timing is unsupported. Export using beats/PPQ timing.')
    if len(midi.tracks) > MAX_TRACKS or sum(len(t) for t in midi.tracks) > MAX_EVENTS:
        raise StudioError('This MIDI is too large for a source loop. Export only your eight- or sixteen-bar idea.')

    warnings: list[str] = []
    layer_maps, references = _layer_maps(midi, warnings)
    source_bars, normalized_tail = _source_duration(midi, references, warnings)
    source_ticks = source_bars * BAR
    tempos: set[int] = set()
    time_signatures: set[tuple[int, int]] = set()
    channel_programs: dict[int, int] = {}
    channel_program_orders: dict[int, int] = {}
    channel_late_programs: set[int] = set()
    channel_controls: dict[int, dict[int, int]] = defaultdict(dict)
    channel_bends: dict[int, list[PitchBend]] = defaultdict(list)
    channel_changes: dict[int, list[ControlChange]] = defaultdict(list)
    # Stage channel events before pairing notes: controller-only tracks can
    # terminate notes in another lane, and SMF track order resolves equal ticks.
    events: list[tuple[int, int, int, mido.Message]] = []
    lanes: list[dict] = []
    imported: list[Track] = []
    max_tick = 0

    for track_index, raw_track in enumerate(midi.tracks):
        absolute = 0
        name = ''
        instrument_name = ''
        has_bends = False
        has_controls = False
        for event_index, message in enumerate(raw_track):
            if not isinstance(message.time, int) or message.time < 0:
                raise StudioError('This MIDI contains invalid event timing.')
            absolute += message.time
            tick = _ticks(absolute, midi.ticks_per_beat)
            if normalized_tail and tick > source_ticks:
                tick = source_ticks
            order = track_index * (MAX_EVENTS + 1) + event_index + 1
            max_tick = max(max_tick, tick)
            if message.is_meta:
                if message.type == 'track_name' and not name:
                    name = _metadata_text(message.name)
                elif message.type == 'instrument_name' and not instrument_name:
                    instrument_name = _metadata_text(message.name)
                elif message.type == 'set_tempo':
                    if message.tempo <= 0:
                        raise StudioError('The MIDI tempo must be greater than zero.')
                    if absolute and not tempos:
                        raise StudioError('The first tempo event must be at the start of the MIDI.')
                    tempos.add(message.tempo)
                elif message.type == 'time_signature':
                    time_signatures.add((message.numerator, message.denominator))
                continue
            if track_index in references:
                # These empty lanes identify keys only. Their instrument setup
                # must never leak onto a retained Layer sharing a MIDI channel.
                continue
            if message.type in ('aftertouch', 'polytouch', 'sysex'):
                raise StudioError(f'{name or "A track"} contains {message.type} expression. '
                                  'This version arranges notes, pitch bends, and MIDI controllers; export a '
                                  'note-only seed or bake the expression into your instrument first.')
            if message.type == 'pitchwheel':
                has_bends = True
            elif message.type == 'program_change':
                prior = channel_programs.get(message.channel)
                if prior is not None and prior != message.program:
                    raise StudioError('Changing or conflicting instrument programs share a MIDI channel. '
                                      'Use a separate named track and MIDI channel for each instrument.')
                channel_programs[message.channel] = message.program
                if tick == 0:
                    channel_program_orders.setdefault(message.channel, order)
                else:
                    channel_late_programs.add(message.channel)
            elif message.type == 'control_change':
                has_controls = True
            elif message.type not in ('note_on', 'note_off'):
                raise StudioError(f'Unsupported MIDI event: {message.type}. Export a standard note-only seed.')
            if message.type != 'program_change':
                events.append((tick, order, track_index, message))
        final_name = name or instrument_name or f'Track {track_index + 1}'
        lanes.append({'name': final_name, 'instrument_name': instrument_name,
                      'named': bool(name or instrument_name), 'has_bends': has_bends,
                      'has_controls': has_controls, 'notes': defaultdict(list),
                      'overlap': False, 'zero_mode_notes': False})

    # Keep each lane's note FIFO, including already-terminated entries. A later
    # explicit note-off may acknowledge a note released by a channel-mode CC.
    # It must not accidentally consume a newer note of the same pitch.
    active: dict[tuple[int, int, int], deque[list[int | bool]]] = defaultdict(deque)
    prior_channel_event: dict[int, tuple[int, str, int]] = {}
    remaining_ons = Counter((owner, event.channel, event.note) for _, _, owner, event in events
                            if event.type == 'note_on' and event.velocity > 0)
    remaining_offs = Counter((owner, event.channel, event.note) for _, _, owner, event in events
                             if event.type == 'note_off'
                             or (event.type == 'note_on' and event.velocity == 0))
    for tick, order, track_index, message in sorted(events, key=lambda event: event[:2]):
        channel = message.channel
        if message.type == 'pitchwheel':
            signature = (tick, message.type, message.pitch)
            if prior_channel_event.get(channel) != signature:
                channel_bends[channel].append(PitchBend(tick, message.pitch, order))
            prior_channel_event[channel] = signature
            continue
        prior_channel_event[channel] = (tick, message.type, order)
        if message.type == 'control_change':
            channel_changes[channel].append(ControlChange(tick, message.control, message.value, order))
            if tick == 0 and message.control not in PROCEDURAL_CONTROLS:
                channel_controls[channel][message.control] = message.value
            if message.control in NOTE_END_CONTROLS:
                for (owner, note_channel, pitch), queue in active.items():
                    if note_channel != channel:
                        continue
                    for record in queue:
                        start, velocity, start_order, terminated = record
                        if terminated:
                            continue
                        if tick > start:
                            lanes[owner]['notes'][channel].append(
                                Note(start, tick - start, pitch, velocity, start_order, order))
                        else:
                            lanes[owner]['zero_mode_notes'] = True
                        record[3] = True
            continue
        key = (track_index, channel, message.note)
        if message.type == 'note_on' and message.velocity > 0:
            remaining_ons[key] -= 1
            if any(not record[3] for record in active[key]):
                lanes[track_index]['overlap'] = True
            active[key].append([tick, message.velocity, order, False])
        else:
            remaining_offs[key] -= 1
            # Some files keep a redundant explicit off after a mode release;
            # others omit it and immediately reuse the same pitch. Reserve the
            # available offs for current/future note-ons before acknowledging
            # already-terminated FIFO entries.
            needed_offs = sum(not record[3] for record in active[key]) + remaining_ons[key]
            while (active[key] and active[key][0][3]
                   and remaining_offs[key] + 1 <= needed_offs):
                active[key].popleft()
            if not active[key]:
                raise StudioError(f'{lanes[track_index]["name"]} has a note-off without a matching note-on. '
                                  'Re-export the source selection with complete notes.')
            start, velocity, start_order, terminated = active[key].popleft()
            if terminated:
                continue
            if tick <= start:
                raise StudioError(f'{lanes[track_index]["name"]} contains a zero-length note. '
                                  'Give every note a positive length before exporting.')
            lanes[track_index]['notes'][channel].append(
                Note(start, tick - start, message.note, velocity, start_order, order))
    for (owner, _, _), queue in active.items():
        if any(not record[3] for record in queue):
            raise StudioError(f'{lanes[owner]["name"]} has notes with no ending. '
                              'Re-export a complete source selection with note-off events.')

    for track_index, lane in enumerate(lanes):
        if track_index in references:
            continue
        final_name = lane['name']
        instrument_name = lane['instrument_name']
        channel_notes = lane['notes']
        if lane['overlap']:
            warnings.append(f'{final_name}: overlapping notes of the same pitch were paired '
                            'first-in, first-out. Check this part against your original.')
        if lane['zero_mode_notes']:
            warnings.append(f'{final_name}: notes stopped by a channel command at their exact start '
                            'were omitted because they have no duration.')
        if not channel_notes:
            if lane['named']:
                if lane['has_controls']:
                    warnings.append(f'{final_name}: MIDI controllers applied to matching MIDI channels; '
                                    'this lane has no notes of its own.')
                elif lane['has_bends']:
                    warnings.append(f'{final_name}: pitch bends applied to matching MIDI channels; '
                                    'this lane has no notes of its own.')
                else:
                    warnings.append(f'{final_name}: empty note lane skipped.')
            continue
        drum_map, drum_names = layer_maps.get(track_index, ({}, {}))
        if drum_map:
            used = {note.pitch for notes in channel_notes.values() for note in notes}
            missing = sorted(used - drum_map.keys())
            if missing:
                pitches = ', '.join(str(pitch) for pitch in missing)
                raise StudioError(f'{final_name}: MIDI note(s) {pitches} have no matching empty drum lane. '
                                  'Keep every drum reference lane in key order immediately after the Layer, '
                                  'starting at C5 (MIDI note 60). The script will not shift your notes or guess sounds.')
            if len(channel_notes) != 1:
                raise StudioError(f'{final_name}: Layer notes use more than one MIDI channel. Export this '
                                  'combined Layer on one channel so its routing can be preserved.')
        role = 'percussion' if drum_map else detect_role(final_name)
        if role == 'unassigned' and instrument_name:
            role = detect_role(instrument_name)
        for channel, notes in sorted(channel_notes.items()):
            lane_role = role
            if lane_role == 'unassigned':
                lane_role = _instrument_role(final_name, notes)
                if lane_role == 'unassigned' and instrument_name:
                    lane_role = _instrument_role(instrument_name, notes)
            imported.append(Track(id=f't{track_index:03d}_c{channel:02d}', name=final_name,
                                  role=lane_role, channel=channel, instrument_name=instrument_name,
                                  notes=sorted(notes, key=lambda n: (n.start, n.pitch, n.end)),
                                  drum_map=dict(drum_map), drum_names=dict(drum_names)))
        if len(channel_notes) > 1:
            warnings.append(f'{final_name}: split into {len(channel_notes)} lanes by MIDI channel; '
                            'the original name is preserved on each lane.')

    if len(tempos) > 1:
        raise StudioError('Tempo changes are unsupported. Export your source idea at one fixed tempo.')
    if time_signatures - {(4, 4)}:
        raise StudioError('Only a constant 4/4 time signature is supported. Export an eight- or sixteen-bar 4/4 idea.')
    if not imported:
        raise StudioError('This MIDI contains no playable notes. Export the named instrument tracks.')
    if not tempos:
        warnings.append('No tempo was stored in the MIDI; using the MIDI standard default of 120 BPM.')
    for track in imported:
        track.program = channel_programs.get(track.channel)
        track.program_order = channel_program_orders.get(track.channel, 0)
        track.controls = list(channel_controls.get(track.channel, {}).items())
        track.pitch_bends = list(channel_bends.get(track.channel, ()))
        track.control_changes = list(channel_changes.get(track.channel, ()))
    for channel in sorted(channel_late_programs - channel_program_orders.keys()):
        warnings.append(f'MIDI channel {channel + 1}: its constant instrument program was only '
                        'stored after the start and is initialized at the start of the song.')
    return Source(path=str(path.resolve()), tracks=imported,
                  bpm=mido.tempo2bpm(next(iter(tempos), 500_000)), bars=source_bars,
                  warnings=warnings, sha256=hashlib.sha256(data).hexdigest())


def _tempo(bpm: float) -> int:
    if not isinstance(bpm, (int, float)) or not math.isfinite(bpm) or bpm <= 0:
        raise StudioError('Tempo must be a positive finite number.')
    tempo = mido.bpm2tempo(bpm)
    if not 1 <= tempo <= 0xFFFFFF:
        raise StudioError('Tempo is outside the standard MIDI range.')
    return tempo


def _conductor(midi: mido.MidiFile, bpm: float, total_ticks: int,
               markers: Iterable[tuple[int, str]] = ()) -> None:
    if not isinstance(total_ticks, int) or total_ticks < 0:
        raise StudioError('Song length must be a non-negative whole number of ticks.')
    track = mido.MidiTrack()
    midi.tracks.append(track)
    track.append(mido.MetaMessage('track_name', name='Song Timeline', time=0))
    track.append(mido.MetaMessage('set_tempo', tempo=_tempo(bpm), time=0))
    track.append(mido.MetaMessage('time_signature', numerator=4, denominator=4, time=0))
    last = 0
    for tick, name in sorted(markers):
        if not last <= tick <= total_ticks:
            raise StudioError('Section marker falls outside the song.')
        track.append(mido.MetaMessage('marker', text=name, time=tick - last))
        last = tick
    track.append(mido.MetaMessage('end_of_track', time=total_ticks - last))


def _note_track(name: str, instrument_name: str, channel: int,
                notes: Iterable[Note], total_ticks: int, program: int | None = None,
                controls: Iterable[tuple[int, int]] = (),
                pitch_bends: Iterable[PitchBend] = (),
                control_changes: Iterable[ControlChange] = (),
                program_order: int = 0) -> mido.MidiTrack:
    if not isinstance(channel, int) or not 0 <= channel <= 15:
        raise StudioError('MIDI channels must be between 0 and 15 internally.')
    track = mido.MidiTrack()
    track.append(mido.MetaMessage('track_name', name=name, time=0))
    track.append(mido.MetaMessage('instrument_name', name=instrument_name or name, time=0))
    changes = tuple(control_changes)
    timed_controls = {change.control for change in changes}
    for control, value in controls:
        if control in timed_controls:
            continue
        track.append(mido.Message('control_change', channel=channel, control=control, value=value, time=0))
    if program is not None and (type(program) is not int or not 0 <= program <= 127):
        raise StudioError('Instrument program must be between 0 and 127.')
    # A small heap of pending note ends avoids doubling all notes into an event list.
    pending: list[tuple[tuple[int, int, int], int, int]] = []
    last_event = 0
    last_start = None
    bend_iterator = iter(pitch_bends)
    last_bend_key = None
    change_iterator = iter(changes)
    last_change_key = None

    def event_key(tick: int, order: int, priority: int) -> tuple[int, int, int]:
        if type(order) is not int:
            raise StudioError('MIDI event order must be a whole number.')
        # Original event ordinals also preserve CC121/bend interactions and
        # pedal changes on the same tick as a note. Zero keeps the legacy order:
        # note-off, controller, pitch bend, note-on. Synthetic order may be negative.
        return tick, order, priority

    def next_bend() -> PitchBend | None:
        nonlocal last_bend_key
        bend = next(bend_iterator, None)
        if bend is not None:
            if (type(bend.start) is not int or type(bend.value) is not int
                    or not 0 <= bend.start <= total_ticks
                    or not -8192 <= bend.value <= 8191):
                raise StudioError('A generated pitch bend has invalid timing or value.')
            key = event_key(bend.start, bend.order, 2)
            if last_bend_key is not None and key < last_bend_key:
                raise StudioError('Pitch bends must be supplied in chronological order for MIDI export.')
            last_bend_key = key
        return bend

    def next_change() -> ControlChange | None:
        nonlocal last_change_key
        change = next(change_iterator, None)
        if change is not None:
            if (not all(type(value) is int for value in (change.start, change.control, change.value))
                    or not 0 <= change.start <= total_ticks
                    or not 0 <= change.control <= 127 or not 0 <= change.value <= 127):
                raise StudioError('A generated MIDI controller has invalid timing, controller, or value.')
            key = event_key(change.start, change.order, 1)
            if last_change_key is not None and key < last_change_key:
                raise StudioError('MIDI controllers must be supplied in chronological order for MIDI export.')
            last_change_key = key
        return change

    bend = next_bend()
    change = next_change()
    pending_program = program

    def emit_until(limit: tuple[int, int, int]) -> None:
        nonlocal last_event, bend, change, pending_program
        # Three small streams merge with the pending note-end heap, keeping
        # dense note-only automation streaming instead of doubling its events.
        while True:
            candidates = []
            if pending:
                candidates.append((pending[0][0], 'off'))
            if change is not None:
                candidates.append((event_key(change.start, change.order, 1), 'cc'))
            if bend is not None:
                candidates.append((event_key(bend.start, bend.order, 2), 'bend'))
            if pending_program is not None:
                candidates.append((event_key(0, program_order, 1), 'program'))
            if not candidates:
                break
            key, kind = min(candidates)
            if key > limit:
                break
            if kind == 'off':
                key, pitch, _ = heapq.heappop(pending)
                end = key[0]
                track.append(mido.Message('note_off', channel=channel, note=pitch,
                                          velocity=0, time=end - last_event))
                last_event = end
            elif kind == 'cc':
                track.append(mido.Message('control_change', channel=channel,
                                          control=change.control, value=change.value,
                                          time=change.start - last_event))
                last_event = change.start
                change = next_change()
            elif kind == 'bend':
                track.append(mido.Message('pitchwheel', channel=channel, pitch=bend.value,
                                          time=bend.start - last_event))
                last_event = bend.start
                bend = next_bend()
            elif kind == 'program':
                track.append(mido.Message('program_change', channel=channel,
                                          program=pending_program, time=-last_event))
                last_event = 0
                pending_program = None

    for sequence, note in enumerate(notes):
        if (not all(isinstance(v, int) for v in (note.start, note.duration, note.pitch, note.velocity))
                or note.start < 0 or note.duration <= 0 or note.end > total_ticks
                or not 0 <= note.pitch <= 127 or not 1 <= note.velocity <= 127):
            raise StudioError('A generated note has invalid timing, pitch, or velocity.')
        key = event_key(note.start, note.start_order, 3)
        end_key = event_key(note.end, note.end_order, 0)
        if last_start is not None and key < last_start:
            raise StudioError('Notes must be supplied in chronological order for MIDI export.')
        last_start = key
        emit_until(key)
        track.append(mido.Message('note_on', channel=channel, note=note.pitch,
                                  velocity=note.velocity, time=note.start - last_event))
        last_event = note.start
        heapq.heappush(pending, (end_key, note.pitch, sequence))
    emit_until((total_ticks + 1, 0, 0))
    track.append(mido.MetaMessage('end_of_track', time=total_ticks - last_event))
    return track


def _save(midi: mido.MidiFile, path: str | Path) -> None:
    try:
        midi.save(str(path))
    except (OSError, ValueError, UnicodeError) as exc:
        raise StudioError(f'Could not save MIDI: {exc}') from exc


def write_music(path: str | Path, arrangement: Arrangement,
                tracks: list[Track] | None = None) -> None:
    """Export arranged parts, names, static sound settings, and pitch bends."""
    midi = mido.MidiFile(type=1, ticks_per_beat=PPQ, charset='utf-8')
    total_ticks = arrangement.bars * BAR
    _conductor(midi, arrangement.bpm, total_ticks,
               ((section.start_bar * BAR, section.name) for section in arrangement.sections))
    for track in arrangement.tracks if tracks is None else tracks:
        midi.tracks.append(_note_track(track.name, track.instrument_name, track.channel,
                                      sorted(track.notes, key=lambda n: (n.start, n.start_order, n.pitch, n.end)),
                                      total_ticks, track.program, track.controls, track.pitch_bends,
                                      track.control_changes, track.program_order))
    _save(midi, path)


def write_automation(path: str | Path, name: str, channel: int,
                     notes: Iterable[Note], total_ticks: int, bpm: float) -> None:
    """Write chronological automation notes only; pitch is section, velocity value.

    Iterables are consumed once in chronological order. No controllers, program
    changes, or pitch bend can enter this dedicated automation export.
    """
    midi = mido.MidiFile(type=1, ticks_per_beat=PPQ, charset='utf-8')
    _conductor(midi, bpm, total_ticks)
    midi.tracks.append(_note_track(name, name, channel, notes, total_ticks))
    _save(midi, path)
