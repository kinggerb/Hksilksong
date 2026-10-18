#!/usr/bin/env python3
"""Low-memory UnityFS (Unity asset bundle) reader / writer.

UnityPy's own BundleFile reader concatenates every decompressed block in
memory, which OOMs on the multi-hundred-MB bundles in this project.  This
module streams blocks instead and can write a bundle back out in the exact
same container format (UnityFS v8, LZ4HC blocks, blocksInfo at the end).

Only the parts of the format that this project needs are implemented.
"""
import os
import struct

from UnityPy.enums.BundleFile import ArchiveFlags, CompressionFlags
from UnityPy.helpers import CompressionHelper
from UnityPy.streams import EndianBinaryReader, EndianBinaryWriter

# container flags used by the shipped bundles:
#   0x003  LZ4HC compression for the block table
#   0x040  blocks + directory info combined
#   0x080  block table stored at the end of the file
#   0x200  data starts on a 16 byte boundary
DATA_FLAGS = 0x243
BLOCK_FLAG = 3  # LZ4HC
CHUNK_SIZE = 0x20000  # 128 KiB, matches the original bundles


class BundleHeader:
    __slots__ = ('version', 'version_player', 'version_engine', 'size',
                 'dataflags', 'blocks', 'nodes', 'data_start', 'block_offsets')

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def read_header(path):
    """Parse the UnityFS header of *path* without decompressing any block."""
    with open(path, 'rb') as fh:
        reader = EndianBinaryReader(fh)
        signature = reader.read_string_to_null()
        if signature != 'UnityFS':
            raise ValueError(f'not a UnityFS bundle: {signature!r}')
        version = reader.read_u_int()
        version_player = reader.read_string_to_null()
        version_engine = reader.read_string_to_null()
        size = reader.read_long()
        compressed_size = reader.read_u_int()
        uncompressed_size = reader.read_u_int()
        dataflags = reader.read_u_int()
        if version >= 7:
            reader.align_stream(16)
        start = reader.Position
        if dataflags & ArchiveFlags.BlocksInfoAtTheEnd:
            reader.Position = reader.Length - compressed_size
            bib = reader.read_bytes(compressed_size)
            reader.Position = start
        else:
            bib = reader.read_bytes(compressed_size)
        bib = CompressionHelper.DECOMPRESSION_MAP[
            CompressionFlags(dataflags & ArchiveFlags.CompressionTypeMask)](bib, uncompressed_size)
        bir = EndianBinaryReader(bib, offset=start)
        bir.read_bytes(16)
        n = bir.read_int()
        blocks = [(bir.read_u_int(), bir.read_u_int(), bir.read_u_short()) for _ in range(n)]
        nodes_count = bir.read_int()
        nodes = [(bir.read_long(), bir.read_long(), bir.read_u_int(), bir.read_string_to_null())
                 for _ in range(nodes_count)]
        if dataflags & ArchiveFlags.BlockInfoNeedPaddingAtStart:
            reader.align_stream(16)
        data_start = reader.Position

    offs = []
    acc = 0
    for usz, _csz, _fl in blocks:
        offs.append((acc, acc + usz))
        acc += usz
    return BundleHeader(version=version, version_player=version_player,
                         version_engine=version_engine, size=size,
                         dataflags=dataflags, blocks=blocks, nodes=nodes,
                         data_start=data_start, block_offsets=offs)


def iter_node_chunks(path, header, node_index, chunk=4 << 20):
    """Yield the decompressed content of one node in *chunk* sized pieces.

    Only one chunk is held in memory at a time, so multi-GB nodes are fine.
    """
    noff, nsize, _nflags, _npath = header.nodes[node_index]
    end = noff + nsize
    with open(path, 'rb') as fh:
        fh.seek(header.data_start)
        buf = bytearray()
        for bi, (usz, csz, fl) in enumerate(header.blocks):
            bstart, bend = header.block_offsets[bi]
            if bend <= noff or bstart >= end:
                fh.seek(csz, 1)
                continue
            raw = fh.read(csz)
            dec = CompressionHelper.DECOMPRESSION_MAP[CompressionFlags(fl & 0x3F)](raw, usz)
            del raw
            lo = max(bstart, noff)
            hi = min(bend, end)
            buf += dec[lo - bstart:hi - bstart]
            del dec
            while len(buf) >= chunk:
                yield bytes(buf[:chunk])
                del buf[:chunk]
    if buf:
        yield bytes(buf)


def read_node(path, header, node_index):
    """Whole node content as bytes (use only for small nodes)."""
    return b''.join(iter_node_chunks(path, header, node_index))


