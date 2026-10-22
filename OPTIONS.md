# Cutting below 1238.7 MB — every option, and how to make the load faster

Companion to `SIZE_BREAKDOWN.md`. Everything marked **measured** was tested on
the real bundles in this repo; everything marked **estimate** is an educated
projection and is labelled as such.

---

## 0. First, a correction to the baseline

`StreamingAssets/aa/WebGL/` (the folder from your other question) is **not**
part of the 1238.7 MB. It is the *old per-bundle layout* — 144 loose
`.bundle` files, 237.4 MB, tracked in git, sitting next to `catalog.bin`.

Why it matters:

* The 21 bundles we optimised ship inside `WebGL.zip` and get extracted into
  Cache Storage on first run.
* **34 bundles referenced by `catalog.bin` are *not* in the zip.** They live in
  this folder and are fetched from the network **on demand, one at a time,
  with no OPFS caching** — 60.1 MB in total (biggest: `packed-textures`
  17.4 MB, `packed-profiles` 10.1 MB, `packed-tools` 6.4 MB,
  `packed-enemyjournal` 5.1 MB, `packed-questsystem` 4.3 MB,
  `packed-collectables` 4.0 MB, `packed-achievementslist` 3.2 MB).
* The remaining **~177 MB of that folder is stale** — not referenced by the
  current catalog. Dead weight in the repo, never downloaded by a player.

So the honest current numbers are:

| | MB |
|---|---:|
| `WebGL.zip` (21 bundles, preloaded) | 1238.7 |
| Catalog-referenced loose bundles (on demand) | 60.1 |
| **Effective total a player pulls** | **≈1299** |
| Stale loose bundles (repo only) | ≈177 |

Any plan below should also **move those 34 bundles into the compressed
container** — it makes them cacheable, compresses them ~27%, and removes 60 MB
of sequential one-at-a-time requests from gameplay.

---

## 1. Size reduction options

### Tier 1 — lossless, no content changes at all

These change only *how* the bytes are stored, never what they are.

#### 1a. Replace DEFLATE with XZ/LZMA or Brotli-11 (biggest single win)

The zip's DEFLATE is doing very little work, because it is compressing
already-LZ4HC-compressed UnityFS data. Measured on real bundles:

| Bundle | Current (zip) | xz -6 | Saving |
|---|---:|---:|---:|
| `packed-sprites-scenery` | 169.09 MB | **122.24 MB** | −46.85 (−27.7 %) |
| `packed-animations-props` | 107.37 MB | **77.01 MB** | −30.36 (−28.3 %) |
| `packed-animations-hornet` | 76.29 MB | **55.98 MB** | −20.31 (−26.6 %) |
| `packed-sprites-hero` | 34.00 MB | 25.97 MB (brotli-11) | −8.03 (−23.6 %) |

The gain is consistent at **~25–28 %** across texture-heavy bundles.
Projected on the whole archive: **1238.7 → ≈900 MB**. With the 60 MB of loose
bundles folded in: **≈945 MB total** — under 1000 MB with *nothing removed*.

For reference, these alternatives were measured and **rejected**:

| Method | Result on `sprites-hero` (34.00 MB) |
|---|---:|
| DEFLATE-9 on the *raw* (un-LZ4'd) content | 35.21 MB — worse |
| zstd-19 | −16 % only |
| LZ4HC (current inner format) | baseline |

**Cost:** you must decompress client-side, because Unity can't read xz.
That is a ~40-line change in `hk-writer.js` (a Worker already exists) using
`xz-wasm` or `brotli-wasm`. Decompression runs at roughly 100–300 MB/s, so
~5–12 s on first run, against 350 MB less to download (≈56 s at 50 Mbps).
Also removes the need for JSZip entirely.

**Effort:** medium. **Risk:** low. **Saving: ≈335 MB.**

#### 1b. Move the 34 catalog-referenced loose bundles into the container

Same 60.1 MB, but compressed (~27 % → ≈44 MB) and cached on device instead of
re-fetched. **Saving ≈16 MB** plus a real gameplay-latency win.

#### 1c. Delete the ~177 MB of stale bundles from the repo

Not download size, but it's 177 MB of git history and confusing dead weight.
**Saving: repo only.**

#### 1d. Drop the ZIP layer entirely

Once 1a is done the container is just a tar-like blob, so the zip directory
and per-entry overhead disappears (~5 KB — negligible), and you get one
streamed file instead of 63 static requests, which enables HTTP range-based
parallelism (see §2).

---

### Tier 2 — quality trade-offs, content stays

#### 2a. Texture format conversion DXT5 → ASTC 6×6

Measured texture inventory:

| Bundle | DXT5 (8 bpp) | RGBA32 (32 bpp) |
|---|---:|---:|
| `packed-sprites-scenery` | 762.6 MB | 113.3 MB |
| `packed-prefabs` | 591.7 MB | 34.6 MB |
| `packed-animations-hornet` | 357.4 MB | 2.0 MB |

Everything is BC3/DXT5 at 8 bits per pixel. **ASTC 6×6 is 3.56 bpp** — a
55 % reduction — with quality roughly comparable to BC7 for 2D art, and it is
supported on basically all mobile GPUs and Apple Silicon. It is *not*
universally available on desktop GPUs (needs
`WEBGL_compressed_texture_astc`), so this needs a capability check + fallback.

**Estimate:** re-encoding the ~2 GB of DXT5 down to ASTC would take the
texture bundles from ~590 MB compressed to **≈330–380 MB**. **Saving ≈200 MB.**

**Risk:** medium-high — needs `astcenc`, a long one-off re-encode, a runtime
format check, and it's lossy (quality knob is the block size).

#### 2b. Halve the scenery texture resolution

`packed-sprites-scenery` is 876 MB of pixels, much of it in 4096-wide
background atlases that are drawn at 1080p or below. Half-resolution is 4×
fewer pixels. Visually subtle on backgrounds, obvious on edges if you overdo
it. **Estimate ≈90–110 MB saved** on scenery + props alone.

#### 2c. Strip mipmaps where they're not needed

DXT5 atlases with full mip chains carry +33 % data. 2D UI and sprite atlases
often don't need them if the camera is orthographic at 1×. **Estimate
≈30–60 MB**, needs per-texture judgement.

#### 2d. Audio, one more notch

Now at 32–48 kbps AAC, 172.8 MB total. Dropping voices/SFX to 24 kbps mono
and music to 48 kbps mono would land at **≈110–120 MB** (**saving ≈55 MB**).
Below that, artefacts become obvious on music.

#### 2e. Deduplicate identical atlases

Several atlases appear in more than one bundle (prefabs and sprites both carry
boss art). A content-hash pass would find the duplicates; the fix (a shared
bundle + container remap) is fiddly because two containers can't own the same
pathID. **Estimate 20–50 MB.** Medium effort, medium risk.

---

### Tier 3 — removing content (you've asked me not to, listing for completeness)

#### 3a. Reachability pass — delete what nothing references

The scenes bundle is 328 MB across 574 scenes. Some are almost certainly dev
or unreachable (I already see duplicate-named entries: `Bonetown` ×2,
`Peak_05` ×3, `Belltown` ×2, `Coral_35` ×2). A proper reachability walk from
the title/menu scenes would tell you exactly which ones nothing loads.
**Estimate 30–80 MB**, and it's lossless in the sense that nobody can reach
the removed content anyway.

#### 3b. Drop whole areas

Removing the heaviest scenes was measured last time: the top 11 scenes are
≈125 MB of the 328 MB. Cutting, say, the *Under* + *Arborium* clusters is
**≈65 MB** — but it deletes playable levels.

#### 3c. Drop the scenes bundle

−328 MB → lands at **≈900 MB** with Tier 1, or **≈610 MB** with Tier 1 + 3b.

---

### Where the totals land

| Plan | Size | Content lost |
|---|---:|---|
| Today | 1299 MB | — |
| **Tier 1a + 1b** | **≈945 MB** | **nothing** |
| Tier 1 + 2d (audio) | ≈890 MB | audio quality only |
| Tier 1 + 2a (ASTC) | ≈745 MB | slight texture quality |
| Tier 1 + 2a + 2b + 2d | ≈650 MB | texture quality |
| Tier 1 + 3a (unused scenes) | ≈865 MB | nothing reachable |
| Tier 1 + 3c (no scenes) | ≈617 MB | all levels |

**The recommended path to sub-1000 MB without deleting anything is 1a + 1b.**

---

## 2. Making the load faster

This is currently a bigger win than shaving megabytes. Here is what the
loader actually does today:

1. Downloads `Build/w-pt.data` (14.6 MB) — **sequential, part by part**.
2. Downloads `Build/w-pt.wasm` (19.3 MB) — **sequential**.
3. Downloads `WebGL.zip` in **63 parts of 20.9 MB** — **strictly one at a
   time, one connection** (`for (i = 0; i < parts.length; i++) await fetchPartTo(...)`
   in `hk-stream.js`).
4. Reads the whole 1.24 GB into a single `ArrayBuffer`.
5. `JSZip.loadAsync()` — parses and inflates **in JS**, on the main thread.
6. Writes 21 files into Cache Storage.
7. Only then boots Unity.

Each of those has a fix.

#### A. Download parts in parallel — the single biggest win

`hk-stream.js` fetches parts one after another. Over HTTP/2 (or 3) the browser
multiplexes them over one connection, so you can trivially run 6–10 at once.

* With 63 parts and ~80 ms RTT, sequential costs ≈5 s of pure round-trip
  latency *before* counting bandwidth, and it never fills a fast pipe because
  a single TCP flow takes time to ramp up.
* 8-way parallel typically gives **2–4× more effective throughput** on
  high-latency links, and removes the stall between parts.

Implementation: a small worker pool (say 8) pulling from a shared part queue,
each writing at its own byte offset into the same OPFS file — the sink already
supports positional writes (`p.sink.write(bytes, at, flush)`), so the change is
contained. **Effort: low. Risk: low.**

#### B. Start the three downloads together

`data`, `wasm` and `aa` are awaited one after another. `data` and `wasm` are
needed to boot; `aa` is not needed until the first asset request. Kicking all
three off concurrently and only awaiting what's needed shortens the critical
path by the whole size of the addressables download (~1.2 GB).

Better still, boot Unity as soon as `data`+`wasm` are there, show the title
screen, and let the addressables stream in behind it (§D).

#### C. Stop buffering 1.24 GB in RAM

`await blob.arrayBuffer()` + `JSZip.loadAsync(arrayBuf)` means the whole
archive is in memory, plus JSZip's copy — this is the real reason for the
`HARD_MEMORY_CAP_MB = 1400` guard, and it's what kills low-RAM phones.

Replace with a streaming extraction in the existing worker: read the central
directory from the tail of the OPFS file, then for each entry stream
`OPFS slice → DecompressionStream('deflate-raw') → Cache Storage`.
`DecompressionStream` is native (≈10× faster than JSZip's JS inflate) and
never holds more than a window. **This alone can cut the post-download phase
from tens of seconds to a couple.** If you take Tier 1a, the same pipeline does
xz/brotli instead of deflate and you delete JSZip from the page entirely.

#### D. Split the scenes bundle and load areas on demand

The 328 MB scenes bundle is the bulk of the wait, and 90 % of it is for levels
you won't see for an hour. Splitting it back into per-area bundles
(`packed-scenes-hornet/<area>`) and letting Addressables pull only the area
you're entering turns a 1.24 GB gate into a **~150 MB gate** and then
background prefetch. This is the single largest *perceived* improvement
available: time-to-title drops from "download everything" to "download the menu".

Combine with a prefetch: when the player enters area N, quietly fetch N+1.

#### E. HTTP/2 or 3, a CDN, and immutable caching

* Serve over **HTTP/2+** so parallel parts share one connection. HTTP/1.1
  caps you at 6 connections per host, which is why naive parallelism plateaus.
* **Preconnect** early: `<link rel="preconnect" href="..." crossorigin>` plus
  `dns-prefetch` for the asset host — saves 100–300 ms on the first part.
* Part files are content-addressed by build id (`BUILD.id`). Serve them with
  `Cache-Control: public, max-age=31536000, immutable`. Today a returning
  player re-downloads unless OPFS survived; with immutable headers + a
  service worker you get instant repeat visits.
* Make sure the host isn't doing **on-the-fly gzip** on the `.part` files — it
  burns CPU and gains nothing (they're already compressed).

#### F. Serve the bundles pre-compressed by the browser itself

If you can set headers (Cloudflare Pages / Netlify / Vercel / nginx — not
GitHub Pages), ship `WebGL.bin.br` and let the browser decompress during
download with `Content-Encoding: br`. That is free: native decoding at
~1 GB/s, no wasm, no JS, and it stacks with Tier 1a's ~25 % saving.
jsDelivr (the current `FALLBACK_BASE`) already serves brotli for many types.

#### G. A service worker instead of the fetch override

Right now every asset request goes through a JS `window.fetch` patch that opens
`caches.open()` and does `cache.match(cleanPath)` — an async hop plus a string
split per request, on the main thread. A Service Worker would intercept at the
network layer, stream from OPFS with correct `Content-Length`/`Content-Type`,
and keep the main thread free during the Unity boot. Also lets you serve
`Range` requests, which Unity's download handler sometimes issues.

#### H. Also worth doing

* `WebAssembly.instantiateStreaming` with the right `application/wasm` MIME —
  compiles while downloading instead of after.
* Copy the 21 bundles into **OPFS** rather than Cache Storage if you can; OPFS
  gives you synchronous-in-worker reads and no quota-eviction surprises.
  (Cache Storage is subject to eviction under pressure; a 1.3 GB cache is a
  prime candidate.)
* Show a "ready" state as soon as the menu is playable, not when everything is
  downloaded.

### Expected effect

At 50 Mbps and 60 ms RTT, roughly:

| Change | Time to game |
|---|---:|
| Today (sequential, 1.24 GB + JSZip) | ≈250 s + 20–40 s unpack |
| + parallel parts (A) | ≈215 s + 20 s |
| + concurrent data/wasm/aa (B) | ≈210 s |
| + streaming unpack (C) | ≈205 s |
| + Tier 1a (0.95 GB) | ≈160 s |
| + split scenes / on-demand (D) | **≈35 s to title**, rest in background |

The last row is the one players will actually feel.

---

## 3. What I'd do, in order

1. **Tier 1a + 1b** — xz/brotli container, fold in the loose bundles.
   ≈945 MB, nothing removed, and it deletes JSZip in the process. (1–2 days)
2. **A + B + C** — parallel parts, concurrent datasets, streaming unpack.
   Pure win, no size change. (1 day)
3. **D** — split the scenes bundle, load areas on demand, prefetch ahead.
   This is what makes it *feel* fast. (2–3 days)
4. **2d** — audio one more notch, ≈55 MB, cheap.
5. Only if you still want smaller: **2a** (ASTC) or **3a** (unreachable scenes).

Hit 1 + 2 + 3 and you have a **≈945 MB download that reaches the title screen
in well under a minute** — which is a better product than a 650 MB download
that still makes you wait for all of it.
