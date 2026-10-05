"""Selective revision planning and SMF rewriting.

Actions are planned once against the original alignment (no re-alignment
per action order):

- fix_pitch (performed note index from the wrong_pitch errors): change
  the pitch of the performed note_on/note_off events to the reference
  pitch, keeping absolute ticks and velocity.
- delete_extra (performed note index from the extra errors): remove the
  note's note_on and note_off events.
- add_missed (reference note index from the missed errors): insert a
  note with the reference pitch at the reference absolute start and end
  time, converted back to performed ticks through the performed tempo
  segments (exact Fraction math, rounded half up). Note-on velocity 80,
  timing counted from tick 0 with no shifting.

The whole request is rejected (locating the offending action) when an
action does not exist, is duplicated or contradicts another action, when
a rounded note would have zero duration or negative time, or when the
final selected-channel notes would overlap.
"""

import hashlib
import json
from fractions import Fraction

import mido

from .errors import MidiRejection
from .midi_loader import MAX_FILE_BYTES, build_timeline_us

MAX_ACTIONS = 1024
ADDED_NOTE_VELOCITY = 80

ACTION_TYPES = ("fix_pitch", "delete_extra", "add_missed")


def _reject_action(message, action_index):
    raise MidiRejection(message, file="actions", event=action_index)


def parse_actions(raw):
    """Parse and validate the JSON action list (1 to 1024 actions)."""
    if len(raw) > MAX_FILE_BYTES:
        raise MidiRejection("actions JSON exceeds 2 MiB limit",
                            file="actions")
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise MidiRejection(f"actions is not valid JSON: {exc}",
                            file="actions")
    if isinstance(data, dict):
        data = data.get("actions")
    if not isinstance(data, list):
        raise MidiRejection("actions must be a JSON list", file="actions")
    if not 1 <= len(data) <= MAX_ACTIONS:
        raise MidiRejection(
            f"actions must contain 1 to {MAX_ACTIONS} items, "
            f"got {len(data)}", file="actions")
    actions = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            _reject_action(f"action {i} must be an object", i)
        atype = item.get("type")
        if atype not in ACTION_TYPES:
            _reject_action(
                f"action {i} has unknown type {atype!r}; expected one of "
                f"fix_pitch, delete_extra, add_missed", i)
        index = item.get("index")
        if not isinstance(index, int) or isinstance(index, bool) \
                or index < 0:
            _reject_action(
                f"action {i} needs a non-negative integer index", i)
        actions.append({"type": atype, "index": index})
    return actions


def _locate_perf_notes(track, channel):
    """Map selected-channel notes to event indices and ticks.

    Mirrors midi_loader.extract_melody ordering (sorted by start tick
    then note-on event index) so positions match the reported perf_index
    values. Assumes the track already passed extract_melody validation.
    """
    per_tick = []
    tick = 0
    current = None
    for idx, msg in enumerate(track):
        if msg.time:
            if current is not None:
                per_tick.append(current)
                current = None
            tick += msg.time
        if msg.type not in ("note_on", "note_off"):
            continue
        if msg.channel != channel:
            continue
        if current is None:
            current = [tick, [], []]
        if msg.type == "note_off" or (msg.type == "note_on"
                                      and msg.velocity == 0):
            current[1].append((idx, msg.note))
        else:
            current[2].append((idx, msg.note))
    if current is not None:
        per_tick.append(current)

    open_notes = {}
    notes = []
    for t, closes, opens in per_tick:
        for idx, pitch in closes:
            start_tick, start_idx = open_notes.pop(pitch)
            notes.append((start_tick, t, start_idx, idx, pitch))
        for idx, pitch in opens:
            open_notes[pitch] = (t, idx)
    notes.sort(key=lambda n: (n[0], n[2]))
    return [
        {"start_tick": s, "end_tick": e, "on_idx": oi, "off_idx": fi,
         "pitch": p}
        for s, e, oi, fi, p in notes
    ]


def _round_half_up(fr):
    """Round a non-negative Fraction to the nearest int, halves up."""
    return (fr.numerator * 2 + fr.denominator) // (2 * fr.denominator)


def _us_to_tick(target_us, segments, start_us, ppqn):
    """Inverse of the tempo map: absolute microseconds -> exact tick."""
    lo, hi = 0, len(segments) - 1
    while lo < hi:
        mid_i = (lo + hi + 1) // 2
        if start_us[mid_i] <= target_us:
            lo = mid_i
        else:
            hi = mid_i - 1
    start_tick, tempo = segments[lo]
    return Fraction(start_tick) + Fraction(
        (target_us - start_us[lo]) * ppqn, tempo)