def write_bundle(out_path, version_player, version_engine, node_payloads,
                 dataflags=DATA_FLAGS, block_flag=BLOCK_FLAG):
    """Write a UnityFS bundle.

    node_payloads: iterable of (path, node_flags, bytes)
    """
    node_payloads = list(node_payloads)
    data = b''.join(p[2] for p in node_payloads)

    file_data, block_info = CompressionHelper.chunk_based_compress(data, block_flag)

    w = EndianBinaryWriter()
    w.write_bytes(b'\x00' * 0x10)
    w.write_int(len(block_info))
    for usz, csz, fl in block_info:
        w.write_u_int(usz)
        w.write_u_int(csz)
        w.write_u_short(fl)
    w.write_int(len(node_payloads))
    off = 0
    for path, nflags, blob in node_payloads:
        w.write_long(off)
        w.write_long(len(blob))
        w.write_u_int(nflags)
        w.write_string_to_null(path)
        off += len(blob)
    block_data = w.bytes
    w.dispose()

    uncompressed_block_size = len(block_data)
    switch = dataflags & 0x3F
    if switch in CompressionHelper.COMPRESSION_MAP:
        block_data = CompressionHelper.COMPRESSION_MAP[switch](block_data)
    else:
        raise NotImplementedError(f'no compression function for {switch}')

    out = EndianBinaryWriter()
    out.write_string_to_null('UnityFS')
    out.write_u_int(8)
    out.write_string_to_null(version_player)
    out.write_string_to_null(version_engine)
    header_pos = out.Position
    out.write_long(0)
    out.write_u_int(len(block_data))
    out.write_u_int(uncompressed_block_size)
    out.write_u_int(dataflags)
    out.align_stream(16)
    if dataflags & 0x80:
        if dataflags & 0x200:
            out.align_stream(16)
        out.write(file_data)
        out.write(block_data)
    else:
        out.write(block_data)
        if dataflags & 0x200:
            out.align_stream(16)
        out.write(file_data)
    end = out.Position
    out.Position = header_pos
    out.write_long(end)
    out.Position = end
    data_out = out.bytes
    out.dispose()
    with open(out_path, 'wb') as fh:
        fh.write(data_out)
    return len(data_out)


# --------------------------------------------------------------------------
# SerializedFile (the "node") helpers
# --------------------------------------------------------------------------
from UnityPy.enums import ClassIDType
from UnityPy.helpers.TypeTreeNode import _get_blob_node_struct


def parse_serialized_header(buf):
    """(version, endian, metadata_size) of a SerializedFile header."""
    r = EndianBinaryReader(bytes(buf), offset=0)
    r.endian = '>'
    r.read_u_int(); r.read_u_int()
    version = r.read_u_int()
    r.read_u_int()
    endian = '>'
    metadata_size = 0
    if version >= 9:
        endian = '>' if r.read_boolean() else '<'
        r.read_bytes(3)
        if version >= 22:
            metadata_size = r.read_u_int()
            r.read_long(); r.read_long(); r.read_long()
    return version, endian, metadata_size


class _Shim:
    """Minimal stand-in for SerializedFile, enough for SerializedType /
    ObjectReader to work without materialising the whole file."""
    __slots__ = ('header', 'types', 'big_id_enabled', 'target_platform',
                 '_enable_type_tree', 'version', 'env')

    def __init__(self, header):
        self.header = header
        self.types = []
        self.big_id_enabled = 0
        self.target_platform = 0
        self._enable_type_tree = True
        self.version = header.version
        self.env = None


def parse_objects(node_bytes, want_container=False):
    """Parse a SerializedFile node.

    Returns a dict with:
      version, endian, metadata_size, data_offset,
      objects: list of dicts(path_id, byte_start, byte_size, type_id, type_name)
    `byte_start` is absolute within the node, i.e. it already includes the
    SerializedFile data_offset, so ``node[byte_start:byte_start+byte_size]``
    is the object payload.
    """
    from UnityPy.files.ObjectReader import ObjectReader
    from UnityPy.files.SerializedFile import SerializedFile, SerializedType

    r = EndianBinaryReader(bytes(node_bytes), offset=0)
    r.endian = '>'
    r.read_u_int(); r.read_u_int()
    version = r.read_u_int()
    r.read_u_int()
    endian = '>'
    metadata_size = 0
    data_offset = 0
    if version >= 9:
        endian = '>' if r.read_boolean() else '<'
        r.read_bytes(3)
        if version >= 22:
            metadata_size = r.read_u_int()
            r.read_long()
            data_offset = r.read_long()
            r.read_long()
    r.endian = endian

    class _Header:
        pass
    header = _Header()
    header.version = version
    header.endian = endian
    header.metadata_size = metadata_size
    header.data_offset = data_offset

    shim = _Shim(header)
    if version >= 7:
        r.read_string_to_null()
    if version >= 8:
        r.read_int()
    shim._enable_type_tree = r.read_boolean() if version >= 13 else True
    type_count = r.read_int()
    shim.types = [SerializedType(r, shim, False) for _ in range(type_count)]
    if 7 <= version < 14:
        shim.big_id_enabled = r.read_int()
    object_count = r.read_int()
    objects = []
    for _ in range(object_count):
        if version >= 14:
            r.align_stream()
        entry_field = r.Position
        # path_id (int64) immediately follows, then byte_start
        bs_field = entry_field + (8 if version >= 14 else 4)
        o = ObjectReader.from_reader(shim, r)
        objects.append({
            'path_id': o.path_id,
            'byte_start': o.byte_start,
            'byte_size': o.byte_size,
            'type_id': o.type_id,
            'type_name': o.type.name,
            'entry_field': entry_field,
            'bs_field': bs_field,
        })
    return {
        'version': version, 'endian': endian,
        'metadata_size': metadata_size, 'data_offset': data_offset,
        'objects': objects,
    }


