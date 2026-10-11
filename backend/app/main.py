"""
trailbook API: upload geotagged photos, get the journey back as one JSON file.

Nothing is stored. Photos are written to a temporary folder for the duration of
the request and deleted before the response is returned.

Run:
    uvicorn app:app --reload            (from this folder, next to journal.py)
Docs and a test form:
    http://localhost:8000/docs

CORS_ORIGINS (env, optional): your frontend's address; default "*" allows any.

Settings are fixed in the constants below. The frontend can only send photos.
"""

import json
import logging
import os
import tempfile
import threading
from pathlib import Path

import requests
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

import journal

log = logging.getLogger("trailbook.api")

# ---- Fixed settings: change them here, the frontend can't ----
MODEL_NAME = "gemma3:4b"                   # <- put the Ollama model you chose here
OLLAMA_HOST = "http://localhost:11434"
TONE = "warm, observant and a little playful"
ABOUT = ""                                  # optional one-line context for the story
GEOCODE = True                              # look up addresses (sends coordinates to Nominatim)
MAX_FILES = 30
MAX_FILE_BYTES = 25 * 1024 * 1024
ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o.strip()]

app = FastAPI(title="trailbook", version="0.3")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],  # lets browser JS read the filename
)

# One journey at a time: it keeps the local model and Nominatim (1 request/second) happy.
_busy = threading.Lock()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _unique_name(name: str, taken: set[str]) -> str:
    """Keep the original filename (the frontend knows it); add (2), (3) on clashes."""
    if name not in taken:
        return name
    stem, suffix = Path(name).stem, Path(name).suffix
    n = 2
    while f"{stem} ({n}){suffix}" in taken:
        n += 1
    return f"{stem} ({n}){suffix}"


def _save_uploads(files: list[UploadFile], folder: Path) -> int:
    """Write uploads into `folder`, validating type and size. Returns how many were saved."""
    saved: set[str] = set()
    for up in files:
        name = Path(up.filename or "").name
        if not name:
            continue
        if Path(name).suffix.lower() not in journal.PHOTO_EXTS:
            raise HTTPException(
                400, f"'{name}' is not a supported photo type "
                     f"({', '.join(sorted(journal.PHOTO_EXTS))}).")
        data = up.file.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise HTTPException(413, f"'{name}' is larger than {MAX_FILE_BYTES // 1024 // 1024} MB.")
        final = _unique_name(name, saved)
        (folder / final).write_bytes(data)
        saved.add(final)
    return len(saved)


def _exit_to_http(message: str) -> HTTPException:
    """journal.py reports problems with sys.exit(message); turn those into HTTP errors.
    Model and server details stay in the log, the frontend gets a generic message."""
    if "Ollama" in message or ("Model" in message and "not found" in message):
        log.error("model problem: %s", message)
        return HTTPException(503, "The AI model isn't available right now.")
    return HTTPException(422, message)


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
@app.get("/health")
def health():
    """Is the API up, and is the model ready? (No model names are exposed.)"""
    info = {"status": "ok", "model_ready": False}
    try:
        r = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=3)
        r.raise_for_status()
        names = [m["name"] for m in r.json().get("models", [])]
        info["model_ready"] = any(n == MODEL_NAME or n.split(":")[0] == MODEL_NAME for n in names)
    except requests.RequestException:
        pass
    return info


@app.post("/journal")
def create_journal(files: list[UploadFile] = File(..., description="Geotagged trip photos")):
    """Photos in, journal.json out. Any other fields sent are ignored."""
    if not files:
        raise HTTPException(400, "Upload at least one photo.")
    if len(files) > MAX_FILES:
        raise HTTPException(413, f"Too many photos: the limit is {MAX_FILES} per request.")

    try:
        with tempfile.TemporaryDirectory(prefix="trailbook_") as tmp:
            folder = Path(tmp)
            if _save_uploads(files, folder) == 0:
                raise HTTPException(400, "No usable photos were uploaded.")
            with _busy:
                result = journal.build_journal(
                    folder, MODEL_NAME, OLLAMA_HOST, TONE, ABOUT,
                    geocode=GEOCODE, use_ai=True, log=log.info)
        # the temp folder (photos + geocode cache) is gone here
    except SystemExit as e:  # journal.py uses sys.exit(message) for user-facing problems
        raise _exit_to_http(str(e.code)) from None
    except HTTPException:
        raise
    except Exception:
        log.exception("journal generation failed")
        raise HTTPException(500, "Something went wrong while building the journal.") from None

    return Response(
        content=json.dumps(result, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="journal.json"'},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