def plan_revision(actions, alignment, ref_notes, perf_mid, perf_track,
                  perf_channel):
    """Plan all actions against the original alignment.

    Returns (operations, audit_actions). Raises MidiRejection locating
    the offending action on any missing, duplicate or contradictory
    action.
    """
    wrong = {it["perf_index"]: it
             for it in alignment["errors"]["wrong_pitch"]["items"]}
    extra = {it["perf_index"]: it
             for it in alignment["errors"]["extra"]["items"]}
    missed = {it["ref_index"]: it
              for it in alignment["errors"]["missed"]["items"]}

    located = _locate_perf_notes(perf_mid.tracks[perf_track], perf_channel)

    seen = set()
    used_perf = {}
    used_ref = {}
    operations = []
    audit = []
    for i, action in enumerate(actions):
        key = (action["type"], action["index"])
        if key in seen:
            _reject_action(
                f"action {i} duplicates an earlier action "
                f"({action['type']} index {action['index']})", i)
        seen.add(key)

        if action["type"] == "fix_pitch":
            item = wrong.get(action["index"])
            if item is None:
                _reject_action(
                    f"action {i}: no wrong-pitch error for performed "
                    f"note {action['index']}", i)
            if action["index"] in used_perf:
                _reject_action(
                    f"action {i} contradicts action "
                    f"{used_perf[action['index']]}: both target "
                    f"performed note {action['index']}", i)
            used_perf[action["index"]] = i
            note = located[action["index"]]
            operations.append(("fix_pitch", note, item["ref_pitch"]))
            audit.append({
                "action_index": i,
                "type": "fix_pitch",
                "perf_index": action["index"],
                "ref_index": item["ref_index"],
                "before": {"pitch": note["pitch"],
                           "start_tick": note["start_tick"],
                           "end_tick": note["end_tick"]},
                "after": {"pitch": item["ref_pitch"],
                          "start_tick": note["start_tick"],
                          "end_tick": note["end_tick"]},
            })
        elif action["type"] == "delete_extra":
            item = extra.get(action["index"])
            if item is None:
                _reject_action(
                    f"action {i}: no extra-note error for performed "
                    f"note {action['index']}", i)
            if action["index"] in used_perf:
                _reject_action(
                    f"action {i} contradicts action "
                    f"{used_perf[action['index']]}: both target "
                    f"performed note {action['index']}", i)
            used_perf[action["index"]] = i
            note = located[action["index"]]
            operations.append(("delete_extra", note, None))
            audit.append({
                "action_index": i,
                "type": "delete_extra",
                "perf_index": action["index"],
                "before": {"pitch": note["pitch"],
                           "start_tick": note["start_tick"],
                           "end_tick": note["end_tick"]},
                "after": None,
            })
        else:
            item = missed.get(action["index"])
            if item is None:
                _reject_action(
                    f"action {i}: no missed-note error for reference "
                    f"note {action['index']}", i)
            if action["index"] in used_ref:
                _reject_action(
                    f"action {i} contradicts action "
                    f"{used_ref[action['index']]}: both target "
                    f"reference note {action['index']}", i)
            used_ref[action["index"]] = i
            ref_note = ref_notes[action["index"]]
            operations.append(("add_missed", ref_note, i))
            audit.append({
                "action_index": i,
                "type": "add_missed",
                "ref_index": action["index"],
                "before": None,
                "after": {"pitch": ref_note.pitch},
            })
    return operations, audit


