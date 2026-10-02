"""Two imported performances, one shared replacement chord progression.

Original patterns remain event-preserving. Validated AI JSON can arrange them
into full songs with melodic entrances and drum-reactive dynamics.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
import bisect
import hashlib
import io
import json
import math
import re
import uuid
from datetime import datetime

import mido

from .midi_tail import verified_fl_setup_tail


class StudioError(ValueError):
    pass


ROLES = ('Instrument', 'Chords', 'Drums', 'Keep')
_DRUMS = re.compile(r'\b(kicks?|snares?|claps?|hats?|hihats?|hi[ -]?hats?|shakers?|toms?|cymbals?|percussion|perc|drums?|drumkit|rimshots?|rides?|crashes?|congas?|bongos?|bd|sd|hh|chh|ohh|oh)\d*\b', re.I)
_CHORDS = re.compile(r'\b(chords?|harmony|harmonies)\b', re.I)


@dataclass
class _Note:
    track: int
    channel: int
    pitch: int
    start: int
    end: int
    velocity: int
    on: int
    off: int
    off_message: object


@dataclass
class _Document:
    path: Path
    raw: bytes
    midi: object
    events: list
    notes: list
    bars: int
    duration: int
    bpm: float
    roles: list
    warnings: list


def _absolute(track):
    tick = 0
    result = []
    for index, message in enumerate(track):
        if type(message.time) is not int or message.time < 0:
            raise StudioError('MIDI event times must be nonnegative whole ticks.')
        tick += message.time
        result.append((tick, index, message))
    return result


def _normalize_setup_tail(midi, events, warnings):
    """Move only a verified, unchanged exporter tail to a whole-bar boundary.

    FL Studio can repeat channel setup a few ticks after the selected loop.
    Notes, expressive events and explicitly longer rests still determine length.
    The loaded MIDI is a working copy; the original file bytes stay untouched.
    """
    duration = max((tick for lane in events for tick, _, _ in lane), default=0)
    bar = 4 * midi.ticks_per_beat
    boundary = duration // bar * bar
    if not boundary or not 0 < duration - boundary <= midi.ticks_per_beat:
        return events, duration
    extended = duration - boundary > midi.ticks_per_beat / 8
    if extended and not verified_fl_setup_tail(events, boundary, duration, midi.ticks_per_beat):
        return events, duration

    timeline = []
    for owner, lane in enumerate(events):
        port = 0
        for tick, index, message in lane:
            if message.type == 'midi_port':
                port = message.port
            timeline.append((tick, owner, index, port, message))
    state, parameter_kind = {}, {}
    for tick, _, _, port, message in sorted(timeline, key=lambda item: item[:3]):
        key, value, harmless = None, None, False
        route = (port, getattr(message, 'channel', None))
        if message.is_meta:
            if message.type == 'end_of_track':
                harmless = True
            elif message.type in ('set_tempo', 'time_signature'):
                key = (message.type,)
                value = tuple((k, v) for k, v in message.dict().items() if k != 'time')
                harmless = state.get(key) == value
        elif message.type in ('program_change', 'pitchwheel'):
            key = (message.type, *route)
            value = message.program if message.type == 'program_change' else message.pitch
            harmless = state.get(key) == value
        elif message.type == 'control_change':
            control = message.control
            key, value = ('cc', *route, control), message.value
            harmless = control in (7, 10, 100, 101) and state.get(key) == value
            if control in (6, 38):
                # Data entry is scoped to the selected parameter, not just CC6.
                msb = state.get(('cc', *route, 101))
                lsb = state.get(('cc', *route, 100))
                key = ('data', *route, parameter_kind.get(route), msb, lsb, control)
                harmless = (parameter_kind.get(route) == 'rpn' and (msb, lsb) == (0, 0)
                            and state.get(key) == value)
            elif control in (100, 101):
                harmless = harmless and parameter_kind.get(route) == 'rpn'
                parameter_kind[route] = 'rpn'
            elif control in (98, 99):
                parameter_kind[route] = 'nrpn'
            elif control in (96, 97) or control >= 120:
                # Relative parameter changes and resets invalidate known state.
                state = {k: v for k, v in state.items() if k[1:3] != route}
                parameter_kind.pop(route, None)
        elif message.type == 'sysex':
            # Opaque device setup may change any channel state on this port.
            state = {k: v for k, v in state.items() if len(k) < 3 or k[1] != port}
            parameter_kind = {k: v for k, v in parameter_kind.items() if k[0] != port}
        if tick > boundary and not harmless:
            return events, duration
        if key is not None:
            state[key] = value

    normalized = []
    for owner, lane in enumerate(events):
        track, adjusted, previous = mido.MidiTrack(), [], 0
        for tick, index, message in lane:
            tick = min(tick, boundary)
            copied = message.copy(time=tick - previous)
            track.append(copied)
            adjusted.append((tick, index, copied))
            previous = tick
        midi.tracks[owner] = track
        normalized.append(adjusted)
    detail = ('Verified repeated FL Studio setup packets within one beat after '
              if extended else 'Duplicate setup and end markers within one eighth of a beat after ')
    warnings.append(detail + f'bar {boundary // bar} were moved to its boundary in generated MIDI. '
                    'All musical notes and original input files were preserved.')
    return normalized, boundary


def _read(path):
    path = Path(path).expanduser().resolve()
    try:
        if path.stat().st_size > 16 * 1024 * 1024:
            raise StudioError('Choose a MIDI file smaller than 16 MB.')
        raw = path.read_bytes()
        midi = mido.MidiFile(file=io.BytesIO(raw))
    except StudioError:
        raise
    except (OSError, ValueError, EOFError, KeyError, IndexError, TypeError) as exc:
        raise StudioError(f'Could not read {path.name}: {exc}') from exc
    if midi.type not in (0, 1):
        raise StudioError('MIDI format 2 has independent timelines. Export format 0 or 1.')
    if midi.ticks_per_beat <= 0:
        raise StudioError('SMPTE MIDI timing is unsupported. Export beat/PPQ MIDI.')
    if len(midi.tracks) > 256 or sum(map(len, midi.tracks)) > 500000:
        raise StudioError('This MIDI is too large. Export just your master pattern.')
    events = [_absolute(track) for track in midi.tracks]
    tempos = set()
    notes = []
    warnings = []
    for track_index, lane in enumerate(events):
        pending = defaultdict(deque)
        for tick, index, message in lane:
            if message.type == 'time_signature' and (message.numerator, message.denominator) != (4, 4):
                raise StudioError('This workflow currently requires constant 4/4 MIDI.')
            if message.type == 'set_tempo':
                if message.tempo <= 0:
                    raise StudioError('MIDI tempo must be positive.')
                tempos.add(message.tempo)
            if message.type == 'note_on' and message.velocity > 0:
                pending[(message.channel, message.note)].append((tick, index, message))
            elif message.type in ('note_off', 'note_on'):
                key = (message.channel, message.note)
                if not pending[key]:
                    # A redundant note-off is legitimate setup and remains untouched.
                    continue
                start, on, original = pending[key].popleft()
                if tick <= start:
                    raise StudioError(f'{path.name}: a note has zero or negative length.')
                notes.append(_Note(track_index, message.channel, message.note, start,
                                   tick, original.velocity, on, index, message))
        if any(pending.values()):
            raise StudioError(f'{path.name}: a note is missing its note-off. Re-export complete notes.')
    if len(tempos) > 1:
        raise StudioError('Tempo changes inside a pattern are unsupported. Export at one fixed tempo.')
    if not notes:
        raise StudioError(f'{path.name} contains no complete musical notes.')
    events, duration = _normalize_setup_tail(midi, events, warnings)
    if duration > midi.ticks_per_beat * 4 * 512:
        raise StudioError('Use master patterns and chord loops no longer than 512 bars.')
    bars = max(1, math.ceil(duration / (4 * midi.ticks_per_beat)))
    names = [track.name or f'Track {index + 1}' for index, track in enumerate(midi.tracks)]
    roles = []
    for index, name in enumerate(names):
        track_notes = [note for note in notes if note.track == index]
        drum_name = bool(_DRUMS.search(name.replace('_', ' '))) and not re.search(r'\bsteel\s*(drums?|pan)\b', name, re.I)
        layer = bool(re.match(r'^layer(?:\b|\s*#?\d)', name, re.I))
        if layer:
            # FL Studio's combined Layer remains one untouched drum lane.
            following = index + 1
            while following < len(names) and not any(note.track == following for note in notes):
                drum_name |= bool(midi.tracks[following].name.strip())
                following += 1
        if not track_notes:
            role = 'Keep'
        elif drum_name or all(note.channel == 9 for note in track_notes):
            role = 'Drums'
        elif _CHORDS.search(name):
            role = 'Chords'
        else:
            role = 'Instrument'
        roles.append(role)
        if track_notes and any(note.channel == 9 for note in track_notes) and role != 'Drums':
            warnings.append(f'{name}: channel 10 notes are always protected as drums.')
    return _Document(path, raw, midi, events, notes, bars,
                     bars * 4 * midi.ticks_per_beat,
                     mido.tempo2bpm(next(iter(tempos), 500000)), roles, warnings)


def inspect_midi(path):
    """Return UI-friendly metadata; track indices are stable raw MIDI indices."""
    doc = _read(path)
    tracks = []
    for index, track in enumerate(doc.midi.tracks):
        notes = [note for note in doc.notes if note.track == index]
        channels = sorted({message.channel for _, _, message in doc.events[index] if hasattr(message, 'channel')})
        tracks.append({'index': index, 'name': track.name or f'Track {index + 1}',
                       'channels': channels, 'note_count': len(notes), 'role': doc.roles[index],
                       'drum_note_count': sum(note.channel == 9 for note in notes)})
    return {'path': str(doc.path), 'ppq': doc.midi.ticks_per_beat, 'bars': doc.bars,
            'bpm': doc.bpm, 'tracks': tracks, 'warnings': list(doc.warnings)}


def _roles(doc, overrides):
    roles = list(doc.roles)
    for key, value in (overrides or {}).items():
        try:
            index = int(key)
        except (ValueError, TypeError) as exc:
            raise StudioError('An instrument role refers to an invalid track.') from exc
        if not 0 <= index < len(roles) or value not in ROLES:
            raise StudioError('An instrument role or track number is invalid.')
        roles[index] = value
    return roles


def _progression(doc):
    notes = [note for note in doc.notes if note.channel != 9 and doc.roles[note.track] != 'Drums']
    if not notes:
        raise StudioError('The chord progression MIDI needs pitched chord notes, outside drum tracks/channel 10.')
    # A reference may contain doubled lanes; unify identical overlapping pitches.
    result = []
    merged = False
    for note in sorted(notes, key=lambda item: (item.pitch, item.start, item.end)):
        if result and result[-1].pitch == note.pitch and note.start < result[-1].end:
            before = result[-1]
            result[-1] = _Note(before.track, before.channel, before.pitch, before.start,
                              max(before.end, note.end), before.velocity, before.on,
                              before.off, before.off_message)
            merged = True
        else:
            result.append(note)
    return sorted(result, key=lambda item: (item.start, item.pitch)), merged


def _harmonies(notes, duration):
    events = defaultdict(list)
    for note in notes:
        events[note.start].append((1, note.pitch))
        events[note.end].append((-1, note.pitch))
    active = defaultdict(int)
    times, harmonies = [], []
    last = None
    for when, changes in sorted(events.items()):
        for direction, pitch in changes:
            active[pitch] += direction
        sounding = sorted(pitch for pitch, count in active.items() if count > 0)
        if sounding:
            last = sounding
        if last:
            times.append(when)
            harmonies.append(last)
    if not times:
        raise StudioError('The chord progression has no usable harmony.')
    return times, harmonies, duration


def _at(harmony, tick):
    times, values, duration = harmony
    index = bisect.bisect_right(times, tick % duration) - 1
    # Introductory rests use the first chord; later rests hold the previous chord.
    return values[max(0, index)]


def _root(pitches):
    pcs = set(pitch % 12 for pitch in pitches)
    bass = min(pitches) % 12
    templates = ({0, 4, 7}, {0, 3, 7}, {0, 3, 6}, {0, 4, 8},
                 {0, 2, 7}, {0, 5, 7}, {0, 4, 7, 10}, {0, 4, 7, 11},
                 {0, 3, 7, 10}, {0, 3, 6, 10}, {0, 3, 6, 9})
    candidates = []
    for root in pcs:
        intervals = {(pitch - root) % 12 for pitch in pcs}
        score = max(20 * len(intervals & template) - 13 * len(intervals ^ template) for template in templates)
        candidates.append((score, root == bass, -((root - bass) % 12), root))
    root = max(candidates)[-1]
    return root, sorted(pcs, key=lambda pitch: (pitch - root) % 12)


def _pitch_class(pitch, source, target):
    target_root, target_pcs = _root(target)
    if source:
        _, source_pcs = _root(source)
        if pitch % 12 in source_pcs:
            degree = source_pcs.index(pitch % 12)
            return target_pcs[min(degree, len(target_pcs) - 1)]
    return min(target_pcs, key=lambda pc: (min((pitch - pc) % 12, (pc - pitch) % 12), (pc - target_root) % 12))


def _rounded(value):
    return (value.numerator * 2 + value.denominator) // (2 * value.denominator)


def _reference_notes(reference, source):
    doc, notes = reference
    ratio = Fraction(source.midi.ticks_per_beat, doc.midi.ticks_per_beat)
    loop = Fraction(doc.duration) * ratio
    result = []
    cycle = 0
    while cycle * loop < source.duration:
        for note in notes:
            start = _rounded(cycle * loop + note.start * ratio)
            end = min(source.duration, _rounded(cycle * loop + note.end * ratio))
            if start < source.duration and end > start:
                result.append(_Note(note.track, note.channel, note.pitch, start, end,
                                    note.velocity, note.on, note.off, note.off_message))
        cycle += 1
    return result


def _build_track(events, duration=None):
    result = mido.MidiTrack()
    previous = 0
    end = duration or 0
    for tick, order, message in sorted(events, key=lambda item: (item[0], item[1])):
        if message.type == 'end_of_track':
            end = max(end, tick)
            continue
        result.append(message.copy(time=tick - previous))
        previous = tick
        end = max(end, tick)
    result.append(mido.MetaMessage('end_of_track', time=end - previous))
    return result


def _variation(doc, reference, roles):
    ref_doc, ref_notes = reference
    target_notes = _reference_notes(reference, doc)
    target_harmony = _harmonies(target_notes, doc.duration)
    source_chords = [note for note in doc.notes if roles[note.track] == 'Chords' and note.channel != 9]
    source_harmony = _harmonies(source_chords, doc.duration) if source_chords else None
    changes = [dict() for _ in doc.midi.tracks]
    remove = [set() for _ in doc.midi.tracks]
    additions = [[] for _ in doc.midi.tracks]
    reservations = defaultdict(list)
    for note in doc.notes:
        if note.channel == 9 or roles[note.track] in ('Drums', 'Keep'):
            reservations[note.channel, note.pitch].append((note.start, note.end))
    chord_count = 0
    for index, role in enumerate(roles):
        if role != 'Chords':
            continue
        original = [note for note in doc.notes if note.track == index and note.channel != 9]
        if not original:
            continue
        channels = {note.channel for note in original}
        if len(channels) != 1:
            raise StudioError(f'{doc.midi.tracks[index].name}: the replacement chord track must use one pitched MIDI channel.')
        channel = next(iter(channels))
        for _, event_index, message in doc.events[index]:
            if message.type in ('note_on', 'note_off', 'polytouch') and message.channel != 9:
                remove[index].add(event_index)
        for count, note in enumerate(target_notes):
            additions[index].append((note.start, 1000000 + count * 2 + 1,
                mido.Message('note_on', channel=channel, note=note.pitch, velocity=note.velocity)))
            additions[index].append((note.end, -1000000 + count * 2,
                note.off_message.copy(channel=channel, note=note.pitch, time=0)))
            reservations[channel, note.pitch].append((note.start, note.end))
        chord_count += 1
    changed = 0
    intervals = {}
    pitched = [note for note in doc.notes if note.channel != 9 and roles[note.track] == 'Instrument']
    for note in sorted(pitched, key=lambda item: (item.start, item.track, item.pitch, item.on)):
        source = _at(source_harmony, note.start) if source_harmony else None
        target = _at(target_harmony, note.start)
        preferred = _pitch_class(note.pitch, source, target)
        pcs = {pitch % 12 for pitch in target}
        candidates = sorted((pitch for pitch in range(128) if pitch % 12 in pcs),
                            key=lambda pitch: (pitch % 12 != preferred, abs(pitch - note.pitch), pitch))
        pitch = next((pitch for pitch in candidates if not any(
            start < note.end and note.start < end for start, end in reservations[note.channel, pitch])), None)
        if pitch is None:
            raise StudioError('There are too many overlapping voices on one MIDI channel to retune safely. Split instruments onto separate channels.')
        reservations[note.channel, pitch].append((note.start, note.end))
        changes[note.track][note.on] = pitch
        changes[note.track][note.off] = pitch
        intervals[note.track, note.on] = (note, pitch)
        changed += pitch != note.pitch
    midi = mido.MidiFile(type=doc.midi.type, ticks_per_beat=doc.midi.ticks_per_beat,
                         charset=doc.midi.charset)
    for index, track in enumerate(doc.midi.tracks):
        if not changes[index] and not remove[index] and not additions[index]:
            midi.tracks.append(track.copy())
            continue
        events = []
        for tick, event_index, message in doc.events[index]:
            if event_index in remove[index]:
                continue
            if event_index in changes[index]:
                message = message.copy(note=changes[index][event_index])
            elif message.type == 'polytouch' and message.channel != 9 and roles[index] == 'Instrument':
                matches = {pitch for (owner, _), (note, pitch) in intervals.items()
                           if owner == index and note.channel == message.channel and note.pitch == message.note
                           and note.start <= tick < note.end}
                if len(matches) == 1:
                    message = message.copy(note=next(iter(matches)))
                elif len(matches) > 1:
                    raise StudioError('Polyphonic aftertouch targets overlapping notes that retune differently. Split these voices before applying chords.')
            events.append((tick, event_index, message))
        midi.tracks.append(_build_track(events + additions[index], doc.duration if additions[index] else None))
    return midi, {'changed_instrument_notes': changed, 'replaced_chord_tracks': chord_count,
                  'mapping': 'Source chord-tone degree, with nearest target chord tone for other notes' if source_harmony else 'Nearest target chord tone (no source Chords track selected)'}


_CC_DEFAULTS = {1: 0, 2: 0, 4: 0, 5: 0, 7: 100, 10: 64, 11: 127, 64: 0,
                65: 0, 66: 0, 67: 0, 68: 0, 69: 0, 71: 64, 72: 64, 73: 64,
                74: 64, 75: 64, 76: 64, 77: 64, 78: 64, 91: 40, 93: 0}


def _song_scan(midi):
    """Retain original event ordinals and the port active at every event."""
    tracks = []
    for track_index, track in enumerate(midi.tracks):
        port = 0
        prefix = None
        records = []
        note_routes = set()
        for tick, index, message in _absolute(track):
            if message.type == 'midi_port':
                port = message.port
            elif message.type == 'channel_prefix':
                prefix = message.channel
            records.append((tick, track_index * 1000000 + index, message, port, prefix))
            if message.type == 'note_on' and message.velocity:
                note_routes.add((port, message.channel))
        tracks.append((records, note_routes))
    return tracks


def _song_catalog(documents, midis):
    """Match named A/B parts without requiring identical source track layouts."""
    nodes = {}
    maps = {}
    scans = {token: _song_scan(midi) for token, midi in midis.items()}
    for label in ('A', 'B'):
        document = documents[label]
        occurrences = defaultdict(int)
        maps[label] = {}
        for index, track in enumerate(document.midi.tracks):
            routes = scans[label][index][1] | scans[label + 'C'][index][1]
            name = track.name.strip()
            for port, channel in sorted(routes):
                identity = (name.casefold(), port, channel)
                occurrence = occurrences[identity]
                occurrences[identity] += 1
                # Unnamed tracks have no evidence of A/B identity; keep both.
                key = identity + (occurrence,) if name else ('unnamed', label, index, port, channel)
                node = nodes.setdefault(key, {'name': name or f'Master {label} - Track {index + 1}',
                    'original_port': port, 'original_channel': channel, 'sources': {}, 'events': {}})
                node['sources'][label] = {'track_index': index, 'port': port, 'channel': channel}
                maps[label][index, port, channel] = key

    auxiliary = {}
    for token, scan in scans.items():
        label = token[0]
        direct = maps[label]
        route_targets = defaultdict(list)
        for (index, port, channel), key in direct.items():
            route_targets[port, channel].append(key)
            nodes[key]['events'][token] = []
        for index, (records, _) in enumerate(scan):
            own = {route: key for (owner, *route_values), key in direct.items()
                   if owner == index for route in [tuple(route_values)]}
            extra = []
            for tick, order, message, port, prefix in records:
                if message.type in ('end_of_track', 'track_name'):
                    continue
                if not message.is_meta and hasattr(message, 'channel'):
                    route = (port, message.channel)
                    if route in own:
                        targets = [own[route]]
                    else:
                        # A controller-only source lane addresses the original
                        # route. Duplicate it only when that route was split.
                        targets = route_targets.get(route, [])
                    if targets:
                        for key in targets:
                            nodes[key]['events'].setdefault(token, []).append((tick, order, message))
                    else:
                        extra.append((tick, order, message, port, prefix))
                elif message.is_meta and message.type not in ('midi_port', 'channel_prefix') and own:
                    # Preserve text, lyrics, instrument labels and other metadata
                    # once, on the first instrument belonging to this raw track.
                    candidates = [key for (owner_port, _), key in own.items() if owner_port == port]
                    key = candidates[0] if candidates else next(iter(own.values()))
                    nodes[key]['events'].setdefault(token, []).append((tick, order, message))
                elif message.type not in ('midi_port', 'channel_prefix'):
                    # Opaque system messages retain their original port/prefix;
                    # their vendor-specific payload must never be rewritten.
                    extra.append((tick, order, message, port, prefix))
            if extra:
                auxiliary[label, index, token] = extra
    return nodes, auxiliary


def _song_routes(nodes):
    """Keep unique existing routes; only collisions consume new channels/ports."""
    occupied = set()
    pending = []
    # Reserve every distinct requested route before assigning collision fallbacks.
    for key, node in nodes.items():
        route = node['original_port'], node['original_channel']
        if route not in occupied:
            node['port'], node['channel'] = route
            occupied.add(route)
        else:
            pending.append(node)
    for node in pending:
        original_port = node['original_port']
        original_channel = node['original_channel']
        ports = [original_port] + [port for port in range(256) if port != original_port]
        channels = [9] if original_channel == 9 else [original_channel] + [channel for channel in range(16) if channel not in (9, original_channel)]
        route = next(((port, channel) for port in ports for channel in channels if (port, channel) not in occupied), None)
        if route is None:
            raise StudioError('This arrangement exceeds the independent instrument routes available in a standard MIDI file.')
        node['port'], node['channel'] = route
        occupied.add(route)
    counts = defaultdict(int)
    for node in nodes.values():
        counts[node['name'].casefold()] += 1
    used_names = set()
    for node in nodes.values():
        name = node['name']
        if counts[name.casefold()] > 1:
            labels = '/'.join(node['sources'])
            name += f' [{labels}, port {node["port"] + 1}, channel {node["channel"] + 1}]'
        candidate = name
        suffix = 2
        while candidate.casefold() in used_names:
            candidate = f'{name} ({suffix})'
            suffix += 1
        node['output_name'] = candidate
        used_names.add(candidate.casefold())


def _song_reset(node):
    """Known MIDI defaults precede the source's own initialization sequence."""
    channel = node['channel']
    used_controls = set()
    programs = False
    for events in node['events'].values():
        for _, _, message in events:
            if message.type == 'control_change':
                used_controls.add(message.control)
            programs |= message.type == 'program_change'
    result = [mido.Message('control_change', channel=channel, control=121, value=0),
              mido.Message('control_change', channel=channel, control=64, value=0),
              mido.Message('control_change', channel=channel, control=66, value=0),
              mido.Message('pitchwheel', channel=channel, pitch=0),
              mido.Message('aftertouch', channel=channel, value=0)]
    parameters = {6, 38, 96, 97, 98, 99, 100, 101}
    if used_controls & parameters:
        # Reset standard pitch range/tuning. The original ordered RPN/NRPN
        # setup follows unchanged, including FL Studio's 12-semitone range.
        for control, value in ((101, 0), (100, 0), (6, 2), (38, 0),
                               (101, 0), (100, 1), (6, 64), (38, 0),
                               (101, 0), (100, 2), (6, 64), (38, 0),
                               (101, 127), (100, 127), (99, 127), (98, 127)):
            result.append(mido.Message('control_change', channel=channel, control=control, value=value))
    for control in sorted(used_controls - parameters):
        if control < 120:
            result.append(mido.Message('control_change', channel=channel, control=control,
                                       value=_CC_DEFAULTS.get(control, 0)))
    if used_controls & {124, 125, 126, 127}:
        result.extend([mido.Message('control_change', channel=channel, control=124, value=0),
                       mido.Message('control_change', channel=channel, control=127, value=0)])
    if 122 in used_controls:
        result.append(mido.Message('control_change', channel=channel, control=122, value=127))
    if programs:
        result.append(mido.Message('program_change', channel=channel, program=0))
    return result


