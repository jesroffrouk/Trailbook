import { useEffect, useMemo, useRef, useState } from "react";
import L from "leaflet";

const API = import.meta.env.VITE_API_URL || "http://localhost:8000";
const IS_PHOTO = /\.(jpe?g|png|webp|heic|heif)$/i;

// The backend renames duplicate filenames to "name (2).jpg"; mirror that so photos match stops.
function serverNames(files) {
  const taken = new Set();
  return files.map((f) => {
    let name = f.name;
    if (taken.has(name)) {
      const dot = name.lastIndexOf(".");
      const stem = dot > 0 ? name.slice(0, dot) : name;
      const ext = dot > 0 ? name.slice(dot) : "";
      let n = 2;
      while (taken.has(`${stem} (${n})${ext}`)) n++;
      name = `${stem} (${n})${ext}`;
    }
    taken.add(name);
    return name;
  });
}

const when = (iso) =>
  iso ? new Date(iso).toLocaleString([], { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "";

// Accept only files that look like a trailbook journal.json.
function checkJournal(d) {
  const ok = d && d.journey && Array.isArray(d.stops) && d.stops.length > 0 &&
    d.stops.every((s) => s && s.filename && s.location && Number.isFinite(s.location.lat) && Number.isFinite(s.location.lon));
  if (!ok) throw new Error("This doesn't look like a trailbook journal.json: it needs a journey and stops with coordinates.");
  return { ...d, skipped: d.skipped || [] };
}

function Win({ title, accent, className = "", children }) {
  return (
    <section className={`win ${accent ? "accent" : ""} ${className}`}>
      <header>{title}</header>
      <div className="body">{children}</div>
    </section>
  );
}

const pin = (src, n, active) =>
  L.divIcon({
    className: "",
    iconSize: [56, 56],
    iconAnchor: [28, 28],
    html: `<div class="pin ${active ? "on" : ""} ${src ? "" : "nopic"}" ${src ? `style="background-image:url('${src}')"` : ""}><b>${n}</b></div>`,
  });

function TrailMap({ stops, previews, selected, onSelect }) {
  const el = useRef(null);
  const map = useRef(null);
  const markers = useRef([]);

  useEffect(() => {
    const m = L.map(el.current, { zoomControl: false, maxBoundsViscosity: 1 });
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: "&copy; OpenStreetMap contributors",
    }).addTo(m);
    L.control.zoom({ position: "bottomright" }).addTo(m);

    const pts = stops.map((s) => [s.location.lat, s.location.lon]);
    if (pts.length > 1) L.polyline(pts, { className: "trail", color: "#7dcfff", weight: 3, dashArray: "9 11" }).addTo(m);
    markers.current = stops.map((s, i) =>
      L.marker(pts[i], { icon: pin(previews[s.filename], i + 1, false) }).addTo(m).on("click", () => onSelect(i))
    );

    // Crop the view to just the journey: fit all stops, and don't let the user wander far.
    const fit = pts.length > 1 ? L.latLngBounds(pts) : L.latLng(pts[0]).toBounds(400);
    m.fitBounds(fit, { padding: [80, 80] });
    m.setMinZoom(m.getZoom() - 1);
    m.setMaxBounds(fit.pad(0.8));
    map.current = m;
    return () => m.remove();
  }, [stops]);

  useEffect(() => {
    markers.current.forEach((mk, i) => {
      mk.setIcon(pin(previews[stops[i].filename], i + 1, i === selected));
      mk.setZIndexOffset(i === selected ? 1000 : 0);
    });
    const cur = markers.current[selected];
    if (cur && map.current) map.current.panTo(cur.getLatLng());
  }, [selected, previews, stops]);

  return <div ref={el} className="map" role="img" aria-label="Map of the journey" />;
}

function StopCard({ stop, index, total, src, go }) {
  const loc = stop.location;
  return (
    <Win title={`stop ${index + 1} of ${total}  ${stop.filename}`} accent>
      {src ? <img className="shot" src={src} alt={stop.title || loc.name} /> : <div className="shot empty">no preview</div>}
      <h2>{stop.title || loc.name || stop.filename}</h2>
      {stop.quote && <p className="quote">{stop.quote}</p>}
      <dl>
        <dt>place</dt>
        <dd>{loc.name || "unknown"}</dd>
        <dt>coords</dt>
        <dd>{loc.lat.toFixed(5)}, {loc.lon.toFixed(5)}</dd>
        {stop.taken_at && (<><dt>taken</dt><dd>{when(stop.taken_at)}</dd></>)}
        {loc.address && loc.address !== loc.name && (<><dt>address</dt><dd>{loc.address}</dd></>)}
      </dl>
      <div className="nav">
        <button onClick={() => go(-1)} disabled={index === 0} aria-label="Previous stop">prev</button>
        <button onClick={() => go(1)} disabled={index === total - 1} aria-label="Next stop">next</button>
      </div>
    </Win>
  );
}

