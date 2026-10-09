// Austin Food Scores: map, filters, detail panel, decliners.
//
// Everything is same-origin: the page and /api/* come from one host (CloudFront
// in AWS, `afs serve` locally), so there is no API URL to configure and no CORS.
//
// Security note: names and addresses come from a third-party dataset. They are
// only ever inserted with textContent / setAttribute (see el()), never innerHTML.

/* global L */

import { buildIndex, search } from "./search.js";

const API = "/api";
const AUSTIN = [30.29, -97.74];
const BAND_LABEL = { green: "90+", yellow: "70–89", red: "under 70", none: "no score" };

// --- tiny DOM helpers ---------------------------------------------------------

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const child of children.flat()) {
    if (child != null && child !== false) node.append(child instanceof Node ? child : String(child));
  }
  return node;
}

const svg = (tag, attrs = {}) => {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
};

// API dates are 'YYYY-MM-DD'. new Date('2026-05-05') would parse as UTC midnight,
// which is the previous evening in Austin, so build a local date instead.
function parseDate(iso) {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(y, m - 1, d);
}
const fmtDate = (iso) => parseDate(iso).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });

async function getJSON(path) {
  const res = await fetch(API + path);
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`${res.status} from ${path}`);
  return res.json();
}

// --- map -------------------------------------------------------------------------

const map = L.map("map", { zoomControl: true }).setView(AUSTIN, 11);
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
}).addTo(map);

const markerIcons = Object.fromEntries(
  ["green", "yellow", "red", "none"].map((band) => [band, L.divIcon({ className: `marker ${band}`, iconSize: [14, 14] })]),
);

const clusters = L.markerClusterGroup({
  chunkedLoading: true, // add markers in slices so the page stays responsive
  showCoverageOnHover: false,
  maxClusterRadius: 50,
  // Clicks are handled below instead of by the plugin: half of all
  // establishments share a point with another (strip malls, food halls, the
  // airport has 42), and the plugin's "spiderfy" fans those out as unlabeled
  // dots you have to click one by one.
  zoomToBoundsOnClick: false,
  spiderfyOnMaxZoom: false,
  iconCreateFunction: (cluster) => donutIcon(cluster.getAllChildMarkers()),
});

// Establishments closer than this count as "one spot" (shared address, or a
// geocode a few meters off). Zooming in can't separate them, so list them.
const SPOT_METERS = 30;

function spreadMeters(latlngs) {
  const bounds = L.latLngBounds(latlngs);
  return map.distance(bounds.getSouthWest(), bounds.getNorthEast());
}

clusters.on("clusterclick", (e) => {
  const children = e.layer.getAllChildMarkers();
  const atMaxZoom = map.getZoom() >= map.getMaxZoom();
  if (atMaxZoom || spreadMeters(children.map((m) => m.getLatLng())) <= SPOT_METERS) {
    const c = e.layer.getLatLng();
    location.hash = `#/spot/${c.lat.toFixed(5)},${c.lng.toFixed(5)}`;
  } else {
    e.layer.zoomToBounds({ padding: [20, 20] });
  }
});
map.addLayer(clusters);

// Cluster icon: a ring split by band in proportion, count in the middle.
// Each segment is a circle whose dashed stroke covers only its share of the
// circumference, rotated into place with stroke-dashoffset. The markup is built
// only from numbers computed here, never from API data, and colors come from
// CSS classes (the CSP forbids inline style attributes).
function donutIcon(children) {
  const n = children.length;
  const counts = { green: 0, yellow: 0, red: 0, none: 0 };
  for (const m of children) counts[m.options.band] += 1;

  const size = n < 10 ? 32 : n < 100 ? 40 : 48;
  const r = size / 2 - 5;
  const c = 2 * Math.PI * r;
  let offset = 0;
  let segments = "";
  for (const band of ["green", "yellow", "red", "none"]) {
    if (!counts[band]) continue;
    const len = (counts[band] / n) * c;
    segments += `<circle class="seg ${band}" r="${r}" cx="${size / 2}" cy="${size / 2}" `
      + `stroke-dasharray="${len} ${c - len}" stroke-dashoffset="${-offset}"/>`;
    offset += len;
  }
  const html = `<svg viewBox="0 0 ${size} ${size}" width="${size}" height="${size}" aria-hidden="true">`
    + `<g transform="rotate(-90 ${size / 2} ${size / 2})">${segments}</g>`
    + `<circle class="hole" r="${r - 4}" cx="${size / 2}" cy="${size / 2}"/>`
    + `<text x="50%" y="50%" dy="0.35em" text-anchor="middle">${n}</text></svg>`;
  return L.divIcon({ html, className: "cluster", iconSize: [size, size] });
}

