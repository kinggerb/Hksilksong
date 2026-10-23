#!/usr/bin/env python3
"""Rewrite a UnityFS bundle with its data blocks stored *uncompressed*.

The logical content of the bundle is unchanged: the same bytes, in the same
order, at the same offsets inside the concatenated block data.  Unity handles
`CompressionFlags.NONE` blocks natively, so the game loads the result exactly
like the original - we only change how the payload is stored.

Why bother: the shipped bundles use LZ4HC blocks, which are fast but weak.  If
we hand the raw bytes to the archive compressor instead, it can do a much
better job on the whole 1.2 GB.

Usage:
  decompress_bundle.py IN OUT
"""
import os
import struct
import sys

import lz4.block

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ufs  # noqa: E402

BLOCK_SIZE = 0x20000        # 128 KiB, same as the originals
BIG_LONG = struct.Struct('>q')
PAD16 = lambda n: (n + 15) & ~15  # noqa: E731


def build_blocks_info(nodes, block_sizes):
    parts = [b'\x00' * 16, struct.pack('>i', len(block_sizes))]
    for usz in block_sizes:
        parts.append(struct.pack('>IIH', usz, usz, ufs.FLAG_NONE))
    parts.append(struct.pack('>i', len(nodes)))
    for noff, nsize, nflags, npath in nodes:
        parts.append(struct.pack('>qqI', noff, nsize, nflags))
        parts.append(npath.encode() + b'\x00')
    return b''.join(parts)


def decompress_bundle(src, dst):
    hdr = ufs.parse_header(src)
    nodes = hdr.nodes

    # new block table: same payload split into 128 KiB uncompressed blocks
    total = sum(b[0] for b in hdr.blocks)
    sizes = []
    left = total
    while left:
        take = min(BLOCK_SIZE, left)
        sizes.append(take)
        left -= take

    info_raw = build_blocks_info(nodes, sizes)
    info_comp = lz4.block.compress(info_raw, mode='high_compression',
                                   compression=9, store_size=False)

    flags = hdr.flags
    head = b''.join([
        ufs.SIG,
        struct.pack('>I', hdr.version),
        hdr.player.encode() + b'\x00',
        hdr.engine.encode() + b'\x00',
        struct.pack('>qIII', 0, len(info_comp), len(info_raw), flags),
    ])
    if hdr.version >= 7:
        head += b'\x00' * (PAD16(len(head)) - len(head))

    at_end = bool(flags & ufs.FLAG_INFO_AT_END)
    if at_end:
        prefix = head
        data_start = len(head)
    else:
        prefix = head + info_comp
        data_start = len(prefix)
        if flags & ufs.FLAG_PAD_AT_START:
            data_start = PAD16(data_start)
            prefix = prefix + b'\x00' * (data_start - len(prefix))

    written = 0
    buf = bytearray()
    with open(src, 'rb') as fh, open(dst, 'wb') as out:
        out.write(prefix)
        fh.seek(hdr.data_start)
        for usz, csz, codec in hdr.blocks:
            dec = ufs.decode(fh.read(csz), codec & ufs.MASK_INFO_COMPRESSION, usz)
            if len(dec) != usz:
                raise ValueError(f'block decoded to {len(dec)} not {usz}')
            buf += dec
            del dec
            while len(buf) >= BLOCK_SIZE:
                out.write(buf[:BLOCK_SIZE])
                written += BLOCK_SIZE
                del buf[:BLOCK_SIZE]
        if buf:
            out.write(buf)
            written += len(buf)
        del buf
        if at_end:
            out.write(info_comp)
        end = out.tell()
        out.seek(len(ufs.SIG) + 4 + len(hdr.player) + 1 + len(hdr.engine) + 1)
        out.write(BIG_LONG.pack(end))

    if written != total:
        raise ValueError('payload size mismatch')
    return written, end


if __name__ == '__main__':
    raw, total = decompress_bundle(sys.argv[1], sys.argv[2])
    print(f'{sys.argv[1]}: {os.path.getsize(sys.argv[1]) / 1048576:.2f} MB -> '
          f'{total / 1048576:.2f} MB (payload {raw / 1048576:.2f} MB)')