function Upload({ onBuild, onOpen, error }) {
  const [openErr, setOpenErr] = useState("");
  const [files, setFiles] = useState([]);
  const [drag, setDrag] = useState(false);
  const add = (list) => setFiles((p) => [...p, ...[...list].filter((f) => IS_PHOTO.test(f.name))]);

  async function open(list) {
    setOpenErr("");
    try { await onOpen([...list]); } catch (e) { setOpenErr(e.message); }
  }

  return (
    <main className="solo">
      <Win title="Build a Journey" accent>
        <h1>Your walk, as a map.</h1>
        <p className="dim">Add the photos you took on the way. Everything is processed by a model running on your own machine.</p>
        <label
          className={`drop ${drag ? "drag" : ""}`}
          onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
          onDragLeave={() => setDrag(false)}
          onDrop={(e) => { e.preventDefault(); setDrag(false); add(e.dataTransfer.files); }}
        >
          <input type="file" accept="image/*,.heic,.heif" multiple onChange={(e) => { add(e.target.files); e.target.value = ""; }} />
          {files.length ? `${files.length} photo${files.length > 1 ? "s" : ""} ready, drop or click to add more` : "drop photos here, or click to choose"}
        </label>
        {error && <p className="err" role="alert">{error}</p>}
        <div className="row">
          <button className="go" disabled={!files.length} onClick={() => onBuild(files)}>Build journal</button>
          {files.length > 0 && <button onClick={() => setFiles([])}>Clear</button>}
        </div>
      </Win>
      <Win title="open a saved journal" accent>
        <p className="dim">Already have a journal.json? Open it to see the map without running the model. Select your photos together with it to see them on the pins.</p>
        <label className="drop">
          <input type="file" accept=".json,application/json,image/*,.heic,.heif" multiple onChange={(e) => { open(e.target.files); e.target.value = ""; }} />
          choose journal.json (and photos)
        </label>
        {openErr && <p className="err" role="alert">{openErr}</p>}
      </Win>
    </main>
  );
}

function Loading() {
  const [s, setS] = useState(0);
  useEffect(() => { const t = setInterval(() => setS((x) => x + 1), 1000); return () => clearInterval(t); }, []);
  return (
    <main className="solo">
      <Win title="building" accent>
        <p className="mono-line">reading locations and asking the local model<span className="cursor" /></p>
        <p className="dim">{s}s elapsed. A few photos take about a minute; more photos take longer.</p>
      </Win>
    </main>
  );
}

function Result({ data, blobUrl, previews, onReset }) {
  const [sel, setSel] = useState(0);
  const { journey, stops, skipped } = data;
  const go = (d) => setSel((i) => Math.min(stops.length - 1, Math.max(0, i + d)));

  useEffect(() => {
    const k = (e) => { if (e.key === "ArrowLeft") go(-1); if (e.key === "ArrowRight") go(1); };
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [stops.length]);

  const dates = useMemo(() => [journey.started_at, journey.ended_at].filter(Boolean).map(when).join(" to "), [journey]);

  return (
    <>
      <div className="bar">
        <span className="brand">trailbook</span>
        <span className="grow" />
        <a className="dl" href={blobUrl} download="journal.json" title="Download the journal data">save journal.json</a>
        <button onClick={onReset}>new</button>
      </div>
      <main className="grid">
        <Win title="trail" className="mapwin">
          <TrailMap stops={stops} previews={previews} selected={sel} onSelect={setSel} />
        </Win>
        <div className="side">
          <Win title="journey">
            <h2>{journey.title}</h2>
            <p className="dim stats">{stops.length} stops{journey.distance_km != null && `, ${journey.distance_km} km`}{dates && `, ${dates}`}</p>
            {journey.story && <p className="story">{journey.story}</p>}
            {skipped.length > 0 && (
              <p className="warn">{skipped.length} photo{skipped.length > 1 ? "s" : ""} left out: {skipped.map((s) => `${s.filename} (${s.reason})`).join(", ")}</p>
            )}
          </Win>
          <StopCard stop={stops[sel]} index={sel} total={stops.length} src={previews[stops[sel].filename]} go={go} />
        </div>
      </main>
    </>
  );
}

export default function App() {
  const [phase, setPhase] = useState("upload");
  const [error, setError] = useState("");
  const [result, setResult] = useState(null);

  async function build(files) {
    setPhase("loading");
    setError("");
    try {
      const fd = new FormData();
      files.forEach((f) => fd.append("files", f));
      const res = await fetch(`${API}/journal`, { method: "POST", body: fd });
      if (!res.ok) {
        const j = await res.json().catch(() => ({}));
        throw new Error(j.detail || `The server returned ${res.status}.`);
      }
      const blob = await res.blob();
      const data = JSON.parse(await blob.text());
      const names = serverNames(files);
      const previews = Object.fromEntries(files.map((f, i) => [names[i], URL.createObjectURL(f)]));
      setResult({ data, previews, blobUrl: URL.createObjectURL(blob) });
      setPhase("result");
    } catch (e) {
      setError(e instanceof TypeError ? `Can't reach the API at ${API}. Start it with: uvicorn app:app` : e.message);
      setPhase("upload");
    }
  }

  async function openSaved(files) {
    const jsonFile = files.find((f) => /\.json$/i.test(f.name));
    if (!jsonFile) throw new Error("Choose a journal.json file. You can select the photos along with it.");
    let data;
    try { data = checkJournal(JSON.parse(await jsonFile.text())); }
    catch (e) { throw e instanceof SyntaxError ? new Error("That file isn't valid JSON.") : e; }
    const previews = Object.fromEntries(files.filter((f) => IS_PHOTO.test(f.name)).map((f) => [f.name, URL.createObjectURL(f)]));
    setResult({ data, previews, blobUrl: URL.createObjectURL(jsonFile) });
    setPhase("result");
  }

  function reset() {
    if (result) {
      URL.revokeObjectURL(result.blobUrl);
      Object.values(result.previews).forEach(URL.revokeObjectURL);
    }
    setResult(null);
    setPhase("upload");
  }

  if (phase === "loading") return <Loading />;
  if (phase === "result") return <Result {...result} onReset={reset} />;
  return <Upload onBuild={build} onOpen={openSaved} error={error} />;
}
