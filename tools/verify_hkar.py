#!/usr/bin/env python3
"""Check a .hkar container end to end.

For every entry: locate it in the file, xz-decode it, verify the stored CRC32
and length, and parse the result as a UnityFS bundle (header, block table,
node table, block sizes adding up).  Anything that would make Unity reject the
bundle shows up here.

Usage: verify_hkar.py assets.hkar [--sample N]
"""
import argparse
import hashlib
import lzma
import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ufs  # noqa: E402

MAGIC = b'HKAR0001'
BLOCK_NAMES = {0: 'stored', 1: 'lzma', 2: 'lz4', 3: 'lz4hc'}
FOOTER = struct.Struct('<8sIIQQ')
ENTRY = struct.Struct('<HBBIQQQI')


def read_index(path):
    size = os.path.getsize(path)
    with open(path, 'rb') as fh:
        fh.seek(size - 32)
        magic, count, _r, index_off, index_len = FOOTER.unpack(fh.read(32))
        if magic != MAGIC:
            raise SystemExit(f'bad magic {magic!r}')
        fh.seek(index_off)
        blob = fh.read(index_len)
    entries, p = [], 0
    for _ in range(count):
        nl, codec, _a, _b, off, comp, raw, crc = ENTRY.unpack_from(blob, p)
        p += ENTRY.size
        name = blob[p:p + nl].decode()
        p += nl
        entries.append(dict(name=name, codec=codec, off=off, comp=comp,
                            raw=raw, crc=crc))
    return entries, size, index_off, index_len


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('archive')
    ap.add_argument('--sample', type=int, default=0, help='only check N entries')
    ap.add_argument('--tmp', default='/tmp/hkar_verify')
    args = ap.parse_args()

    entries, size, index_off, index_len = read_index(args.archive)
    print(f'{args.archive}: {size / 1048576:.2f} MB, {len(entries)} entries, '
          f'index {index_len} B at {index_off}')
    declared = sum(e['comp'] for e in entries)
    if declared != index_off:
        raise SystemExit(f'payload size mismatch: entries sum to {declared}, index at {index_off}')

    os.makedirs(args.tmp, exist_ok=True)
    todo = entries if not args.sample else entries[:args.sample]
    total_raw = total_comp = 0
    problems = []
    for e in todo:
        with open(args.archive, 'rb') as fh:
            fh.seek(e['off'])
            blob = fh.read(e['comp'])
        dec = lzma.decompress(blob)
        total_comp += len(blob)
        total_raw += len(dec)
        bad = []
        if len(dec) != e['raw']:
            bad.append(f"size {len(dec)} != {e['raw']}")
        if zlib.crc32(dec) & 0xFFFFFFFF != e['crc']:
            bad.append('crc mismatch')
        try:
            tmp = os.path.join(args.tmp, 'one.bundle')
            with open(tmp, 'wb') as fh:
                fh.write(dec)
            hdr = ufs.parse_header(tmp)
            blocks = sum(b[0] for b in hdr.blocks)
            nodes = sum(n[1] for n in hdr.nodes)
            if blocks != nodes:
                bad.append(f'block bytes {blocks} != node bytes {nodes}')
            if hdr.size != len(dec):
                bad.append(f"header size {hdr.size} != file {len(dec)}")
            codecs = sorted({b[2] & 0x3F for b in hdr.blocks})
            info = (f'{len(hdr.blocks)} blocks ({", ".join(BLOCK_NAMES.get(c, str(c)) for c in codecs)}), '
                    f'{len(hdr.nodes)} nodes, {blocks / 1048576:.1f} MB')
        except Exception as exc:  # noqa: BLE001
            bad.append(f'unityfs: {exc}')
            info = 'unparsable'
        print(f"  {e['name'][:56]:<58} {e['comp'] / 1048576:7.2f} -> "
              f"{len(dec) / 1048576:7.2f} MB  {info}"
              + ('  <-- ' + '; '.join(bad) if bad else '  ok'), flush=True)
        if bad:
            problems.append((e['name'], bad))

    print(f'\nchecked {len(todo)} entries: {total_comp / 1048576:.2f} MB compressed, '
          f'{total_raw / 1048576:.2f} MB of bundles')
    if problems:
        print('FAILED:')
        for n, b in problems:
            print(' ', n, b)
        return 1
    print('all entries decode, match their CRC and parse as UnityFS bundles')
    return 0


if __name__ == '__main__':
    sys.exit(main())
