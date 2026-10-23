# What the 1,098.58 MB is made of — and what could still shrink it

Measured on the shipped container (`StreamingAssets/aa/assets.hkar.part*`,
1,098.58 MB). Reproduce with `tools/compose_report.py` and
`tools/texture_inventory.py`.

---

## 1. By asset type

Every bundle was unpacked, every serialized file's object table was read, and
each object's byte size was charged to the bytes a player downloads (a
bundle's xz size shared out in proportion to its contents).

| what | in the download | share | objects | raw before compression |
|---|---:|---:|---:|---:|
| **Textures** (`Texture2D`) | **710 MB** | 65 % | 2,359 | 4,618 MB |
| **Audio** (`.resS` AAC payloads + `AudioClip`) | **175 MB** | 16 % | 17,572 clips | 188 MB |
| Scene/prefab logic (`MonoBehaviour`) | 43 MB | 3.9 % | 535,264 | 508 MB |
| Particles (`ParticleSystem`) | 54 MB | 4.9 % | 94,450 | 680 MB |
| Meshes | 34 MB | 3.1 % | 6,386 | 437 MB |
| Sprites (`Sprite`) | 17 MB | 1.6 % | 18,548 | — |
| `SpriteRenderer` component data | 12 MB | 1.1 % | 791,606 | 151 MB |
| `Transform` / `GameObject` | 13 MB | 1.2 % | 1.18 M each | 88 MB |
| Fonts | 3 MB | 0.3 % | 31 | — |
| everything else (materials, shaders, colliders, animation clips, …) | ~37 MB | 3 % | — | — |

Two facts explain almost all of it:

* **Textures are already GPU-compressed.** 99 % of the atlas bytes are **BC7**
  (8 bits per pixel). The whole 4.6 GB of texture data squeezing down to
  710 MB is that BC7 payload plus the sprite/mesh data that rides along.
* **Audio is already lossy-encoded.** 188 MB of AAC in MP4 containers across
  17,572 clips, 19.6 hours, ≈22 kbit/s average. xz gets 0.6–2.4 % out of it.

Per bundle, the biggest contributors are the same as before
(`packed-sprites-scenery` 156.7, `packed-prefabs` 114.9, `packed-animations-props`
99.2, `packed-animations-hornet` 70.8, `packed-scenes` 250.5 MB). See
`OPTIONS.md` for the full table.

---

## 2. Is there any lossless headroom left? No — measured

On `packed-animations-props` (126 MB of bundle, the same bytes the container
stores), four encoder settings were run to completion:

| setting | output | vs. shipped |
|---|---:|---:|
| `xz --lzma2=preset=9` (what ships) | 99.23 MB | — |
| `xz --lzma2=preset=9e` | 99.24 MB | +0.01 MB |
| `xz --lzma2=preset=9e,dict=256MiB` | 99.13 MB | **−0.10 MB (−0.1 %)** |
| `xz --lzma2=preset=9e,lc=0,pb=0` | 99.34 MB | +0.11 MB |

A 256 MiB dictionary buys 0.1 %. Brotli-11 measured worse earlier (7.3 % vs
xz's 17 % on a texture bundle). Solid-compressing every bundle as one stream
would break random access — the loader would have to decode from the start of
the container to reach any bundle — for a gain in the same range.

**Conclusion: 1,098.58 MB is the lossless floor for this content.**

---

## 3. Textures are the only real lever, and it is lossy

2,359 textures, 4,618 MB raw. 448 of them are `sactx-…-BC7-…` sprite atlases
that account for 99 % of the bytes:

| resolution (min side) | textures |
|---|---:|
| 4096 px | 119 |
| 2048 px | 150 |
| 1024 px | 125 |
| 512 px | 45 |
| ≤256 px | 9 |

The largest atlases are 4096×8192 and 8192×4096 — 32 MB of BC7 each before
mips (`sactx-…-Peak-…`, `…-Song-…`, `…-City-…`, `…-Shellwood-…`). A
quarter-resolution copy of `Peak` is one sixteenth of the pixels but the same
source art, so the saving is linear in pixels.

| option | texture data | download | lossy? | notes |
|---|---:|---:|---|---|
| halve every atlas ≥1024 px | −4,100 MB | **−618 MB → ~480 MB** | yes | 2× softer everywhere; the single biggest lever |
| BC7 → ASTC 6×6 (3.56 bpp) | −3,050 MB | **−460 MB → ~640 MB** | slightly | ASTC needs iOS/Android WebGL support — desktop has no ASTC, so a fallback copy would erase the win |
| BC7 → ETC2 RGB 4 bpp | −2,747 MB | **−414 MB → ~685 MB** | yes + no alpha | ETC2 RGB has no alpha; UI/sprites need alpha, and ETC2 RGBA8 is 8 bpp (no saving) |
| drop mip chains | ~−25 % | ~−175 MB | yes | visible shimmer at distance; not recommended |
| re-encode audio lower | — | ≤ −40 MB | very | already 22 kbit/s average; anything lower is audible mush |

---

## 4. Things that would help without touching the art

1. **Pack the 144 loose bundles too.** `StreamingAssets/aa/WebGL/` still holds
   143 catalog-referenced `.bundle` files (237 MB) that the game fetches one at
   a time, uncompressed, with no on-device cache. Putting them through the same
   xz container saves ≈**43 MB** and makes them cacheable. Small, lossless,
   safe.
2. **Don't download scenes for areas the player never visits.** `packed-scenes`
   is 250.5 MB of the 1,098.58 MB. Addressables already loads it lazily per
   scene, but the container ships it in full up front. Splitting it (574 scene
   nodes) and streaming on demand would cut the **first** download by up to
   250 MB without changing the total — it needs the catalog rebuilt, so it is
   not a loader-only change.
3. **Keep the current 1,098.58 MB.** It is the same bytes Unity expects; no
   runtime cost, no quality change, and every alternative above trades image
   quality or platform support for size.

---

## 5. Reproducing

```bash
# join the container
cat StreamingAssets/aa/assets.hkar.part* > /tmp/assets.hkar   # sort -V for part1..part9

# per-class composition of every bundle (forks UnityPy per serialized file)
python3 tools/compose_report.py                                 # ~10 min, 2 cores

# texture inventory: format, resolution, atlas names
python3 tools/texture_inventory.py /tmp/ana/prefabs.bundle

# compressor experiments on one bundle
xz -c --lzma2=preset=9e,dict=256MiB bundle > out.xz
```
