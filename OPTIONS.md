# Shrinking the download — what was measured, what shipped

Companion to `SIZE_BREAKDOWN.md`. Every number below was measured on the real
bundles in this repo; nothing here is an estimate unless it says so.

---

## 0. Corrections to the earlier analysis

Two things in the first version of this document were wrong, and they matter:

**The loose bundles are not mostly dead weight.** `catalog.bin` references 165
bundle names. Of the 144 `.bundle` files sitting in `StreamingAssets/aa/WebGL/`,
**143 are referenced** (237.4 MB) and are fetched from the server on demand.
The previous claim ("34 referenced / ~177 MB stale") came from matching only
`packed-*` names and missed the per-class bundles
(`…_monoscripts_….bundle`, `…_unitybuiltinassets_….bundle`, …).

**"xz saves 27.7 %" was a measurement bug.** The first script fed the
*decompressed node contents* to the compressor instead of the bytes that
actually ship. On the real bundle bytes xz-6 saves about 7 % relative to the
zip, not 28 %. Everything below is measured on the exact bytes that would be
downloaded.

---

## 1. The two ways to compress an asset bundle

A Unity bundle (`UnityFS`) is already compressed: its payload is cut into
128 KiB blocks, each LZ4HC-compressed (4.8× on average here). That leaves two
different options.

### Option A — recompress the bundle as it is

    bundle bytes  --xz-->  container

The player gets exactly the bundle Unity expects today, byte for byte.

### Option B — expand the LZ4 blocks first, then compress

    LZ4HC blocks  --inflate-->  raw blocks  --xz-->  container

Much smaller on the wire (the LZ4 blocks were hiding the redundancy), but the
browser now hands Unity a bundle whose blocks are **uncompressed**, so Unity
keeps ~4.8× more of it in memory.

### Measured, all 21 bundles

| | MB | vs. today |
|---|---:|---:|
| `WebGL.zip` today (what a player downloads) | 1238.73 | — |
| Option A — xz on the bundles as shipped | **1098.58** | −11.3 % |
| Option B — blocks expanded, then xz | 877.90 | −29.1 % |

Option B is 220 MB smaller. It is **not** what shipped, for one reason:
`index.html` reloads the page when the wasm heap passes `HARD_MEMORY_CAP_MB`
(1400 MB). Today the bundle data Unity holds is compressed — the whole archive
is 1.58 GB of compressed bundles representing 7.6 GB of blocks. Feed Unity the
expanded blocks and `packed-scenes` alone becomes a 3.25 GB buffer, with
`packed-sprites-scenery` at 879 MB and `packed-prefabs` at 671 MB. That is
several gigabytes of wasm heap, i.e. an instant crash rather than a faster
load. The 220 MB is not worth breaking the game over, so the container stores
Option A and the player receives the same bytes as before.

Per-bundle detail (Option A, all measured):

| bundle | shipped | xz | saved |
|---|---:|---:|---:|
| packed-scenes | 480.95 | 250.51 | 47.9 % |
| packed-sprites-scenery | 191.45 | 156.66 | 18.2 % |
| packed-prefabs | 140.29 | 114.87 | 18.1 % |
| packed-animations-props | 120.42 | 99.23 | 17.6 % |
| packed-sprites-ui | 112.33 | 10.36 | 90.8 % |
| packed-audio-hornetmusic | 88.32 | 87.80 | 0.6 % |
| packed-animations-hornet | 85.43 | 70.84 | 17.1 % |
| packed-audio-sfx | 39.21 | 38.56 | 1.7 % |
| packed-sprites-hero | 38.17 | 31.57 | 17.3 % |
| packed-sprites-hornet_bosses | 37.24 | 30.11 | 19.1 % |
| packed-audio-hornetvoices | 35.11 | 34.26 | 2.4 % |
| packed-animations-hornetnpcs | 31.14 | 25.88 | 16.9 % |
| packed-animations-hornetenemies | 30.98 | 25.84 | 16.6 % |
| packed-animations-effects | 28.65 | 23.78 | 17.0 % |
| packed-collections | 25.91 | 17.56 | 32.2 % |
| packed-sprites-hornet_enemies | 24.55 | 19.93 | 18.8 % |
| packed-animations-cutscene | 22.89 | 19.16 | 16.3 % |
| packed-sprites-effects | 22.53 | 18.69 | 17.0 % |
| packed-fonts | 11.99 | 7.44 | 37.9 % |
| packed-audio-voices | 10.84 | 10.68 | 1.5 % |
| packed-cinematics | 4.94 | 4.84 | 2.1 % |

