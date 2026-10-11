# Trailbook

**Go outside, take photos, come back to a map of your walk.**

trailbook turns a set of geotagged photos into an interactive journey: a map with your photos
pinned where you took them, a dashed path showing the route, and a title, quote and short
story written for every stop by an open-weight model running **on your own machine**.

Built for the Hacktoberfest Open-Source AI Challenge, Week 1: *Touch Grass*. 

The app only has value if you leave the screen and take photos. It does nothing during the trip, and the
journal is what you get when you come back.

## Open pieces this project runs on

| Piece | Role |
|---|---|
| [Ollama](https://ollama.com) + an open-weight vision-language model | Looks at each photo and writes its title, quote and location name, then the story of the whole journey |
| [OpenStreetMap](https://www.openstreetmap.org) tiles and [Nominatim](https://nominatim.org) | Map background and address lookup |
| [Leaflet](https://leafletjs.com) | Interactive map |
| [FastAPI](https://fastapi.tiangolo.com), [Pillow](https://python-pillow.org) | Backend, and reading GPS data from photos |
| [React](https://react.dev), [Vite](https://vitejs.dev), [Bun](https://bun.sh) | Frontend |

## How it works

```
photos ──► frontend ──► POST /journal ──► backend
                                          1. read GPS + time from each photo (EXIF)
                                          2. look up the address (Nominatim)
                                          3. local model fills in title, quote, place name
                                          4. local model writes the journey story
        journal.json ◄────────────────────  5. return JSON, delete the photos
            │
            └──► map, pins, dashed path, stop cards
```

Nothing is stored on the server. Photos live in a temporary folder for the length of one
request and are deleted before the response is sent. The journal comes back as a
downloadable `journal.json`, and the same file drives the page. You can also open a saved
`journal.json` later to see the map again without running the model.

## Repo layout

```
trailbook/
├── backend/
│   ├── journal.py          # the pipeline (also works as a command-line tool)
│   ├── app/main.py              # FastAPI server
│   └── requirements.txt
├── frontend/               # React + Vite
│   ├── src/App.jsx
│   ├── src/styles.css
│   └── ...
└── README.md
```

## Requirements

- Python 3.10 or newer
- [Bun](https://bun.sh) (or Node 18+ with npm)
- [Ollama](https://ollama.com), running locally
- Photos that contain GPS data (see "Taking good photos" below)

## Setup

### 1. The model

```bash
ollama pull gemma3:4b      # or any vision-capable model
ollama list                # check the exact name
```
The model must accept images, because every photo is sent to it. Pick a size that fits your
RAM or GPU.

### 2. The backend
```bash
# make sure uv is installed
uv add -r requirements.txt
ollama serve                 # in another terminal
ollama pull gemma3:4b    # vision capable model
```
Open `app.py` and set the model at the top, in the fixed settings block:

```python
MODEL_NAME = "gemma3:4b"          # must match a name from `ollama list`
OLLAMA_HOST = "http://localhost:11434"
TONE = "warm, observant and a little playful"
ABOUT = ""                        # optional one-line context for the story
GEOCODE = True                    # False = no address lookups, no network calls
```
Start it:
```bash
uvicorn app:app --reload          # http://localhost:8000
```
Check it with `http://localhost:8000/health`, which should return `"model_ready": true`.

### 3. The frontend

```bash
cd frontend
bun install
bun run dev                       # http://localhost:5173
```

## Using it

1. **Take the photos** with your phone's location tag turned on.
2. Open the app, **drop the photos** in, and press **Build journal**. This takes a while,
   because the model looks at every photo. The first run is the slowest.
3. You get the map: a round photo button at each stop, a dashed path between them, and a
   card for each stop with the photo, title, quote, place, coordinates, time and address.
   Click a pin, or use the prev and next buttons or the arrow keys.
4. Use **save journal.json** in the top corner to keep the data.
5. Later, on the first screen, use **open a saved journal** to see it again without the model.
   Select the photos together with the JSON file if you want them on the pins.
6. Take screenshot or video to share it with others

### Taking good photos

- Turn on location tags in the camera app, and wait for a GPS fix first. Opening a maps app
  until the blue dot is steady works well. A photo taken without a fix has empty location data.
- make sure Image contain location information in metadata
- Photos with no usable GPS data are skipped, and the app lists them with the reason.
- Browsers can't show HEIC (iPhone) photos, so their pins show a number instead of a picture.
  The location and story still work.

## API

`POST /journal`: multipart form with the photos in a repeated `files` field (jpg, jpeg, png,
heic, heif, webp). It returns the journey as a downloadable `journal.json`. The model and all
other settings are fixed in `app.py`, and any extra fields a client sends are ignored.

`GET /health` returns `{"status": "ok", "model_ready": true}`.

Errors come back as `{"detail": "..."}`: 400 bad input, 413 too large, 422 no photo had GPS
data, 503 the model is unavailable. Model details go to the server log, not to the client.
Limits: 30 photos per request, 25 MB each.

## Output format

```json
{
  "journey": {"title": "...", "story": "...", "photo_count": 3,
              "started_at": "2026-10-04T07:41:10", "ended_at": "...", "distance_km": 1.4},
  "stops": [
    {"order": 1, "filename": "p0.jpg", "taken_at": "2026-10-04T07:41:10",
     "location": {"lat": 20.2961, "lon": 85.8245, "name": "...", "address": "..."},
     "title": "...", "quote": "..."}
  ],
  "geojson": {"type": "FeatureCollection", "features": ["a Point per stop, plus the route as a LineString"]},
  "skipped": [{"filename": "x.png", "reason": "no GPS data"}]
}
```
- Images are not inside the JSON. The frontend matches photos to stops by `filename`.
- `geojson` uses the standard `[longitude, latitude]` order and works directly with Leaflet,
  MapLibre and Mapbox.
- The model fills in `title`, `quote` and the friendly location `name`. The address comes
  from the map lookup, not the model, so the model can't invent places. If the model fails,
  the address is used as the name and the other fields stay empty.

## Privacy: what leaves your machine

- **Photos, descriptions and stories:** nothing. You can run it deploy on with your needs
- **Coordinates:** sent to OpenStreetMap Nominatim for the address lookup. Set
  `GEOCODE = False` in `app.py` to turn that off.
- **Map tiles:** the browser loads them from OpenStreetMap, so the map needs internet.
- The server keeps no copy of your photos or your journal.

## Troubleshooting

| Problem | Likely cause |
|---|---|
| Every photo is skipped as "no GPS data" | Location tags were off, the phone had no GPS fix, or the photos came through a chat app |
| 503 "The AI model isn't available" | Ollama isn't running, or `MODEL_NAME` isn't in `ollama list`. Check `/health` and the server log |
| Titles or quotes are blank for some photos | A small model returned invalid JSON. Try a larger model |

## License

[MIT](LICENSE)
