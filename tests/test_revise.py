import hashlib
import io
import json
import os
import struct
import sys
import zipfile

import mido
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from midi_revision203.main import app  # noqa: E402
from tests.test_compare import REF, make_midi, notes_track  # noqa: E402

client = TestClient(app)
EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "examples")


def example(name):
    with open(os.path.join(EXAMPLES, name), "rb") as fh:
        return fh.read()


def revise(ref_bytes, perf_bytes, actions, **form):
    data = {
        "ref_track": form.get("ref_track", 1),
        "ref_channel": form.get("ref_channel", 0),
        "perf_track": form.get("perf_track", 1),
        "perf_channel": form.get("perf_channel", 0),
    }
    files = {
        "reference": ("ref.mid", ref_bytes, "audio/midi"),
        "performance": ("perf.mid", perf_bytes, "audio/midi"),
        "actions": ("actions.json", json.dumps(actions).encode(),
                    "application/json"),
    }
    return client.post("/revise", files=files, data=data)


def unzip(response):
    zf = zipfile.ZipFile(io.BytesIO(response.content))
    revised = mido.MidiFile(file=io.BytesIO(zf.read("revised.mid")))
    audit = json.loads(zf.read("audit.json"))
    return revised, audit


def melody(mid, track=1, channel=0):
    notes = []
    open_notes = {}
    tick = 0
    for msg in mid.tracks[track]:
        tick += msg.time
        if msg.type == "note_on" and msg.velocity > 0 \
                and msg.channel == channel:
            open_notes[msg.note] = (tick, msg.velocity)
        elif msg.type in ("note_on", "note_off") \
                and msg.channel == channel and msg.note in open_notes:
            start, vel = open_notes.pop(msg.note)
            notes.append((msg.note, start, tick, vel))
    return notes


def test_fix_and_delete_on_examples():
    ref, perf = example("reference.mid"), example("performance.mid")
    r = revise(ref, perf, [{"type": "fix_pitch", "index": 2},
                           {"type": "delete_extra", "index": 5}])
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    revised, audit = unzip(r)
    assert melody(revised) == [(60, 48, 248, 90), (62, 528, 728, 90),
                               (64, 1008, 1208, 90), (65, 1488, 1688, 90),
                               (67, 1968, 2168, 90)]
    assert revised.type == 1
    assert revised.ticks_per_beat == 480
    assert len(revised.tracks) == 2
    tempos = [m.tempo for m in revised.tracks[0]
              if m.type == "set_tempo"]
    assert tempos == [500000, 400000]
    assert audit["reference_sha256"] == hashlib.sha256(ref).hexdigest()
    assert audit["performance_sha256"] == hashlib.sha256(perf).hexdigest()
    fix = audit["actions"][0]
    assert fix["before"]["pitch"] == 63
    assert fix["after"]["pitch"] == 64
    assert fix["before"]["start_tick"] == fix["after"]["start_tick"] == 1008
    delete = audit["actions"][1]
    assert delete["before"]["pitch"] == 70
    assert delete["after"] is None


def test_add_missed_uses_reference_time_and_velocity():
    ref = make_midi(tracks=[notes_track([60, 62, 64])])
    perf_events = [
        mido.Message("note_on", note=60, velocity=80, time=0),
        mido.Message("note_off", note=60, velocity=0, time=240),
        mido.Message("note_on", note=64, velocity=80, time=720),
        mido.Message("note_off", note=64, velocity=0, time=240),
    ]
    perf = make_midi(tracks=[perf_events])
    r = revise(ref, perf, [{"type": "add_missed", "index": 1}])
    assert r.status_code == 200
    revised, audit = unzip(r)
    assert melody(revised) == [(60, 0, 240, 80), (62, 480, 720, 80),
                               (64, 960, 1200, 80)]
    add = audit["actions"][0]
    assert add["before"] is None
    assert add["after"] == {"pitch": 62, "start_tick": 480,
                            "end_tick": 720}


def test_add_missed_tempo_inverse_rounding():
    ref = make_midi(tracks=[notes_track([60, 62])])
    perf = make_midi(tracks=[notes_track([60])], tempos=[(0, 300000)])
    r = revise(ref, perf, [{"type": "add_missed", "index": 1}])
    assert r.status_code == 200
    revised, _ = unzip(r)
    assert melody(revised) == [(60, 0, 240, 80), (62, 800, 1200, 80)]


def test_add_missed_extends_track_end():
    ref = make_midi(tracks=[notes_track([60, 62], dur=240, gap=240)])
    perf = make_midi(tracks=[notes_track([60])])
    r = revise(ref, perf, [{"type": "add_missed", "index": 1}])
    assert r.status_code == 200
    revised, _ = unzip(r)
    track = revised.tracks[1]
    total = sum(m.time for m in track)
    assert total >= 720
    assert track[-1].type == "end_of_track"


def test_reject_nonexistent_action():
    r = revise(REF, REF, [{"type": "fix_pitch", "index": 0}])
    assert r.status_code == 422
    d = r.json()["detail"]
    assert d["file"] == "actions"
    assert d["event"] == 0
    assert "no wrong-pitch error" in d["message"]