// --- state -----------------------------------------------------------------------

const markers = new Map(); // facility_id -> L.Marker
let features = [];
let searchIndex = []; // normalized name/address per feature, built once at load
let results = null; // current search matches (array), or null when not searching
let onMap = []; // features passing every filter (what the map shows right now)
const MAX_RESULTS = 50;

const filtersForm = document.getElementById("filters");
const countEl = document.getElementById("count");
const panel = document.getElementById("panel");
const panelBody = document.getElementById("panel-body");

function currentFilters() {
  const data = new FormData(filtersForm);
  const zip = (data.get("zip") || "").trim();
  return {
    bands: new Set(data.getAll("band")),
    zip: /^\d{5}$/.test(zip) ? zip : null,
    q: (data.get("q") || "").trim(),
  };
}

// Band, ZIP, and search are all filters: the map and the count show what passes
// all three, and search results are drawn only from those.
function applyFilters({ fit = false } = {}) {
  const { bands, zip, q } = currentFilters();
  const passes = (f) => {
    const p = f.properties;
    return bands.has(p.latest_band || "none") && (!zip || p.zip5 === zip);
  };
  let visible = features.filter(passes);

  const matches = search(searchIndex, q); // [] when q is under 2 characters
  results = q.length >= 2 ? matches.filter(passes) : null;
  if (results) visible = results;

  onMap = visible;
  clusters.clearLayers();
  clusters.addLayers(visible.map((f) => markers.get(f.id)));
  const total = features.length.toLocaleString();
  countEl.textContent = results
    ? `${visible.length.toLocaleString()} ${visible.length === 1 ? "match" : "matches"} of ${total}`
    : `${visible.length.toLocaleString()} of ${total} establishments`;
  if (fit && visible.length) map.fitBounds(clusters.getBounds(), { maxZoom: 15, padding: [20, 20] });

  // With no establishment or decliners view open, the panel shows the results.
  if (!location.hash) route();
}

filtersForm.addEventListener("change", (e) => {
  if (e.target.name !== "q") applyFilters(); // search runs on input, below
});
filtersForm.zip.addEventListener("input", (e) => {
  const v = e.target.value.trim();
  if (v === "" || v.length === 5) applyFilters({ fit: v.length === 5 });
});

// Search as you type, debounced so a fast typist doesn't refilter 6k markers per key.
let searchTimer;
filtersForm.q.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    // Typing a new search leaves any open establishment/decliners view.
    if (location.hash) history.pushState(null, "", location.pathname + location.search);
    applyFilters();
  }, 150);
});
filtersForm.q.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && filtersForm.q.value) {
    filtersForm.q.value = "";
    applyFilters();
  }
});

// Enter opens the best match.
filtersForm.addEventListener("submit", (e) => {
  e.preventDefault();
  clearTimeout(searchTimer);
  applyFilters();
  if (results && results.length) location.hash = `#/establishment/${results[0].id}`;
});

