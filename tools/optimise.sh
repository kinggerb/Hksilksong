#!/usr/bin/env bash
# Rebuild StreamingAssets/aa/WebGL.zip after the size cuts.
#
# The archive is shipped split into 20,866,662-byte chunks; this script glues
# them together, replaces the bundles produced by tools/cut_*.py and re-splits.
set -euo pipefail
cd "$(dirname "$0")/.."
PART=20866662
PY=${PY:-/tmp/venv/bin/python}
WORK=${WORK:-/tmp/optimise}

mkdir -p "$WORK"
echo '== 1. reassemble WebGL.zip'
$PY - <<'PY'
import glob, re
parts = sorted(glob.glob('StreamingAssets/aa/WebGL.zip.part*'),
               key=lambda p: int(re.search(r'part(\d+)$', p).group(1)))
with open('/tmp/optimise/WebGL.zip', 'wb') as out:
    for p in parts:
        out.write(open(p, 'rb').read())
print(len(parts), 'parts')
PY

echo '== 2. extract the bundles we rewrite'
$PY - <<'PY'
import zipfile
z = zipfile.ZipFile('/tmp/optimise/WebGL.zip')
for name in z.namelist():
    if name.startswith(('packed-cinematics', 'packed-collections', 'packed-fonts',
                        'packed-audio-hornetmusic', 'packed-audio-hornetvoices',
                        'packed-audio-sfx', 'packed-audio-voices')):
        open('/tmp/optimise/' + name, 'wb').write(z.read(name))
PY

echo '== 3. cut'
$PY tools/cut_cinematics.py "$WORK"/packed-cinematics*.bundle "$WORK"/out-cinematics.bundle
$PY tools/cut_collections.py "$WORK"/packed-collections*.bundle "$WORK"/out-collections.bundle
$PY tools/cut_fonts.py       "$WORK"/packed-fonts*.bundle       "$WORK"/out-fonts.bundle
$PY tools/cut_audio.py "$WORK"/packed-audio-hornetmusic*.bundle "$WORK"/out-music.bundle 48
$PY tools/cut_audio.py "$WORK"/packed-audio-hornetvoices*.bundle "$WORK"/out-hornetvoices.bundle 32
$PY tools/cut_audio.py "$WORK"/packed-audio-sfx*.bundle         "$WORK"/out-sfx.bundle 40
$PY tools/cut_audio.py "$WORK"/packed-audio-voices*.bundle      "$WORK"/out-voices.bundle 48

echo '== 4. repack'
ARGS=()
for pair in cinematics collections fonts; do
  ARGS+=("$(basename "$WORK"/packed-$pair*.bundle)=$WORK/out-$pair.bundle")
done
for pair in hornetmusic:music hornetvoices:hornetvoices sfx:sfx voices:voices; do
  ARGS+=("$(basename "$WORK"/packed-audio-${pair%%:*}_*.bundle)=$WORK/out-${pair##*:}.bundle")
done
$PY tools/repack_zip.py "$WORK/WebGL.zip" "$WORK/WebGL-new.zip" "${ARGS[@]}"

echo '== 5. verify + split'
$PY tools/verify_zip.py "$WORK/WebGL-new.zip"
rm -f StreamingAssets/aa/WebGL.zip.part*
$PY - "$WORK/WebGL-new.zip" <<'PY'
import os, sys
src, PART = sys.argv[1], 20866662
size = os.path.getsize(src)
n = (size + PART - 1) // PART
with open(src, 'rb') as f:
    for i in range(n):
        open(f'StreamingAssets/aa/WebGL.zip.part{i+1}', 'wb').write(f.read(PART))
print(f'{n} parts, last {size - (n - 1) * PART} bytes; update index.html:')
print(f"  partBytes: new Array({n-1}).fill(PART).concat([{size - (n - 1) * PART}])")
print(f'  total: {size}')
PY