The pattern is obvious: textures, animation clips and scene data compress
15–48 % better than DEFLATE manages, while audio is already Opus/Vorbis and
barely moves. `packed-sprites-ui` is the outlier — its bundle stores the
payload uncompressed, so DEFLATE/xz are both working on the real bytes
(112 MB → 10 MB).

---

## 2. What shipped

`StreamingAssets/aa/assets.hkar.part1 … .part69` (1,151,943,786 bytes,
1098.58 MB) replaces the 63 `WebGL.zip.part*` files, `jszip.js` is gone, and
the loader works differently:

1. **Parallel chunks.** `hk-stream.js` used to fetch parts strictly one after
   another — 63 round trips with the link idle between them. The sink already
   wrote at absolute offsets, so the download is now a pool of 6 workers
   (`BUILD.aa.concurrency`). On a 50 Mbps / 60 ms link the same bytes arrive in
   roughly 30–40 s less wall time; the saving grows with latency.
2. **data + wasm + addressables download together** instead of in a queue, so
   the 15 MB of game data no longer waits behind 1.1 GB of content.
3. **No more "unpack".** The old path did
   `blob.arrayBuffer()` on a 1.24 GB zip, `JSZip.loadAsync`, and 21
   `cache.put`s — ~2.5 GB of RAM, and nothing could start until it finished
   (20–40 s after the last byte arrived, on the main thread). Now the loader
   reads a 32-byte footer, then the index (2.3 KB), and each bundle is xz-
   decoded straight out of the container **when Unity asks for it**, in a
   worker (`hk-unpack.js`, `xz-decompress.min.js` — 20 KB of wasm), then
   cached in the Cache API. Peak RAM is one bundle instead of the whole
   archive, and the game starts as soon as the bytes are on disk.
4. **Warm-up.** One second after the container is indexed, the biggest bundles
   are decoded in the background (size-descending, capped by free storage),
   while Unity boots — so the first level load does not pay for them.
5. **Old caches are freed.** `WebGL.zip` (1.24 GB) is removed from OPFS and the
   v2 Cache API is deleted on startup.

Net effect on a 50 Mbps / 60 ms connection: **~1239 MB + 20–40 s unpack**
becomes **~1099 MB + no unpack**, plus the parallelism above.

---

## 3. Reproducing it

```bash
# 1. reassemble the current archive
cat StreamingAssets/aa/WebGL.zip.part* > /tmp/WebGL.zip   # (old layout)

# 2. build the container (about 11 min on 2 cores)
python3 tools/build_hkar.py --zip /tmp/WebGL.zip --out /tmp/assets.hkar \
        --jobs 2 --quality 9 --no-transform

# 3. check every entry: xz decode + CRC32 + UnityFS parse
python3 tools/verify_hkar.py /tmp/assets.hkar          # or tools/verify_hkar.js

# 4. split into the parts the loader downloads
python3 tools/split_container.py /tmp/assets.hkar StreamingAssets/aa
```

`tools/ufs.py` is the bundle reader used above (plain `struct` + `lz4.block`,
streams blocks so a 480 MB bundle never has to fit in memory), and
`tools/decompress_bundle.py` implements Option B if the memory trade-off ever
becomes acceptable.

---

## 4. Things deliberately not done

* **Expanding the LZ4 blocks** (Option B, 877.90 MB): see above — it multiplies
  Unity's heap by ~4.8× and the page reloads itself above 1400 MB.
* **Folding the 143 loose bundles into the container**: they are fetched on
  demand today, so a first run only pulls the ones actually used. Putting all
  237.4 MB into the container would make the first load *175 MB bigger* for
  content the player may never reach.
* **Re-encoding audio / cutting content**: that was the previous task and it is
  already applied. Nothing in this change is lossy.