async function loadEstablishments() {
  countEl.textContent = "Loading…";
  const geojson = await getJSON("/establishments");
  features = geojson.features;
  for (const f of features) {
    const [lon, lat] = f.geometry.coordinates; // GeoJSON order is [lon, lat]
    const band = f.properties.latest_band || "none";
    const marker = L.marker([lat, lon], { icon: markerIcons[band], title: f.properties.name, band, keyboard: true });
    marker.on("click", () => { location.hash = `#/establishment/${f.id}`; });
    // Name and score on hover (and on keyboard focus, which Leaflet 1.9 also
    // handles). A function returning a node, not a string: Leaflet inserts
    // string content as HTML, and names come from a third-party dataset.
    marker.bindTooltip(() => tooltipNode(f.properties), { direction: "top", offset: [0, -8] });
    markers.set(f.id, marker);
  }
  searchIndex = buildIndex(features, { name: (f) => f.properties.name, address: (f) => f.properties.address });
  applyFilters();
}

// --- panel -----------------------------------------------------------------------

function showPanel(...content) {
  // replaceChildren() would render null as the text "null"; drop optional parts here.
  panelBody.replaceChildren(...content.filter((c) => c != null && c !== false));
  panel.hidden = false;
  panel.scrollTop = 0;
}

// Close steps back one level: an establishment opened from search returns to
// the results; the results list itself (no hash) just hides.
document.getElementById("panel-close").addEventListener("click", () => {
  if (!location.hash) {
    panel.hidden = true;
    highlight.remove();
    return;
  }
  history.pushState(null, "", location.pathname + location.search);
  route();
});
document.getElementById("show-decliners").addEventListener("click", () => { location.hash = "#/decliners"; });

// A ring drawn over the selected establishment (a vector layer, outside the clusters).
const highlight = L.circleMarker([0, 0], { radius: 13, className: "highlight", interactive: false });

function focusMarker(id) {
  const marker = markers.get(id);
  if (!marker) return;
  // Center on it rather than clusters.zoomToShowLayer(), which spiderfies when the
  // marker shares a spot -- the very UI the spot list replaces. The ring marks it.
  highlight.setLatLng(marker.getLatLng()).addTo(map);
  map.setView(marker.getLatLng(), Math.max(map.getZoom(), 16));
}

function deltaNode(delta) {
  if (delta == null) return el("span", { class: "delta" }, "—");
  if (delta === 0) return el("span", { class: "delta" }, "no change");
  return delta < 0
    ? el("span", { class: "delta down", title: "change since the previous inspection" }, `▼ ${delta}`)
    : el("span", { class: "delta up", title: "change since the previous inspection" }, `▲ +${delta}`);
}

function trendText(e) {
  if (!e.trend) return "Not enough history for a trend (needs 3 scored inspections).";
  const slope = Number(e.trend_slope);
  return `${e.trend[0].toUpperCase()}${e.trend.slice(1)}: ${slope > 0 ? "+" : ""}${slope} points per inspection over the last 3.`;
}

async function showEstablishment(id) {
  showPanel(el("p", { class: "note" }, "Loading…"));
  const e = await getJSON(`/establishments/${id}`);
  if (!e) {
    showPanel(el("h2", {}, "Not found"), el("p", { class: "note" }, `No establishment with id ${id}.`));
    return;
  }
  const band = e.latest_band || "none";
  showPanel(
    el("h2", {}, e.name),
    el("p", { class: "address" }, e.address || "Address not listed"),
    el("div", { class: "headline" },
      el("span", { class: `score-badge ${band}`, title: `Latest score: ${BAND_LABEL[band]}` }, e.latest_score ?? "No score"),
      deltaNode(e.score_delta),
    ),
    el("p", { class: "trend" }, trendText(e)),
    el("dl", { class: "facts" },
      el("dt", {}, "Last inspected"),
      el("dd", {}, e.last_inspected_on ? `${fmtDate(e.last_inspected_on)} (${e.days_since_last} days ago)` : "—"),
      el("dt", {}, "Scored inspections"), el("dd", {}, e.scored_inspections),
      el("dt", {}, "Scores under 80"), el("dd", {}, e.inspections_under_80),
    ),
    el("h3", {}, "Score history"),
    scoreChart(e.history),
    historyTable(e.history),
    e.lat == null ? el("p", { class: "note" }, "This establishment has no map coordinates in the city's data.") : null,
  );
  focusMarker(e.facility_id);
}

