"""Recognise FL's repeated end-of-selection setup without rounding musical time."""
from collections import defaultdict


def verified_fl_setup_tail(events, boundary, duration, ppq):
    """Allow a sub-beat exporter tail only with complete matching setup packets.

    Every packet after the boundary must exactly repeat the seven-message FL
    setup packet already present at that boundary, on the same track/port and
    channel. End markers cannot extend beyond those packets. Notes, automation,
    pedal-held endings and explicit longer rests therefore remain unshortened.
    The caller still checks the full controller-state history before normalising.
    """
    if not 0 < duration - boundary <= ppq:
        return False
    boundary_packets, tails = defaultdict(list), defaultdict(list)
    timeline = []
    for owner, lane in enumerate(events):
        port = 0
        for tick, index, message in lane:
            if message.type == 'midi_port':
                port = message.port
            timeline.append((tick, owner, index, port, message))
            if tick == boundary and not message.is_meta and message.type not in ('note_on', 'note_off'):
                boundary_packets[owner].append((port, message.copy(time=0)))
            if tick <= boundary:
                continue
            if message.is_meta:
                if message.type != 'end_of_track':
                    return False
            else:
                tails[owner, tick].append((port, message.copy(time=0)))

    if not tails or max(tick for _, tick in tails) != duration:
        return False
    for (owner, _), packet in tails.items():
        if not _fl_packet(packet) or packet != boundary_packets[owner][-7:]:
            return False

    # A note-off need not end a sounding note when a pedal remains pressed.
    # Keep the declared ending if sustain/sostenuto is active at the boundary.
    pedals = {}
    for tick, _, _, port, message in sorted(timeline, key=lambda event: event[:3]):
        if tick > boundary:
            break
        if message.type != 'control_change':
            continue
        route = (port, message.channel)
        if message.control in (64, 66):
            pedals[route, message.control] = message.value >= 64
        elif message.control == 121:  # Reset All Controllers releases pedals.
            pedals[route, 64] = pedals[route, 66] = False
    return not any(pedals.values())


def _fl_packet(packet):
    if len(packet) != 7:
        return False
    port, first = packet[0]
    channel = getattr(first, 'channel', None)
    if channel is None or any(p != port or getattr(m, 'channel', None) != channel for p, m in packet):
        return False
    messages = [message for _, message in packet]
    for message, control in zip(messages[:5], (101, 100, 6, 10, 7)):
        if message.type != 'control_change' or message.control != control:
            return False
    return (messages[0].value == messages[1].value == 0
            and messages[5].type == 'pitchwheel'
            and messages[6].type == 'program_change')