def apply_revision(perf_mid, perf_track, perf_channel, operations,
                   ref_tick_to_us, audit):
    """Apply planned operations to the performance MIDI in place.

    Preserves type, PPQN, all tracks, tempo and every unmodified
    event's content, absolute tick and relative order. New events at
    the same tick are placed after existing events, note-off before
    note-on; the track end is postponed when needed. Delta times are
    rebuilt non-negative.
    """
    _, segments, start_us = build_timeline_us(perf_mid, "performance")
    ppqn = perf_mid.ticks_per_beat
    track = perf_mid.tracks[perf_track]

    abs_events = []
    tick = 0
    for msg in track:
        tick += msg.time
        abs_events.append([tick, msg])

    eot_pos = None
    eot_tick = abs_events[-1][0] if abs_events else 0
    for pos, (_, msg) in enumerate(abs_events):
        if msg.type == "end_of_track":
            eot_pos = pos
            eot_tick = abs_events[pos][0]
            break

    deleted = set()
    pitch_fixes = {}
    added = []

    for op in operations:
        kind = op[0]
        if kind == "fix_pitch":
            _, note, new_pitch = op
            pitch_fixes[note["on_idx"]] = new_pitch
            pitch_fixes[note["off_idx"]] = new_pitch
        elif kind == "delete_extra":
            _, note, _ = op
            deleted.add(note["on_idx"])
            deleted.add(note["off_idx"])
        else:
            _, ref_note, action_index = op
            start_target = ref_tick_to_us(ref_note.start_tick)
            end_target = ref_tick_to_us(ref_note.end_tick)
            if start_target < 0 or end_target < 0:
                _reject_action(
                    f"action {action_index}: negative absolute time",
                    action_index)
            start_tick = _round_half_up(_us_to_tick(
                start_target, segments, start_us, ppqn))
            end_tick = _round_half_up(_us_to_tick(
                end_target, segments, start_us, ppqn))
            if start_tick < 0 or end_tick < 0:
                _reject_action(
                    f"action {action_index}: negative tick after "
                    f"rounding", action_index)
            if end_tick <= start_tick:
                _reject_action(
                    f"action {action_index}: added note for reference "
                    f"pitch {ref_note.pitch} has zero duration after "
                    f"rounding (ticks {start_tick}..{end_tick})",
                    action_index)
            on = mido.Message("note_on", note=ref_note.pitch,
                              velocity=ADDED_NOTE_VELOCITY,
                              channel=perf_channel, time=0)
            off = mido.Message("note_off", note=ref_note.pitch,
                               velocity=0, channel=perf_channel, time=0)
            added.append((start_tick, False, on, action_index))
            added.append((end_tick, True, off, action_index))
            for entry in audit:
                if entry["action_index"] == action_index:
                    entry["after"]["start_tick"] = start_tick
                    entry["after"]["end_tick"] = end_tick

    # Overlap check on the final selected-channel note set. Original
    # notes are non-overlapping (validated at load) and deletions or
    # pitch changes cannot create overlaps, so any overlap involves an
    # added note; report its action index.
    scan = []
    for idx, (t, msg) in enumerate(abs_events):
        if idx in deleted or msg.type not in ("note_on", "note_off"):
            continue
        if getattr(msg, "channel", None) != perf_channel:
            continue
        is_off = (msg.type == "note_off"
                  or (msg.type == "note_on" and msg.velocity == 0))
        scan.append((t, is_off, None))
    for t, is_off, msg, ai in added:
        scan.append((t, is_off, ai))
    scan.sort(key=lambda e: (e[0], 0 if e[1] else 1))
    open_start = None
    open_action = None
    for t, is_off, ai in scan:
        if is_off:
            if open_start is not None:
                open_start = None
                open_action = None
        else:
            if open_start is not None:
                culprit = ai if ai is not None else open_action
                _reject_action(
                    f"action {culprit}: resulting notes on channel "
                    f"{perf_channel} overlap at tick {t}", culprit)
            open_start = t
            open_action = ai

    # Build the new event timeline: originals keep content and relative
    # order; new events go after existing events at the same tick, with
    # new note-offs before new note-ons.
    tagged = []
    for idx, (t, msg) in enumerate(abs_events):
        if idx == eot_pos or idx in deleted:
            continue
        if idx in pitch_fixes:
            msg = msg.copy()
            msg.note = pitch_fixes[idx]
        tagged.append((t, 0, idx, msg))
    for seq, (t, is_off, msg, ai) in enumerate(added):
        tagged.append((t, 1, 0 if is_off else 1 + seq, msg))
    tagged.sort(key=lambda e: (e[0], e[1], e[2]))

    last_tick = tagged[-1][0] if tagged else 0
    new_track = mido.MidiTrack()
    prev = 0
    for t, _, _, msg in tagged:
        delta = t - prev
        assert delta >= 0
        msg.time = delta
        new_track.append(msg)
        prev = t
    new_track.append(mido.MetaMessage(
        "end_of_track", time=max(eot_tick, last_tick) - prev))
    perf_mid.tracks[perf_track] = new_track
    return perf_mid


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()