function historyTable(history) {
  return el("table", { class: "history" },
    el("thead", {}, el("tr", {}, el("th", {}, "Date"), el("th", {}, "Score"), el("th", {}, "Type"))),
    el("tbody", {}, history.map((h) =>
      el("tr", {},
        el("td", {}, fmtDate(h.inspected_on)),
        el("td", { class: "score" }, h.score ?? "—"),
        el("td", {}, h.process),
      ),
    )),
  );
}

// Score over time as inline SVG: shaded band zones, a line, and band-colored points.
function scoreChart(history) {
  const pts = history.filter((h) => h.score != null).map((h) => ({ ...h, t: parseDate(h.inspected_on).getTime() })).reverse();
  if (pts.length < 2) return el("p", { class: "note" }, pts.length ? "Only one scored inspection so far." : "No scored inspections.");

  const W = 340, H = 150, L_ = 30, R = 10, T = 8, B = 22;
  const yMin = Math.min(60, Math.floor((Math.min(...pts.map((p) => p.score)) - 5) / 10) * 10);
  const t0 = pts[0].t, t1 = pts[pts.length - 1].t;
  const x = (t) => L_ + ((t - t0) / (t1 - t0 || 1)) * (W - L_ - R);
  const y = (s) => T + ((100 - s) / (100 - yMin)) * (H - T - B);

  const chart = svg("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Score history chart" });
  for (const [band, hi, lo] of [["green", 100, 90], ["yellow", 90, 70], ["red", 70, yMin]]) {
    chart.append(svg("rect", { class: `zone ${band}`, x: L_, y: y(hi), width: W - L_ - R, height: y(lo) - y(hi) }));
  }
  for (const s of [yMin, 70, 90, 100]) {
    chart.append(svg("line", { class: "grid", x1: L_, x2: W - R, y1: y(s), y2: y(s) }));
    const label = svg("text", { class: "axis", x: L_ - 4, y: y(s) + 3, "text-anchor": "end" });
    label.textContent = s;
    chart.append(label);
  }
  chart.append(svg("polyline", { class: "line", points: pts.map((p) => `${x(p.t)},${y(p.score)}`).join(" ") }));
  for (const p of pts) {
    const dot = svg("circle", { class: `pt ${p.band}`, cx: x(p.t), cy: y(p.score), r: 4.5 });
    const title = svg("title");
    title.textContent = `${fmtDate(p.inspected_on)}: ${p.score} (${p.process})`;
    dot.append(title);
    chart.append(dot);
  }
  for (const [p, anchor] of [[pts[0], "start"], [pts[pts.length - 1], "end"]]) {
    const label = svg("text", { class: "axis", x: x(p.t), y: H - 6, "text-anchor": anchor });
    label.textContent = parseDate(p.inspected_on).toLocaleDateString(undefined, { year: "numeric", month: "short" });
    chart.append(label);
  }
  return chart;
}

function tooltipNode(p) {
  return el("span", {}, el("strong", {}, p.name), ` · ${p.latest_score ?? "no score"}`);
}

// One row in a list of establishments (search results, a shared spot):
// score badge, name, and full street address, which tells same-name locations apart.
function resultRow(f, { address = true } = {}) {
  const p = f.properties;
  const band = p.latest_band || "none";
  return el("li", {},
    el("button", { type: "button", onclick: () => { location.hash = `#/establishment/${f.id}`; } },
      el("div", { class: "row" },
        el("span", { class: `mini ${band}`, title: `Latest score: ${BAND_LABEL[band]}` }, p.latest_score ?? "—"),
        el("div", {},
          el("div", { class: "name" }, p.name),
          address ? el("div", { class: "meta" }, p.address || "Address not listed") : null,
        ),
      ),
    ),
  );
}

