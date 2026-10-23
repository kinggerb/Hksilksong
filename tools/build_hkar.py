#!/usr/bin/env python3
"""Build the ``.hkar`` asset container that replaces WebGL.zip.

Pipeline per bundle:

    UnityFS (LZ4HC blocks)  ->  UnityFS (uncompressed blocks)  ->  xz

The first step is lossless and reversible: the bundle keeps the same logical
payload at the same offsets, only the block storage changes.  The second step
is where the saving comes from - LZ4HC leaves ~70% of the redundancy on the
table that xz/LZMA can still exploit.

Container layout (all little-endian)::

    [ payload: entry0 | entry1 | ... ][ index ][ footer 32 bytes ]

    footer: <8sIIQQ>  magic b'HKAR0001', count, reserved, index_off, index_len
    entry : <HBBIQQQI> name_len, codec, reserved, reserved,
                       offset, comp_size, raw_size, crc32   + name bytes

Codec 0 = stored, 1 = xz stream, 2 = brotli stream.

Usage:
  build_hkar.py --zip WebGL.zip --out assets.hkar [--jobs 2] [--limit N]
                [--loose DIR]
"""
import argparse
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zipfile
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ufs  # noqa: E402
from decompress_bundle import decompress_bundle  # noqa: E402

MAGIC = b'HKAR0001'
FOOTER = struct.Struct('<8sIIQQ')
ENTRY = struct.Struct('<HBBIQQQI')
CODEC_STORE, CODEC_XZ, CODEC_BROTLI = 0, 1, 2


def build_one(args):
    """Optionally expand a bundle's blocks, xz it, return metadata."""
    name, src, tmpdir, quality, work, transform = args
    raw_path = os.path.join(tmpdir, work + '.bundle')
    xz_path = os.path.join(tmpdir, work + '.xz')
    t0 = time.time()
    if transform:
        payload, total = decompress_bundle(src, raw_path)
    else:
        shutil.copyfile(src, raw_path)
        payload, total = os.path.getsize(src), os.path.getsize(src)
    t1 = time.time()

    crc = 0
    raw_size = 0
    with open(raw_path, 'rb') as fh, open(xz_path, 'wb') as out:
        p = subprocess.Popen(['xz', '-c', f'-{quality}', '-T1', '-q'],
                             stdin=subprocess.PIPE, stdout=out, bufsize=1 << 20)
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            crc = zlib.crc32(chunk, crc)
            raw_size += len(chunk)
            p.stdin.write(chunk)
        p.stdin.close()
        if p.wait() != 0:
            raise RuntimeError(f'xz failed for {name}')
    os.unlink(raw_path)
    comp = os.path.getsize(xz_path)
    return {
        'name': name, 'file': xz_path, 'codec': CODEC_XZ,
        'raw_size': raw_size, 'comp': comp, 'crc': crc & 0xFFFFFFFF,
        'bundle_bytes': os.path.getsize(src), 'payload': payload,
        'secs_zip': round(t1 - t0, 1), 'secs_xz': round(time.time() - t1, 1),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--zip')
    ap.add_argument('--out', required=True)
    ap.add_argument('--jobs', type=int, default=2)
    ap.add_argument('--quality', type=int, default=9)
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--only', default='')
    ap.add_argument('--loose', default='')
    ap.add_argument('--tmp', default='/tmp/hkar')
    ap.add_argument('--no-transform', action='store_true',
                    help='keep the UnityFS blocks as they are (only recompress)')
    args = ap.parse_args()

    os.makedirs(args.tmp, exist_ok=True)
    work = []

    if args.zip:
        z = zipfile.ZipFile(args.zip)
        names = sorted(z.namelist())
        if args.only:
            keep = tuple(args.only.split(','))
            names = [n for n in names if n.startswith(keep)]
        if args.limit:
            names = names[:args.limit]
        for i, n in enumerate(names):
            src = os.path.join(args.tmp, f'src{i}.bundle')
            with open(src, 'wb') as fh:
                fh.write(z.read(n))
            work.append((os.path.basename(n), src, args.tmp, args.quality, f'e{i}',
                         not args.no_transform))

    if args.loose:
        for j, fn in enumerate(sorted(os.listdir(args.loose))):
            if fn.endswith('.bundle'):
                work.append((fn, os.path.join(args.loose, fn), args.tmp,
                             args.quality, f'l{j}', not args.no_transform))
        if args.limit:
            work = work[:args.limit]

    total_src = sum(os.path.getsize(w[1]) for w in work)
    print(f'{len(work)} bundles, {total_src / 1048576:.2f} MB of bundle bytes', flush=True)

    from concurrent.futures import ProcessPoolExecutor, as_completed
    done = []
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=args.jobs) as pool:
        futs = {pool.submit(build_one, w): w[0] for w in work}
        for f in as_completed(futs):
            r = f.result()
            done.append(r)
            print(f'  {r["name"][:46]:<48} {r["bundle_bytes"] / 1048576:7.2f} -> '
                  f'{r["comp"] / 1048576:7.2f} MB '
                  f'({100 * (1 - r["comp"] / r["bundle_bytes"]):4.1f}%)  '
                  f'raw {r["raw_size"] / 1048576:7.1f}  {r["secs_xz"]:5.0f}s xz',
                  flush=True)
    done.sort(key=lambda r: r['name'])

    # ---- assemble ------------------------------------------------------
    index = bytearray()
    off = 0
    with open(args.out, 'wb') as out:
        for r in done:
            nb = r['name'].encode()
            index += ENTRY.pack(len(nb), r['codec'], 0, 0, off, r['comp'],
                                r['raw_size'], r['crc'])
            index += nb
            with open(r['file'], 'rb') as fh:
                shutil.copyfileobj(fh, out, 1 << 20)
            off += r['comp']
        index_off = off
        out.write(bytes(index))
        out.write(FOOTER.pack(MAGIC, len(done), 0, index_off, len(index)))

    size = os.path.getsize(args.out)
    stats = {
        'entries': len(done), 'bytes': size, 'bundle_bytes': total_src,
        'payload': sum(r['payload'] for r in done),
        'secs': round(time.time() - t0, 1), 'quality': args.quality,
        'entries_detail': [{k: v for k, v in r.items() if k != 'file'} for r in done],
    }
    json.dump(stats, open(args.out + '.json', 'w'), indent=1)
    print(f'\n{args.out}: {size / 1048576:.2f} MB from {total_src / 1048576:.2f} MB '
          f'bundle bytes -> saved {(total_src - size) / 1048576:.2f} MB '
          f'({100 * (1 - size / total_src):.1f}%) in {time.time() - t0:.0f}s')


if __name__ == '__main__':
    main()