def test_reject_duplicate_action():
    ref, perf = example("reference.mid"), example("performance.mid")
    r = revise(ref, perf, [{"type": "delete_extra", "index": 5},
                           {"type": "delete_extra", "index": 5}])
    assert r.status_code == 422
    d = r.json()["detail"]
    assert d["event"] == 1
    assert "duplicates" in d["message"]


def test_reject_contradiction_fix_vs_delete_same_note():
    # Note 2 is a wrong-pitch pair; deleting it as extra is impossible,
    # so combine a valid fix with a delete of the same performed note by
    # crafting material where note 0 is both wrong-pitched and targeted.
    ref = make_midi(tracks=[notes_track([64])])
    perf = make_midi(tracks=[notes_track([63])])
    r = revise(ref, perf, [{"type": "fix_pitch", "index": 0},
                           {"type": "delete_extra", "index": 0}])
    assert r.status_code == 422
    d = r.json()["detail"]
    assert d["event"] == 1


def test_reject_overlap_after_add():
    ref = make_midi(tracks=[notes_track([60, 62, 64])])
    perf_events = [
        mido.Message("note_on", note=60, velocity=80, time=0),
        mido.Message("note_off", note=60, velocity=0, time=240),
        mido.Message("note_on", note=64, velocity=80, time=60),
        mido.Message("note_off", note=64, velocity=0, time=240),
    ]
    perf = make_midi(tracks=[perf_events])
    r = revise(ref, perf, [{"type": "add_missed", "index": 1}])
    assert r.status_code == 422
    d = r.json()["detail"]
    assert d["file"] == "actions"
    assert d["event"] == 0
    assert "overlap" in d["message"]


def test_reject_zero_duration_after_rounding():
    ref_events = [
        mido.Message("note_on", note=60, velocity=80, time=0),
        mido.Message("note_off", note=60, velocity=0, time=240),
        mido.Message("note_on", note=62, velocity=80, time=240),
        mido.Message("note_off", note=62, velocity=0, time=1),
    ]
    ref = make_midi(tracks=[ref_events])
    perf = make_midi(tracks=[notes_track([60])], tempos=[(0, 16777215)])
    r = revise(ref, perf, [{"type": "add_missed", "index": 1}])
    assert r.status_code == 422
    assert "zero duration" in r.json()["detail"]["message"]


def test_reject_bad_actions_json():
    data = {"ref_track": 1, "ref_channel": 0, "perf_track": 1,
            "perf_channel": 0}
    files = {
        "reference": ("ref.mid", REF, "audio/midi"),
        "performance": ("perf.mid", REF, "audio/midi"),
        "actions": ("actions.json", b"not json", "application/json"),
    }
    r = client.post("/revise", files=files, data=data)
    assert r.status_code == 422
    assert "not valid JSON" in r.json()["detail"]["message"]


def test_reject_empty_actions():
    r = revise(REF, REF, [])
    assert r.status_code == 422
    assert "1 to 1024" in r.json()["detail"]["message"]


def test_long_actions_json_within_2mib_accepted():
    ref = make_midi(tracks=[notes_track([60])])
    perf = make_midi(tracks=[notes_track([60] + [72] * 1023)])
    actions = [{"type": "delete_extra", "index": i, "pad": "x" * 1000}
               for i in range(1, 1024)]
    payload = json.dumps(actions).encode()
    assert 1024 * 1024 < len(payload) < 2 * 1024 * 1024
    data = {"ref_track": 1, "ref_channel": 0, "perf_track": 1,
            "perf_channel": 0}
    files = {
        "reference": ("ref.mid", ref, "audio/midi"),
        "performance": ("perf.mid", perf, "audio/midi"),
        "actions": ("actions.json", payload, "application/json"),
    }
    r = client.post("/revise", files=files, data=data)
    assert r.status_code == 200
    revised, audit = unzip(r)
    assert melody(revised) == [(60, 0, 240, 80)]
    assert len(audit["actions"]) == 1023


def test_reject_different_pitch_overlap():
    events = [
        mido.Message("note_on", note=60, velocity=80, time=0),
        mido.Message("note_on", note=62, velocity=80, time=100),
        mido.Message("note_off", note=60, velocity=0, time=100),
        mido.Message("note_off", note=62, velocity=0, time=100),
    ]
    bad = make_midi(tracks=[events])
    files = {
        "reference": ("ref.mid", bad, "audio/midi"),
        "performance": ("perf.mid", REF, "audio/midi"),
    }
    data = {"ref_track": 1, "ref_channel": 0, "perf_track": 1,
            "perf_channel": 0}
    r = client.post("/compare", files=files, data=data)
    assert r.status_code == 422
    assert "overlap" in r.json()["detail"]["message"]


def test_reject_zero_track_file():
    raw = b"MThd" + struct.pack(">IHHH", 6, 0, 0, 480)
    files = {
        "reference": ("ref.mid", raw, "audio/midi"),
        "performance": ("perf.mid", REF, "audio/midi"),
    }
    data = {"ref_track": 0, "ref_channel": 0, "perf_track": 1,
            "perf_channel": 0}
    r = client.post("/compare", files=files, data=data)
    assert r.status_code == 422
    assert "no tracks" in r.json()["detail"]["message"]


def test_failure_delivers_no_file():
    r = revise(REF, REF, [{"type": "delete_extra", "index": 99}])
    assert r.status_code == 422
    assert r.headers["content-type"] == "application/json"