// Everything on the map within SPOT_METERS of a point, by name.
function showSpot(lat, lng) {
  const here = L.latLng(lat, lng);
  const atSpot = onMap
    .filter((f) => here.distanceTo([f.geometry.coordinates[1], f.geometry.coordinates[0]]) <= SPOT_METERS)
    .sort((a, b) => a.properties.name.localeCompare(b.properties.name));
  if (!atSpot.length) {
    showPanel(el("h2", {}, "Nothing here"), el("p", { class: "note" }, "No establishments at this spot match the current filters."));
    return;
  }
  // A shared address goes in the heading once instead of on every row.
  const addresses = new Set(atSpot.map((f) => f.properties.address));
  const shared = addresses.size === 1 ? [...addresses][0] : null;
  highlight.setLatLng(here).addTo(map);
  showPanel(
    el("h2", {}, `${atSpot.length} establishments here`),
    el("p", { class: "address" }, shared || "Same spot on the map; addresses differ slightly."),
    el("ol", { class: "decliners results" }, atSpot.map((f) => resultRow(f, { address: !shared }))),
  );
}

function showSearchResults() {
  const { q, bands, zip } = currentFilters();
  const filtered = bands.size < 4 || zip;
  const items = results.slice(0, MAX_RESULTS).map((f) => resultRow(f));

  let note;
  if (!results.length) {
    note = filtered
      ? `No matches for “${q}” within the current filters. Try turning bands back on or clearing the ZIP.`
      : `No matches for “${q}”.`;
  } else {
    note = `${results.length.toLocaleString()} ${results.length === 1 ? "match" : "matches"} for “${q}”`
      + (filtered ? " within the current filters" : "")
      + (results.length > MAX_RESULTS ? `; showing the first ${MAX_RESULTS}. Add a word to narrow it.` : ".");
  }
  showPanel(
    el("h2", {}, "Search"),
    el("p", { class: "note" }, note),
    items.length ? el("ol", { class: "decliners results" }, items) : null,
  );
}

async function showDecliners() {
  showPanel(el("p", { class: "note" }, "Loading…"));
  const { zip } = currentFilters();
  const data = await getJSON(`/stats/decliners?limit=50${zip ? `&zip=${zip}` : ""}`);
  const items = data.decliners.map((d) =>
    el("li", {},
      el("button", { type: "button", onclick: () => { location.hash = `#/establishment/${d.facility_id}`; } },
        el("div", { class: "name" }, d.name),
        el("div", { class: "meta" }, `${d.previous_score} → ${d.latest_score} (${d.score_delta}) · ${fmtDate(d.latest_scored_on)} · ${d.zip5 ?? ""}`),
      ),
    ),
  );
  showPanel(
    el("h2", {}, "Decliners"),
    el("p", { class: "note" },
      `Dropped ${data.min_drop}+ points at an inspection in the last ${data.window_days} days, biggest drop first${zip ? `, in ${zip}` : ""}.`),
    items.length ? el("ol", { class: "decliners" }, items) : el("p", {}, "None right now."),
  );
}

// --- routing: #/establishment/<id>, #/decliners ----------------------------------------
// The hash makes any view linkable and the back button work.

async function route() {
  const m = location.hash.match(/^#\/establishment\/(\d+)$/);
  const spot = location.hash.match(/^#\/spot\/(-?[\d.]+),(-?[\d.]+)$/);
  try {
    if (m) await showEstablishment(Number(m[1]));
    else if (spot) showSpot(Number(spot[1]), Number(spot[2]));
    else if (location.hash === "#/decliners") await showDecliners();
    else if (results) {
      showSearchResults();
      highlight.remove();
    } else {
      panel.hidden = true;
      highlight.remove();
    }
  } catch (err) {
    showPanel(el("h2", {}, "Something went wrong"), el("p", { class: "note" }, String(err.message || err)));
  }
}
window.addEventListener("hashchange", route);

loadEstablishments()
  .then(route)
  .catch((err) => { countEl.textContent = `Couldn't load establishments (${err.message}).`; });
