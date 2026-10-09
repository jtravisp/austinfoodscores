// Run: node --test frontend/*.test.mjs
import assert from "node:assert/strict";
import { test } from "node:test";

import { buildIndex, normalize, search } from "./search.js";

const PLACES = [
  { id: 1, name: "Wink", address: "1014 N Lamar Blvd Austin, TX 78703-4972" },
  { id: 2, name: "Torchy's Tacos", address: "1311 S 1st St Austin, TX 78704" },
  { id: 3, name: "Torchy's Tacos", address: "2801 Guadalupe St Austin, TX 78705" },
  { id: 4, name: "PF - Dairy Queen", address: "100 W Pecan St Pflugerville, TX 78660" },
  { id: 5, name: "Café Crème", address: "500 E 6th St Austin, TX 78701" },
  { id: 6, name: "Lamar Grill", address: "900 Congress Ave Austin, TX 78701" },
  { id: 7, name: "Brazas Taco House", address: "6901 N Lamar Blvd Austin, TX 78752" },
];
const index = buildIndex(PLACES);
const ids = (q) => search(index, q).map((p) => p.id);

test("normalize folds case, accents, apostrophes, punctuation", () => {
  assert.equal(normalize("Torchy's  Tacos #3"), "torchys tacos 3");
  assert.equal(normalize("Café Crème"), "cafe creme");
  assert.equal(normalize("  "), "");
  assert.equal(normalize(undefined), "");
});

test("matches names regardless of case and apostrophes", () => {
  assert.deepEqual(ids("WINK"), [1]);
  assert.deepEqual(ids("torchys"), [2, 3]);
  assert.deepEqual(ids("torchy's"), [2, 3]);
});

test("accent-insensitive", () => {
  assert.deepEqual(ids("cafe"), [5]);
});

test("same-name locations are all returned, ordered by address", () => {
  const hits = search(index, "torchys tacos");
  assert.deepEqual(hits.map((p) => p.address), [
    "1311 S 1st St Austin, TX 78704",
    "2801 Guadalupe St Austin, TX 78705",
  ]);
});

test("every word must match, across name and address", () => {
  assert.deepEqual(ids("taco lamar"), [7]); // "taco" in name, "lamar" in address
  assert.deepEqual(ids("torchys guadalupe"), [3]);
  assert.deepEqual(ids("wink congress"), []);
});

test("name matches outrank address-only matches", () => {
  // Lamar Grill (name starts with "lamar") before Wink and Brazas (address only).
  assert.deepEqual(ids("lamar"), [6, 7, 1]);
});

test("jurisdiction prefixes are ignored for matching but kept on the item", () => {
  const [hit] = search(index, "dairy");
  assert.equal(hit.name, "PF - Dairy Queen");
  assert.deepEqual(ids("pf"), [4]); // still findable by its city, Pflugerville
});

test("queries under two characters return nothing", () => {
  assert.deepEqual(ids("w"), []);
  assert.deepEqual(ids(" ' "), []);
});
