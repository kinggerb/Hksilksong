#!/usr/bin/env python3
"""Minimal, dependency-light UnityFS (asset bundle) reader.

UnityPy's reader is used elsewhere in this repo, but it dislikes some files
that have perfectly valid headers, so the archive pipeline uses this one:
plain `struct` plus `lz4.block` / `lzma`, nothing else.  Everything streams,
so multi-hundred-MB bundles never have to be held in memory.

UnityFS layout we care about::

    char   signature[9] = "UnityFS\\0"
    u32    version            (8)
    string minPlayerVersion
    string minEngineVersion
    i64    size               total file size
    u32    compressedBlocksInfoSize
    u32    uncompressedBlocksInfoSize
    u32    flags
    pad    to 16 if version >= 7
    <blocks data>
    <blocksInfo: i32 blockCount, {u32 usz, u32 csz, u16 flags}*, i32 nodeCount,
                  {i64 offset, i64 size, u32 flags, string path}*, 16 zero bytes>

All integers are big-endian.
"""
import hashlib
import lz4.block
import lzma
import os
import struct

SIG = b'UnityFS\0'
FLAG_LZMA, FLAG_LZ4, FLAG_LZ4HC, FLAG_NONE = 1, 2, 3, 0
MASK_INFO_COMPRESSION = 0x3F
FLAG_INFO_AT_END = 0x80
FLAG_PAD_AT_START = 0x200


def _lzma_decomp(data, usize):
    props, dict_size = struct.unpack('<BI', data[:5])
    lc = props % 9
    rem = props // 9
    pb, lp = rem // 5, rem % 5
    dec = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=[{
        'id': lzma.FILTER_LZMA1, 'dict_size': dict_size,
        'lc': lc, 'lp': lp, 'pb': pb}])
    return dec.decompress(data[5:])


def decode(data, codec, usize):
    if codec == FLAG_NONE:
        return data
    if codec == FLAG_LZ4 or codec == FLAG_LZ4HC:
        return lz4.block.decompress(data, uncompressed_size=usize)
    if codec == FLAG_LZMA:
        return _lzma_decomp(data, usize)
    raise ValueError(f'unsupported compression flag {codec}')


class Header:
    __slots__ = ('version', 'player', 'engine', 'size', 'flags', 'blocks',
                 'nodes', 'data_start', 'file_size')


def _read_cstr(buf, pos):
    end = buf.index(b'\0', pos)
    return buf[pos:end].decode('utf-8', 'replace'), end + 1


def parse_header(path):
    with open(path, 'rb') as fh:
        head = fh.read(4096)
        fh.seek(0, os.SEEK_END)
        file_size = fh.tell()
    if not head.startswith(SIG):
        raise ValueError(f'{path}: not a UnityFS bundle')
    h = Header()
    h.file_size = file_size
    pos = len(SIG)
    h.version, = struct.unpack_from('>I', head, pos)
    pos += 4
    h.player, pos = _read_cstr(head, pos)
    h.engine, pos = _read_cstr(head, pos)
    h.size, csize, usize, h.flags = struct.unpack_from('>qIII', head, pos)
    pos += 20
    if h.version >= 7:
        pos = (pos + 15) & ~15

    if h.flags & FLAG_INFO_AT_END:
        info_at = file_size - csize
        data_start = pos
    else:
        info_at = pos
        data_start = pos + csize
        if h.flags & FLAG_PAD_AT_START:
            data_start = (data_start + 15) & ~15
    h.data_start = data_start
    with open(path, 'rb') as fh:
        fh.seek(info_at)
        blob = fh.read(csize)
    info = decode(blob, h.flags & MASK_INFO_COMPRESSION, usize)

    p = 16  # 16 bytes of padding at the start of the blocksInfo blob
    n, = struct.unpack_from('>i', info, p)
    p += 4
    h.blocks = [struct.unpack_from('>IIH', info, p + 10 * i) for i in range(n)]
    p += 10 * n
    nn, = struct.unpack_from('>i', info, p)
    p += 4
    nodes = []
    for _ in range(nn):
        off, sz, nflags = struct.unpack_from('>qqI', info, p)
        p += 20
        name, p = _read_cstr(info, p)
        nodes.append((off, sz, nflags, name))
    h.nodes = nodes
    return h


def iter_blocks(path, hdr, chunk=8 << 20):
    """Yield (index, decompressed block bytes) in order."""
    with open(path, 'rb') as fh:
        fh.seek(hdr.data_start)
        for i, (usz, csz, codec) in enumerate(hdr.blocks):
            raw = fh.read(csz)
            blk = decode(raw, codec & MASK_INFO_COMPRESSION, usz)
            if len(blk) != usz:
                raise ValueError(f'block {i}: {len(blk)} != {usz}')
            yield i, blk


def iter_node(path, hdr, index, chunk=8 << 20):
    noff, nsize, _f, _n = hdr.nodes[index]
    end = noff + nsize
    buf = bytearray()
    pos = 0
    for _i, blk in iter_blocks(path, hdr):
        b0, b1 = pos, pos + len(blk)
        pos = b1
        if b1 <= noff or b0 >= end:
            continue
        buf += blk[max(b0, noff) - b0:min(b1, end) - b0]
        del blk
        while len(buf) >= chunk:
            yield bytes(buf[:chunk])
            del buf[:chunk]
    if buf:
        yield bytes(buf)


def digest_node(path, hdr, index):
    h = hashlib.sha256()
    n = 0
    for piece in iter_node(path, hdr, index, chunk=8 << 20):
        h.update(piece)
        n += len(piece)
    return n, h.hexdigest()


def verify(path):
    """(size, per-node [(name, size, sha256)], total_payload)"""
    hdr = parse_header(path)
    out = []
    for i, (_o, _s, _f, name) in enumerate(hdr.nodes):
        n, d = digest_node(path, hdr, i)
        out.append((name, n, d))
    total = sum(b[0] for b in hdr.blocks)
    return hdr.size, out, total


if __name__ == '__main__':
    import sys
    for p in sys.argv[1:]:
        size, nodes, total = verify(p)
        print(f'{p}  size={size}  payload={total}  blocks-ok')
        for name, n, d in nodes:
            print(f'   {name:<40} {n:>12} {d[:16]}')
