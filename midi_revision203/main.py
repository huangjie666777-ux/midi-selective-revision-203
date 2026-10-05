"""FastAPI surface for the monophonic MIDI performance comparison."""

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse

from .align import align
from .errors import MidiRejection
from .midi_loader import build_timeline, extract_melody, load_midi

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


@app.get("/health")
async def health():
    return {"status": "ok"}