def _sequence_mid(documents, midis, tokens, arrangement_plan=None, roles=None,
                  expression_automation=True):
    """Always assemble valid master patterns; resolve shared routing locally."""
    nodes, auxiliary = _song_catalog(documents, midis)
    _song_routes(nodes)
    ppq = math.lcm(documents['A'].midi.ticks_per_beat, documents['B'].midi.ticks_per_beat)
    warnings = []
    if ppq > 32767:
        ppq = max(document.midi.ticks_per_beat for document in documents.values())
        warnings.append('Song.mid uses the higher master tick resolution; events from the other master round to its nearest tick.')
    repatched = [node for node in nodes.values() if (node['port'], node['channel']) != (node['original_port'], node['original_channel'])]
    if repatched:
        warnings.append(f'Song.mid gives {len(repatched)} colliding instrument lane(s) separate channels or MIDI ports. The four pattern files retain their original routing. Song routing.json lists the assignments.')
    if any(message.type == 'control_change' and message.control in (98, 99)
           for node in nodes.values() for events in node['events'].values() for _, _, message in events):
        warnings.append('Device-specific NRPN changes are retained. Initialize their values in each master when the receiving instrument needs a particular starting state.')
    if repatched and any(message.type == 'sysex' for events in auxiliary.values() for _, _, message, _, _ in events):
        warnings.append('System-exclusive messages keep their original MIDI port and device addressing; configure device-specific setup for any reassigned Song routes.')
    music = mido.MidiFile(type=1, ticks_per_beat=ppq, charset=documents['A'].midi.charset)
    conductor = [(0, -100, mido.MetaMessage('track_name', name='A B AC BC arrangement')),
                 (0, -99, mido.MetaMessage('time_signature', numerator=4, denominator=4))]
    lane_events = {}
    for key, node in nodes.items():
        lane_events[key] = [(0, -100, mido.MetaMessage('track_name', name=node['output_name'])),
                            (0, -99, mido.MetaMessage('midi_port', port=node['port'])),
                            (0, -98, mido.MetaMessage('channel_prefix', channel=node['channel']))]
    aux_events = {}
    control_events = {}
    dynamics_stats = []
    drum_stats = []
    phrase_stats = []
    blocks = []
    if arrangement_plan:
        for section_index, section in enumerate(arrangement_plan['sections']):
            document = documents[section['pattern'][0]]
            length = document.bars * 4 * ppq
            from .dual_drum_trim import section_drums
            omissions, hits, stats = section_drums(midis[section['pattern']],
                roles[section['pattern'][0]], section, ppq, document.bars)
            drum_stats.append({'section': section_index, 'name': section['name'], **stats})
            for repeat in range(section['repeats']):
                blocks.append((section_index, section, repeat, hits, omissions[repeat]))
    protected = {label: {index for index, role in enumerate(selected) if role in ('Drums', 'Keep')}
                 | {index for index, lane in enumerate(documents[label].events)
                    if any(getattr(message, 'channel', None) == 9 for _, _, message in lane)}
                 for label, selected in (roles or {}).items()}
    expression_allowed = {}
    for key, node in nodes.items():
        # Controller-only raw tracks may share an instrument's route. Keep
        # their expression intact even when the note-bearing track is shaped.
        # Original event ordinals retain raw-track provenance through routing.
        conflict = any(message.type == 'control_change' and message.control == 11
                       and original_order // 1000000 in protected.get(token[0], set())
                       and original_order // 1000000 != node['sources'].get(token[0], {}).get('track_index')
                       for token, records in node['events'].items()
                       for _, original_order, message in records)
        expression_allowed[key] = expression_automation and not conflict
        if arrangement_plan and expression_automation and conflict:
            warnings.append(f"{node['output_name']}: generated CC11 is off because a separate protected source track supplies expression on this route. Original expression, melodic note-velocity shaping and the separate velocity control MIDI are retained.")
    offset = 0
    for number, token in enumerate(tokens):
        label = token[0]
        document = documents[label]
        ratio = Fraction(ppq, document.midi.ticks_per_beat)
        length = document.bars * 4 * ppq
        order_base = number * 1000000000000
        block = blocks[number] if blocks else None
        marker = token
        if block:
            section_index, section, repeat, drum_hits, drum_omissions = block
            marker = f"{section['name']} | {token} | {repeat + 1}/{section['repeats']}"
            # MIDI's original charset may be Latin-1; retain full Unicode in JSON.
            marker = marker.encode(music.charset, errors='replace').decode(music.charset)
        conductor.extend([(offset, order_base, mido.MetaMessage('marker', text=marker)),
                          (offset, order_base + 1, mido.MetaMessage('set_tempo', tempo=mido.bpm2tempo(document.bpm)))])
        for key, node in nodes.items():
            events = lane_events[key]
            source = node['events'].get(token)
            if block:
                source = [(tick, order, message) for tick, order, message in source or ()
                          if order not in drum_omissions] if source is not None else None
            source_index = node['sources'].get(label, {}).get('track_index')
            source_active = not block or source_index in section['active_tracks']
            if not source_active:
                source = [(tick, order, message) for tick, order, message in source or ()
                          if order // 1000000 != source_index
                          and order // 1000000 in protected[label]] or None
            dynamics = None
            if block and source_active and source is not None and source_index not in protected[label]:
                dynamics = next((item for item in section['dynamics'] if item['track_index'] == source_index), None)
            # Every prior section's final messages sort before this section's
            # resets in this same route-owned track. Other tracks cannot cut it.
            setup = _song_reset(node) if source is not None else [
                mido.Message('control_change', channel=node['channel'], control=64, value=0),
                mido.Message('control_change', channel=node['channel'], control=66, value=0)]
            for index, message in enumerate(setup):
                events.append((offset, order_base + index, message))
            if key in control_events and dynamics is None:
                # A rest, missing part, or protected B part must not inherit
                # the previous melodic part's generated expression/knob dip.
                if expression_allowed[key]:
                    events.append((offset, order_base + 10000,
                                   mido.Message('control_change', channel=node['channel'], control=11, value=127)))
                control_events[key].extend([
                    (offset, order_base + 1000000, mido.Message('note_on', channel=0, note=60, velocity=127)),
                    (offset + 1, order_base + 1000001, mido.Message('note_off', channel=0, note=60, velocity=0))])
            rendered_source = [(_rounded(tick * ratio), original_order,
                                 message.copy(channel=node['channel']) if not message.is_meta and hasattr(message, 'channel') else message)
                               for tick, original_order, message in source or ()]
            if dynamics:
                from .dual_dynamics import shape_events
                rendered_source, automation, stats = shape_events(
                    rendered_source, drum_hits, ppq, repeat * length,
                    section['repeats'] * length, dynamics, node['channel'],
                    expression=expression_allowed[key], block_length_ticks=length)
                dynamics_stats.append({'section': section_index, 'repeat': repeat,
                                       'track_name': node['output_name'], **stats})
                controls = control_events.setdefault(key, [])
                controls.extend((offset + tick, order_base + original_order,
                                 message.copy(channel=0) if hasattr(message, 'channel') else message)
                                for tick, original_order, message in automation)
            if block and source_active and source_index not in protected[label]:
                cuts = [cut for cut in section.get('instrument_cuts', [])
                        if cut['track_index'] == source_index]
                if cuts:
                    from .dual_phrase_cuts import gate_events
                    rendered_source, stats = gate_events(rendered_source, cuts, ppq,
                                                         repeat * length, length)
                    phrase_stats.append({'section': section_index, 'repeat': repeat,
                                         'track_index': source_index,
                                         'track_name': node['output_name'], **stats})
            for tick, original_order, message in rendered_source:
                if message.is_meta and message.type in ('set_tempo', 'time_signature'):
                    conductor.append((offset + tick, order_base + 1000000000 + original_order, message))
                    continue
                events.append((offset + tick, order_base + 1000000000 + original_order, message))
        for (owner, index, source_token), records in auxiliary.items():
            if source_token != token:
                continue
            key = owner, index
            if key not in aux_events:
                name = documents[owner].midi.tracks[index].name or f'Track {index + 1}'
                aux_events[key] = [(0, -100, mido.MetaMessage('track_name', name=f'Source {owner} - {name}'))]
            events = aux_events[key]
            previous_route = None
            for tick, original_order, message, port, prefix in records:
                when = offset + _rounded(tick * ratio)
                order = order_base + 1000000000 + original_order * 4
                if message.is_meta and message.type in ('set_tempo', 'time_signature'):
                    conductor.append((when, order, message))
                    continue
                route = port, prefix
                if route != previous_route:
                    events.append((when, order, mido.MetaMessage('midi_port', port=port)))
                    if prefix is not None:
                        events.append((when, order + 1, mido.MetaMessage('channel_prefix', channel=prefix)))
                    previous_route = route
                events.append((when, order + 2, message))
        offset += length
    end_order = len(tokens) * 1000000000000
    for key, node in nodes.items():
        for index, control in enumerate((64, 66)):
            lane_events[key].append((offset, end_order + index,
                mido.Message('control_change', channel=node['channel'], control=control, value=0)))
        if key in control_events and expression_allowed[key]:
            lane_events[key].append((offset, end_order + 100,
                mido.Message('control_change', channel=node['channel'], control=11, value=127)))
    music.tracks.append(_build_track(conductor, offset))
    # Port-addressed system messages precede instrument initialization at shared
    # ticks; opaque payloads are retained rather than guessed or discarded.
    for events in aux_events.values():
        music.tracks.append(_build_track(events, offset))
    for events in lane_events.values():
        music.tracks.append(_build_track(events, offset))
    music._studio_warnings = warnings
    music._studio_routing = [{'track_name': node['output_name'], 'port': node['port'],
        'channel': node['channel'], 'sources': node['sources']} for node in nodes.values()]
    music._studio_automations = []
    for key, events in control_events.items():
        # Explicit neutral control at the end prevents a receiver from retaining
        # a ducked value after playback. End-of-track remains at the song length.
        events = [event for event in events if event[0] < offset - 1]
        sounding = sum(1 if message.type == 'note_on' and message.velocity else -1
                       for _, _, message in events if message.type in ('note_on', 'note_off'))
        if sounding:
            events.append((offset - 1, end_order, mido.Message('note_off', channel=0, note=60, velocity=0)))
        if not any(tick == 0 and message.type == 'note_on' for tick, _, message in events):
            events.extend([(0, -99, mido.Message('note_on', channel=0, note=60, velocity=127)),
                           (1, -99, mido.Message('note_off', channel=0, note=60, velocity=0))])
        events.extend([(offset - 1, end_order + 1, mido.Message('note_on', channel=0, note=60, velocity=127)),
                       (offset, end_order + 2, mido.Message('note_off', channel=0, note=60, velocity=0))])
        control = mido.MidiFile(type=1, ticks_per_beat=ppq, charset=music.charset)
        control.tracks.append(_build_track(conductor, offset))
        title = nodes[key]['output_name'] + ' - Velocity control'
        control.tracks.append(_build_track([(0, -100, mido.MetaMessage('track_name', name=title)), *events], offset))
        music._studio_automations.append((nodes[key]['output_name'], control))
    music._studio_dynamics = dynamics_stats
    music._studio_drum_stats = drum_stats
    music._studio_phrase_stats = phrase_stats
    return music


def export_dual(a_path, b_path, c_path, roles_a, roles_b, output_dir, sequence='A B AC BC',
                arrangement_plan=None, target_bars=None, expression_automation=True,
                expected_source_hashes=None, drum_trimming=True, personal_style=False,
                drum_labels_a=None, drum_labels_b=None, drum_style=False):
    """Export original A/B and chord-adjusted AC/BC into a new result folder."""
    tokens = [token for token in re.split(r'[\s,;]+', sequence.strip().upper()) if token]
    if not tokens or len(tokens) > 128 or any(token not in ('A', 'B', 'AC', 'BC') for token in tokens):
        raise StudioError('Sequence must contain A, B, AC or BC, separated by spaces (up to 128 sections).')
    a, b, c = _read(a_path), _read(b_path), _read(c_path)
    if expected_source_hashes is not None:
        if not isinstance(expected_source_hashes, dict) or set(expected_source_hashes) != {'A', 'B', 'C'}:
            raise StudioError('The source file references are incomplete. Generate the song plan again.')
        for label, document in (('A', a), ('B', b), ('C', c)):
            if expected_source_hashes[label] != hashlib.sha256(document.raw).hexdigest():
                raise StudioError(f'MIDI {label} changed after the AI plan was prepared. Reload the files and generate a new plan.')
    ra, rb = _roles(a, roles_a), _roles(b, roles_b)
    plan = None
    drum_note_map = {}
    if arrangement_plan is not None:
        from .dual_ai import build_context, validate_plan
        target = target_bars if target_bars is not None else arrangement_plan.get('total_bars') if isinstance(arrangement_plan, dict) else None
        context = build_context(a_path, b_path, c_path, roles_a, roles_b,
                                target_bars=target, drum_trimming=drum_trimming,
                                personal_style=personal_style,
                                drum_style=drum_style,
                                drum_labels_a=drum_labels_a, drum_labels_b=drum_labels_b)
        drum_note_map = {label: context['masters'][label].get('drum_labels', {})
                         for label in ('A', 'B')}
        # Reject a source changing while the plan is validated; the hashes used
        # for validation must describe the actual documents being rendered.
        for label, document in (('A', a), ('B', b)):
            if context['masters'][label]['sha256'] != hashlib.sha256(document.raw).hexdigest():
                raise StudioError('A source MIDI changed during export. Load it again before arranging.')
        if context['chord_reference']['sha256'] != hashlib.sha256(c.raw).hexdigest():
            raise StudioError('The chord MIDI changed during export. Load it again before arranging.')
        plan = validate_plan(arrangement_plan, context)
        tokens = [section['pattern'] for section in plan['sections'] for _ in range(section['repeats'])]
    for label, document, selected in (('A', a, ra), ('B', b, rb)):
        if not any(selected[note.track] == 'Chords' and note.channel != 9 for note in document.notes):
            raise StudioError(f'Mark the existing chord track in Master {label} as Chords before exporting. Choose a track with pitched notes.')
    progression, merged = _progression(c)
    ac, info_a = _variation(a, (c, progression), ra)
    bc, info_b = _variation(b, (c, progression), rb)
    warnings = list(dict.fromkeys(a.warnings + b.warnings + c.warnings))
    if merged:
        warnings.append('Overlapping copies of the same pitch in C were merged into one sustained chord voice.')
    if info_a['replaced_chord_tracks'] == 0:
        warnings.append('A has no selected Chords track: AC retunes instruments without adding a chord lane.')
    if info_b['replaced_chord_tracks'] == 0:
        warnings.append('B has no selected Chords track: BC retunes instruments without adding a chord lane.')
    midis = {'A': a.midi, 'B': b.midi, 'AC': ac, 'BC': bc}
    song = _sequence_mid({'A': a, 'B': b}, midis, tokens, plan, {'A': ra, 'B': rb}, expression_automation)
    drum_notes_trimmed = sum(item['drum_notes_trimmed'] for item in song._studio_drum_stats)
    drum_totals = {key: sum(item[key] for item in song._studio_drum_stats)
                   for key in ('source_drum_notes', 'drum_notes_trimmed', 'drum_notes_retained')}
    if plan and plan['version'] >= 2 and any(section['drum_cuts'] for section in plan['sections']) and not drum_notes_trimmed:
        raise StudioError('The AI drum cuts only covered rests; no drum hits were trimmed. Generate a new plan with cuts over actual drum hits.')
    phrase_totals = {key: sum(item[key] for item in song._studio_phrase_stats)
                     for key in ('instrument_notes_removed', 'instrument_notes_shortened',
                                 'instrument_notes_retriggered', 'instrument_notes_affected',
                                 'removed_note_beats')}
    style_applied = bool(personal_style and plan and plan['version'] == 3)
    if (style_applied and any(section.get('instrument_cuts') for section in plan['sections'])
            and not phrase_totals['instrument_notes_affected']):
        raise StudioError('The instrument cuts only covered rests. Generate a new plan with cuts over sounding phrases.')
    song_bars = sum((a if token[0] == 'A' else b).bars for token in tokens)
    song_seconds = sum((a if token[0] == 'A' else b).bars * 240 / (a if token[0] == 'A' else b).bpm for token in tokens)
    measured_end = max(sum(message.time for message in track) for track in song.tracks)
    if measured_end != song_bars * 4 * song.ticks_per_beat or (plan and song_bars != plan['total_bars']):
        raise StudioError('The exported MIDI length did not match the song plan. No result was saved.')
    warnings.extend(song._studio_warnings)
    root = Path(output_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    folder = root / (('AI Full Song ' if plan else 'A B AC BC ') + datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:6])
    folder.mkdir()
    files = []
    automation_files = []
    song_structure_file = None
    try:
        for name, document in (('A', a), ('B', b)):
            path = folder / f'{name}.mid'
            path.write_bytes(document.raw)
            files.append(str(path))
        for name, midi in (('AC', ac), ('BC', bc)):
            path = folder / f'{name}.mid'
            midi.save(path)
            files.append(str(path))
        song.save(folder / 'Song.mid')
        files.append(str(folder / 'Song.mid'))
        if plan:
            (folder / 'AI Arrangement.json').write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding='utf-8')
            files.append(str(folder / 'AI Arrangement.json'))
            drum_report = {
                'version': 1,
                'method': 'Delete selected original note-on events and their paired note-offs. No new drum attacks, retriggers, timing changes or velocity changes.',
                'drum_style': bool(drum_style and drum_trimming),
                'totals': drum_totals,
                'sections': [],
            }
            if drum_style and drum_trimming:
                from .personal_drum_style import drum_evidence_profile
                drum_report['reference_profile'] = drum_evidence_profile()
            for stats in song._studio_drum_stats:
                section = plan['sections'][stats['section']]
                label = section['pattern'][0]
                mapped = drum_note_map.get(label, {})
                drum_report['sections'].append({**stats, 'pattern': section['pattern'],
                    'line_stats': [{**line, 'label': mapped.get(line['track_index'], {}).get(line['pitch'], 'Unassigned')}
                                   for line in stats['line_stats']]})
            (folder / 'Drum Removal Report.json').write_text(json.dumps(drum_report, indent=2, ensure_ascii=False), encoding='utf-8')
            files.append(str(folder / 'Drum Removal Report.json'))
            if any(drum_note_map.values()):
                (folder / 'Drum Note Map.json').write_text(json.dumps({
                    'format': '16 Bar Studio drum note map', 'version': 1,
                    'coordinates': 'Raw source track index, then MIDI pitch number.',
                    'masters': drum_note_map}, indent=2, ensure_ascii=False), encoding='utf-8')
                files.append(str(folder / 'Drum Note Map.json'))
            from .song_structure import build_song_structure
            song_structure = build_song_structure(plan, {'A': a, 'B': b})
            song_structure_file = str(folder / 'Song Structure.json')
            Path(song_structure_file).write_text(json.dumps(song_structure, indent=2, ensure_ascii=False), encoding='utf-8')
            files.append(song_structure_file)
            if style_applied:
                from .personal_style import STYLE_PROFILE
                phrase_report = {'version': 1, 'profile': STYLE_PROFILE,
                                 'method': 'Subtract cut intervals from MIDI note spans after velocity shaping; resume surviving tails at the right edge.',
                                 'totals': phrase_totals, 'stats': song._studio_phrase_stats,
                                 'sections': []}
                section_start = 0
                for section in plan['sections']:
                    doc = a if section['pattern'][0] == 'A' else b
                    phrase_report['sections'].append({
                        'name': section['name'], 'pattern': section['pattern'],
                        'start_bar': section_start / 4 + 1,
                        'cuts': [{**cut, 'track_name': doc.midi.tracks[cut['track_index']].name,
                                  'song_start_beat': section_start + cut['start_beat'],
                                  'song_end_beat': section_start + cut['end_beat']}
                                 for cut in section.get('instrument_cuts', [])]})
                    section_start += doc.bars * section['repeats'] * 4
                (folder / 'Personal Style Cuts.json').write_text(json.dumps(phrase_report, indent=2, ensure_ascii=False), encoding='utf-8')
                files.append(str(folder / 'Personal Style Cuts.json'))
            automation_dir = folder / 'Velocity automation'
            automation_dir.mkdir()
            for index, (name, midi) in enumerate(song._studio_automations):
                filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip('. ')[:80] or 'Instrument'
                path = automation_dir / f'{index + 1:02d} {filename} - Velocity.mid'
                midi.save(path)
                automation_files.append(str(path))
            files.extend(automation_files)
            from .automation import PROTOCOL
            automation_report = {'protocol': PROTOCOL, 'expression_cc': 11 if expression_automation else None,
                                 'drum_trigger': 'Retained note-on times and velocities from Drums tracks and channel 10, after AI drum trims.',
                                 'curve': 'Linear section ramp with strongest overlapping drum dip and linear recovery.',
                                 'stats': song._studio_dynamics, 'drum_sections': song._studio_drum_stats,
                                 'files': [{'instrument': item[0], 'file': str(Path(path).relative_to(folder))}
                                           for item, path in zip(song._studio_automations, automation_files)]}
            (folder / 'Automation.json').write_text(json.dumps(automation_report, indent=2), encoding='utf-8')
            files.append(str(folder / 'Automation.json'))
        (folder / 'Song routing.json').write_text(json.dumps(song._studio_routing, indent=2), encoding='utf-8')
        files.append(str(folder / 'Song routing.json'))
        (folder / 'Chord progression C.mid').write_bytes(c.raw)
        files.append(str(folder / 'Chord progression C.mid'))
        manifest = {'format': '16 Bar Studio dual master', 'version': 2 if plan else 1,
                    'sequence': tokens, 'source_files': {label: {'path': str(doc.path), 'sha256': hashlib.sha256(doc.raw).hexdigest(),
                        'bars': doc.bars, 'bpm': doc.bpm, 'ppq': doc.midi.ticks_per_beat} for label, doc in (('A', a), ('B', b), ('C', c))},
                    'roles_a': ra, 'roles_b': rb, 'AC': info_a, 'BC': info_b, 'warnings': warnings,
                    'song_exported': True, 'song_routing': song._studio_routing,
                    'song_bars': song_bars, 'song_seconds': song_seconds,
                    'arrangement_mode': 'ai_json' if plan else 'manual_sequence',
                    'arrangement_plan': plan, 'expression_automation': bool(plan and expression_automation),
                    'drum_trimming': bool(plan and plan['version'] >= 2 and drum_trimming),
                    'drum_notes_trimmed': drum_notes_trimmed, 'drum_sections': song._studio_drum_stats,
                    'personal_style': style_applied, 'instrument_cut_totals': phrase_totals,
                    'drum_note_map': drum_note_map,
                    'drum_style': bool(plan and drum_style and drum_trimming),
                    'drum_removal_report': 'Drum Removal Report.json' if plan else None,
                    'song_structure_file': 'Song Structure.json' if song_structure_file else None}
        (folder / 'Project.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
        lines = ['A · B · AC · BC', '',
                 'A.mid and B.mid are byte-for-byte copies of your master performances.',
                 'AC.mid is A with C applied. BC.mid is B with the same C applied.',
                 'Chord progression C.mid is an unchanged copy of your chord reference.',
                 'Instrument notes retain their timing, length, velocity and channel; only pitch changes.',
                 'Selected Chords tracks receive C’s notes and rhythm on their original MIDI channel.',
                 'Drums remain unchanged in the four patterns. Keep tracks are always protected.',
                 ('Song applies the AI plan: melodic track entrances, section velocity ramps and drum-reactive dips.'
                  if plan else 'No velocity shaping, humanization, extra drum hits or automated rests are applied.'), '',
                 'C repeats to each master’s whole-bar length. Chord rests remain rests.',
                 'For instrument retuning, harmony holds through chord rests; leading rests use the first chord.',
                 'AC mapping: ' + info_a['mapping'] + '.', 'BC mapping: ' + info_b['mapping'] + '.',
                 'Chord-tone degree means root maps to root, third to third, and so on where available.',
                 'Notes outside the source chord use the closest target chord tone. This is harmonic quantization;',
                 'passing notes can change and should be reviewed by ear. Octaves avoid overlapping same-channel pitches.',
                 'When PPQ differs, C timing rounds to the nearest tick in each master.', '',
                 'Sequence: ' + ' → '.join(tokens),
                 'Song.mid contains the complete sequence with section markers and each master’s tempo.',
                 'Song keeps existing independent MIDI channels and ports. Conflicting lanes receive separate routes.',
                 'Song routing.json records the output port/channel for every named instrument (numbers are zero-based).',
                 'At section boundaries Song releases pedals, restores known MIDI defaults, then repeats the source setup.',
                 'Original controller, pitch-bend, program and system messages remain at their source-relative times.', '',
                 'Warnings:'] + (warnings or ['None.'])
        if plan:
            lines.extend(['', 'Drum Removal Report.json lists original, removed and retained hits for each drum line.',
                          'Drum editing is removal only: retained hits keep their original pitch, onset, length and velocity; no fills or drum retriggers are created.',
                          'My drum style uses the dedicated edited-drum reference for new AI plans. Saved JSON plans are rendered exactly as written.'])
            if style_applied:
                lines.extend(['', 'My arrangement style: phrase cuts and complementary melodic handoffs.',
                              f"Instrument notes removed: {phrase_totals['instrument_notes_removed']}; shortened: {phrase_totals['instrument_notes_shortened']}; resumed tails: {phrase_totals['instrument_notes_retriggered']}.",
                              'Instrument cuts remove note spans, shorten crossing notes and resume surviving tails at the end of a gap.',
                              'Resumed notes keep their original pitch and already-shaped velocity. MIDI controllers remain intact.',
                              'Personal Style Cuts.json lists the exact gates and note counts. MIDI edits do not silence instrument reverb or sustained pedal audio.',
                              'The style is guidance derived from one edited example; it does not copy that song or retrain an AI model.'])
            lines.extend(['', f'Full song: {song_bars} bars, approximately {song_seconds / 60:.2f} minutes.',
                          'AI Arrangement.json is the validated song structure and per-track dynamics plan.',
                          'Song Structure.json imports into Pop Indie Writer using Load song JSON. It carries section timing and lyric-line suggestions.',
                          'The lyric map keeps intro/outro instrumental initially. Lyric line counts can change without changing MIDI bar lengths.',
                          'Only Song.mid receives melodic velocity shaping and intentional part rests; the four patterns remain reusable.',
                          ('Generated CC11 replaces source CC11 on shaped melodic lanes. Other source controls remain.'
                           if expression_automation else 'Expression CC11 generation is off; original controller events remain.'),
                          'Velocity automation/*.mid: separate control MIDIs for each melodic instrument; import at song start.',
                          'Route each control MIDI to its own Velocity Pass/control input, not to a sounding instrument.',
                          'Control pitch stays at 60. Velocity carries the curve; note-off holds the previous control value.',
                          'Control curves reset to neutral during instrument rests and at song end; they resume on reentry.',
                          'Song.mid also contains shaped musical note velocities. CC11 response depends on the receiving instrument.',
                          'Using both musical dynamics and a volume-mapped control MIDI compounds their effect; map the control lane as desired.',
                          f'AI drum cuts removed {drum_notes_trimmed} drum attacks from Song.mid. Retained hits keep their pitch, timing, length and velocity.',
                          'Cuts select note starts; drum notes already ringing before a cut keep their natural tails. Keep tracks cannot be trimmed.',
                          'Velocity and expression automation react to retained drum hits only, after drum cuts.',
                          '', 'Song sections:'])
            start_bar = 1
            for section in plan['sections']:
                bars = (a if section['pattern'][0] == 'A' else b).bars * section['repeats']
                lines.append(f"Bars {start_bar}-{start_bar + bars - 1}: {section['name']} / {section['pattern']} x {section['repeats']}")
                start_bar += bars
        (folder / 'READ ME.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')
        files.extend([str(folder / 'Project.json'), str(folder / 'READ ME.txt')])
    except Exception as exc:
        (folder / 'EXPORT FAILED.txt').write_text(str(exc), encoding='utf-8')
        raise
    return {'folder': str(folder), 'files': files, 'warnings': warnings,
            'song_bars': song_bars, 'song_seconds': song_seconds, 'arrangement_plan': plan,
            'automation_files': automation_files,
            'drum_notes_trimmed': drum_notes_trimmed,
            'drum_style': bool(plan and drum_style and drum_trimming),
            'drum_totals': drum_totals,
            'personal_style': style_applied, **phrase_totals,
            'song_structure_file': song_structure_file,
            'summary': (f'Created your {song_bars}-bar song ({int(song_seconds) // 60}:{int(song_seconds) % 60:02d})'
                        f' plus A, B, AC and BC' + (f' and {len(automation_files)} velocity automation MIDIs.' if plan else '.'))}
