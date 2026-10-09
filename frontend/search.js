// Name/address search over the establishments the map already has.
//
// Pure functions (no DOM, no Leaflet) so they run under `node --test` as well
// as in the browser. Exact matching on purpose: every query word must appear in
// the name or address, after normalizing case, accents, and punctuation.

// "PF - Dairy Queen": the prefix marks another Travis County jurisdiction.
// It's kept for display but not matched, so "dairy" ranks it as a name prefix.
const JURISDICTION = /^[A-Z]{2,3} - /;

/** "Joe's  Café #3" -> "joes cafe 3" */
export function normalize(text) {
  return (text || "")
    .normalize("NFD")
    .replace(/\p{M}/gu, "") // strip accents: é -> e
    .toLowerCase()
    .replace(/['’`]/g, "") // apostrophes join: joe's -> joes
    .replace(/[^\p{L}\p{N}]+/gu, " ") // everything else separates words
    .trim();
}

/** Precompute normalized keys once per establishment (6k items, done at load). */
export function buildIndex(items, { name = (x) => x.name, address = (x) => x.address } = {}) {
  return items.map((item) => ({
    item,
    name: normalize((name(item) || "").replace(JURISDICTION, "")),
    address: normalize(address(item)),
  }));
}

/**
 * Rank one entry against the query words, or null if any word is missing.
 * Lower is better: 0 name starts with the query, 1 every word is in the name,
 * 2 words split across name and address, 3 address only.
 */
export function rank(entry, words, phrase) {
  let inName = 0;
  for (const word of words) {
    const n = entry.name.includes(word);
    if (!n && !entry.address.includes(word)) return null;
    if (n) inName += 1;
  }
  if (entry.name.startsWith(phrase)) return 0;
  if (inName === words.length) return 1;
  if (inName > 0) return 2;
  return 3;
}

/** All matches, best first. `index` comes from buildIndex. */
export function search(index, query) {
  const phrase = normalize(query);
  if (phrase.length < 2) return [];
  const words = phrase.split(" ");
  const hits = [];
  for (const entry of index) {
    const r = rank(entry, words, phrase);
    if (r !== null) hits.push({ entry, rank: r });
  }
  hits.sort((a, b) =>
    a.rank - b.rank
    || a.entry.name.localeCompare(b.entry.name)
    || a.entry.address.localeCompare(b.entry.address));
  return hits.map((h) => h.entry.item);
}