def rewrite_node(src, header, node_index, replacements):
    """Return the node's bytes with `replacements` applied.

    replacements: list of (start, end, new_bytes) with absolute node offsets.
    The replacement list may be huge (hundreds of MB) in total, but only one
    block is decompressed at a time; the result is assembled in memory.
    """
    noff, nsize, _f, _p = header.nodes[node_index]
    node_end = noff + nsize
    reps = sorted(replacements)
    out = bytearray()
    ri = 0
    covered_to = noff
    with open(src, 'rb') as fh:
        fh.seek(header.data_start)
        for bi, (usz, csz, fl) in enumerate(header.blocks):
            bstart, bend = header.block_offsets[bi]
            if bend <= noff or bstart >= node_end:
                fh.seek(csz, 1)
                continue
            raw = fh.read(csz)
            dec = CompressionHelper.DECOMPRESSION_MAP[
                CompressionFlags(fl & 0x3F)](raw, usz)
            del raw
            seg_lo = max(bstart, noff)
            seg_hi = min(bend, node_end)
            pos = max(seg_lo, covered_to)
            while pos < seg_hi:
                while ri < len(reps) and reps[ri][1] <= pos:
                    ri += 1
                if ri < len(reps) and reps[ri][0] <= pos:
                    rs, re_, rb = reps[ri]
                    if pos <= rs:
                        out += rb
                        covered_to = re_
                        pos = re_
                    ri += 1
                    continue
                nxt = reps[ri][0] if ri < len(reps) else seg_hi
                stop = min(seg_hi, nxt)
                if stop <= pos:
                    stop = seg_hi
                out += dec[pos - bstart:stop - bstart]
                pos = stop
            del dec
    return bytes(out)


def read_node_range(src, header, node_index, start, length):
    """Read [start, start+length) of a node (offsets relative to node start).

    Streams blocks and keeps only the wanted bytes, so it works on nodes that
    are far larger than RAM.
    """
    noff, nsize, _f, _p = header.nodes[node_index]
    lo = noff + start
    hi = min(noff + start + length, noff + nsize)
    out = bytearray()
    with open(src, 'rb') as fh:
        fh.seek(header.data_start)
        for bi, (usz, csz, fl) in enumerate(header.blocks):
            bstart, bend = header.block_offsets[bi]
            if bend <= lo or bstart >= hi:
                fh.seek(csz, 1)
                continue
            raw = fh.read(csz)
            dec = CompressionHelper.DECOMPRESSION_MAP[
                CompressionFlags(fl & 0x3F)](raw, usz)
            del raw
            a = max(bstart, lo); b = min(bend, hi)
            if b > a:
                out += dec[a - bstart:b - bstart]
            del dec
            if len(out) >= hi - lo:
                break
    return bytes(out)


def patch_node(src, header, node_index, patches, align=8):
    """Return (new_node_bytes, new_node_size) with object payloads replaced.

    patches: {path_id: new_payload_bytes}.  Object order, every byte_size and
    every byte_start in the object table are rewritten so the SerializedFile
    stays consistent.
    """
    node = read_node(src, header, node_index)
    info = parse_objects(node)
    data_offset = info['data_offset']
    objs = sorted(info['objects'], key=lambda x: x['byte_start'])

    layout = []
    shift = 0
    for o in objs:
        new_start = o['byte_start'] + shift
        if o['path_id'] in patches:
            blob = patches[o['path_id']]
            padded = len(blob) + (-len(blob)) % align
            layout.append((o, new_start, len(blob), padded))
            shift += padded - o['byte_size']
        else:
            layout.append((o, new_start, o['byte_size'], o['byte_size']))

    meta = bytearray(node[:data_offset])
    for o, new_start, size, _padded in layout:
        # relative to data_offset, little-endian (the file's endian flag)
        meta[o['bs_field']:o['bs_field'] + 8] = (new_start - data_offset).to_bytes(8, 'little')
        if o['path_id'] in patches:
            meta[o['bs_field'] + 8:o['bs_field'] + 12] = size.to_bytes(4, 'little')
    new_size = header.nodes[node_index][1] + shift
    # the 48-byte SerializedFile header is always big-endian
    meta[24:32] = new_size.to_bytes(8, 'big')

    reps = [(0, data_offset, bytes(meta))]
    for o, new_start, _size, padded in layout:
        if o['path_id'] in patches:
            blob = patches[o['path_id']]
            reps.append((o['byte_start'], o['byte_start'] + o['byte_size'],
                         blob + b'\x00' * (padded - len(blob))))
    return rewrite_node(src, header, node_index, reps), new_size
