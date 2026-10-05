"""FastAPI surface for the monophonic MIDI performance comparison."""

import io
import json
import zipfile

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse, Response

from .align import align
from .errors import MidiRejection
from .midi_loader import (build_timeline, build_timeline_us, extract_melody,
                          load_midi)
from .revision import (apply_revision, parse_actions, plan_revision,
                       sha256_hex)

app = FastAPI(title="Monophonic MIDI Performance Comparator")


@app.exception_handler(MidiRejection)
async def rejection_handler(request, exc: MidiRejection):
    return JSONResponse(status_code=422, content={"detail": exc.to_detail()})


def _check_selector(track, channel, file_label):
    if track < 0:
        raise MidiRejection("track index must be >= 0", file_label)
    if not 0 <= channel <= 15:
        raise MidiRejection("channel must be between 0 and 15", file_label)


@app.post("/compare")
async def compare(
    reference: UploadFile = File(...),
    performance: UploadFile = File(...),
    ref_track: int = Form(...),
    ref_channel: int = Form(...),
    perf_track: int = Form(...),
    perf_channel: int = Form(...),
):
    _check_selector(ref_track, ref_channel, "reference")
    _check_selector(perf_track, perf_channel, "performance")

    ref_mid = load_midi(await reference.read(), "reference")
    perf_mid = load_midi(await performance.read(), "performance")

    ref_notes = extract_melody(ref_mid, ref_track, ref_channel,
                               build_timeline(ref_mid, "reference"),
                               "reference")
    perf_notes = extract_melody(perf_mid, perf_track, perf_channel,
                                build_timeline(perf_mid, "performance"),
                                "performance")

    result = align(ref_notes, perf_notes)
    result["reference"] = {
        "track": ref_track,
        "channel": ref_channel,
        "note_count": len(ref_notes),
        "notes": [n.to_dict() for n in ref_notes],
    }
    result["performance"] = {
        "track": perf_track,
        "channel": perf_channel,
        "note_count": len(perf_notes),
        "notes": [n.to_dict() for n in perf_notes],
    }
    return result


@app.post("/revise")
async def revise(
    reference: UploadFile = File(...),
    performance: UploadFile = File(...),
    ref_track: int = Form(...),
    ref_channel: int = Form(...),
    perf_track: int = Form(...),
    perf_channel: int = Form(...),
    actions: UploadFile = File(...),
):
    """Apply selective revisions and return a ZIP with the revised SMF
    and a JSON audit. Any invalid action rejects the whole request."""
    _check_selector(ref_track, ref_channel, "reference")
    _check_selector(perf_track, perf_channel, "performance")

    ref_bytes = await reference.read()
    perf_bytes = await performance.read()
    ref_mid = load_midi(ref_bytes, "reference")
    perf_mid = load_midi(perf_bytes, "performance")

    ref_tick_to_us, _, _ = build_timeline_us(ref_mid, "reference")
    ref_notes = extract_melody(ref_mid, ref_track, ref_channel,
                               build_timeline(ref_mid, "reference"),
                               "reference")
    perf_notes = extract_melody(perf_mid, perf_track, perf_channel,
                                build_timeline(perf_mid, "performance"),
                                "performance")

    action_list = parse_actions(await actions.read())
    alignment = align(ref_notes, perf_notes)
    operations, audit_actions = plan_revision(
        action_list, alignment, ref_notes, perf_mid, perf_track,
        perf_channel)
    apply_revision(perf_mid, perf_track, perf_channel, operations,
                   ref_tick_to_us, audit_actions)

    buf = io.BytesIO()
    perf_mid.save(file=buf)
    revised_bytes = buf.getvalue()

    audit = {
        "reference_sha256": sha256_hex(ref_bytes),
        "performance_sha256": sha256_hex(perf_bytes),
        "revised_sha256": sha256_hex(revised_bytes),
        "ref_track": ref_track,
        "ref_channel": ref_channel,
        "perf_track": perf_track,
        "perf_channel": perf_channel,
        "alignment": {
            "total_cost": alignment["total_cost"],
            "correct_count": alignment["correct_count"],
        },
        "actions": audit_actions,
    }
    audit_bytes = json.dumps(audit, ensure_ascii=False, indent=2
                             ).encode("utf-8")

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("revised.mid", revised_bytes)
        zf.writestr("audit.json", audit_bytes)
    return Response(
        content=zip_buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition":
                 'attachment; filename="revised.zip"'},
    )


@app.get("/health")
async def health():
    return {"status": "ok"}
