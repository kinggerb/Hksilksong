#!/usr/bin/env python3
"""Per-scene size analysis of the Silksong scene bundle.

The scene bundle is ~574 CABs (one per Unity scene) plus a matching
*.sharedAssets CAB for each.  This walks the decompressed block stream,
parses only the metadata region of every node, and reports the byte cost of
every scene (both decompressed and on-disk/compressed).

Usage: tools_scene_walk.py <bundle-file-on-disk> <out.json>
"""
import sys, os, json, re, struct, collections
from array import array

from UnityPy.streams import EndianBinaryReader
from UnityPy.enums.BundleFile import ArchiveFlags, CompressionFlags
from UnityPy.enums import ClassIDType
from UnityPy.helpers import CompressionHelper
from UnityPy.helpers.TypeTreeNode import _get_blob_node_struct


def read_bundle_header(path):
    f = open(path, 'rb')
    reader = EndianBinaryReader(f)
    reader.read_string_to_null()
    version = reader.read_u_int()
    reader.read_string_to_null()
    version_engine = reader.read_string_to_null()
    reader.read_long()
    compressedSize = reader.read_u_int()
    uncompressedSize = reader.read_u_int()
    dataflags = reader.read_u_int()
    if version >= 7:
        reader.align_stream(16)
    start = reader.Position
    if dataflags & ArchiveFlags.BlocksInfoAtTheEnd:
        reader.Position = reader.Length - compressedSize
        bib = reader.read_bytes(compressedSize)
        reader.Position = start
    else:
        bib = reader.read_bytes(compressedSize)
    cf0 = CompressionFlags(dataflags & ArchiveFlags.CompressionTypeMask)
    bib = CompressionHelper.DECOMPRESSION_MAP[cf0](bib, uncompressedSize)
    bir = EndianBinaryReader(bib, offset=start)
    bir.read_bytes(16)
    n = bir.read_int()
    blocks = [(bir.read_u_int(), bir.read_u_int(), bir.read_u_short()) for _ in range(n)]
    nodesCount = bir.read_int()
    nodes = [(bir.read_long(), bir.read_long(), bir.read_u_int(), bir.read_string_to_null())
             for _ in range(nodesCount)]
    if dataflags & ArchiveFlags.BlockInfoNeedPaddingAtStart:
        reader.align_stream(16)
    boff = []
    acc = 0
    for usz, csz, fl in blocks:
        boff.append(acc); acc += usz
    data_start = reader.Position
    print(f'[hdr] unity={version_engine} blocks={len(blocks)} nodes={len(nodes)} '
          f'stream={acc/1048576:.1f}MB', file=sys.stderr)
    return path, blocks, nodes, boff, data_start, acc


def collect(path, blocks, boff, data_start, ranges, on_done=None):
    out = [bytearray() for _ in ranges]
    done = [False] * len(ranges)
    order = sorted(range(len(ranges)), key=lambda i: ranges[i][0])
    f = open(path, 'rb')
    f.seek(data_start)
    for bi in range(len(blocks)):
        usz, csz, fl = blocks[bi]
        raw = f.read(csz)
        bstart, bend = boff[bi], boff[bi] + usz
        active = False
        for i in order:
            if done[i]:
                continue
            s, e = ranges[i]
            if s >= bend:
                break
            if s < bend and e > bstart:
                active = True
                break
        if not active:
            del raw
            continue
        cfl = CompressionFlags(fl & ArchiveFlags.CompressionTypeMask)
        dec = CompressionHelper.DECOMPRESSION_MAP[cfl](raw, usz)
        del raw
        for i in order:
            if done[i]:
                continue
            s, e = ranges[i]
            if s >= bend:
                break
            if e <= bstart:
                continue
            a = max(s, bstart); b = min(e, bend)
            if b > a:
                out[i] += dec[a - bstart:b - bstart]
                if len(out[i]) >= e - s:
                    done[i] = True
                    if on_done is not None:
                        on_done(i, bytes(out[i]))
                        out[i] = bytearray()
        del dec
    f.close()
    return out


def parse_header(buf):
    r = EndianBinaryReader(bytes(buf), offset=0)
    r.endian = '>'
    r.read_u_int(); r.read_u_int()
    version = r.read_u_int()
    data_offset = r.read_u_int()
    endian = '>'
    metadata_size = 0
    if version >= 9:
        endian = '>' if r.read_boolean() else '<'
        r.read_bytes(3)
        if version >= 22:
            metadata_size = r.read_u_int(); r.read_long(); r.read_long(); r.read_long()
    r.endian = endian
    return version, endian, data_offset, metadata_size


def parse_object_table(buf, version, endian):
    r = EndianBinaryReader(bytes(buf), offset=0)
    r.endian = '>'
    r.read_u_int(); r.read_u_int(); r.read_u_int(); r.read_u_int()
    if version >= 9:
        r.read_boolean(); r.read_bytes(3)
        if version >= 22:
            r.read_u_int(); r.read_long(); r.read_long(); r.read_long()
    r.endian = endian
    if version >= 7:
        r.read_string_to_null()
    if version >= 8:
        r.read_int()
    if version >= 13:
        enable_type_tree = r.read_boolean()
    else:
        enable_type_tree = True
    type_count = r.read_int()
    types = array('i')
    for _ in range(type_count):
        class_id = r.read_int()
        if version >= 16:
            r.read_bytes(1)
        if version >= 17:
            r.read_bytes(2)
        has_script_id = (version >= 16 and class_id == 114) or (version < 16 and class_id < 0)
        if version >= 13:
            if has_script_id:
                r.read_bytes(16)
            r.read_bytes(16)
        if enable_type_tree:
            if version >= 23:
                r.read_bytes(16); r.read_int()
            if version >= 12 or version == 10:
                if version < 23:
                    node_count = r.read_int()
                    sbuf_size = r.read_int()
                    node_struct, _k = _get_blob_node_struct(r.endian, version)
                    r.read_bytes(node_struct.size * node_count)
                    r.read_bytes(sbuf_size)
            if version >= 21:
                dep_count = r.read_int()
                r.read_bytes(4 * dep_count)
        types.append(class_id)
    if 7 <= version < 14:
        r.read_int()
    object_count = r.read_int()
    type_ids = array('i'); sizes = array('i')
    for _ in range(object_count):
        if version >= 14:
            r.align_stream()
            r.read_long()
        else:
            r.read_int()
        r.read_long() if version >= 22 else r.read_u_int()
        bsize = r.read_u_int()
        tid = r.read_int()
        if version < 16:
            r.read_u_short(); r.read_u_short()
        if version in (15, 16):
            r.read_bytes(1)
        type_ids.append(tid); sizes.append(bsize)
    class_names = []
    for cid in types:
        try:
            class_names.append(ClassIDType(cid).name)
        except Exception:
            class_names.append(f'Class{cid}')
    return type_ids, sizes, class_names


def main():
    path, outpath = sys.argv[1], sys.argv[2]
    path, blocks, nodes, boff, data_start, stream_total = read_bundle_header(path)
    nodes = sorted(nodes)

    # ---- compressed (on-disk) size per node, from the block map ----
    comp_bytes = {}
    for (noff, nsize, fl, npath) in nodes:
        lo = hi = None
        for bi, (usz, csz, _f) in enumerate(blocks):
            bs, be = boff[bi], boff[bi] + usz
            if be <= noff or bs >= noff + nsize:
                continue
            ov = min(be, noff + nsize) - max(bs, noff)
            if ov > 0:
                comp_bytes[npath] = comp_bytes.get(npath, 0) + csz * ov / usz

    # ---- pass A: headers ----
    hb = collect(path, blocks, boff, data_start, [(n[0], n[0] + 65536) for n in nodes])
    meta = []
    for (noff, nsize, fl, npath), buf in zip(nodes, hb):
        try:
            version, endian, data_offset, msize = parse_header(buf)
        except Exception:
            meta.append(None); continue
        meta.append(dict(version=version, endian=endian, metadata_size=msize,
                         nsize=nsize, noff=noff, npath=npath))
    del hb
    print('[pass A] headers', file=sys.stderr)

    # ---- pass B: object tables ----
    tranges = []
    for m in meta:
        if m is None:
            tranges.append((0, 0))
        else:
            want = min(m['metadata_size'] + 256, 16 * 1024 * 1024) if m['metadata_size'] else 4 << 20
            tranges.append((m['noff'], m['noff'] + want))
    node_info = [None] * len(meta)

    def _tab(i, data):
        m = meta[i]
        if m is None:
            return
        try:
            type_ids, sizes, class_names = parse_object_table(data, m['version'], m['endian'])
        except Exception as e:
            print(f'  [tab fail] {m["npath"]}: {e}', file=sys.stderr)
            return
        types = collections.Counter(); type_bytes = collections.Counter()
        for j in range(len(sizes)):
            cn = class_names[type_ids[j]]
            types[cn] += 1
            type_bytes[cn] += sizes[j]
        node_info[i] = dict(types=dict(types), type_bytes=dict(type_bytes),
                            total=sum(type_bytes.values()), objects=len(sizes), m=m)

    collect(path, blocks, boff, data_start, tranges, on_done=_tab)
    del tranges
    print('[pass B] object tables', file=sys.stderr)

    # ---- scene name list: harvested from the first node's path table ----
    scenecabs = [(i, n) for i, n in enumerate(nodes)
                  if not n[3].endswith('.sharedAssets') and not n[3].endswith('.resource')]
    scene_names = set()
    # the global scene-path table lives in the first node of the bundle
    for n0 in nodes[:1]:
        d = bytes(collect(path, blocks, boff, data_start, [(n0[0], n0[0] + n0[1])])[0])
        for m in re.finditer(rb'Assets/Scenes/[A-Za-z0-9_/\- ]*?\.unity', d):
            s0 = m.start()
            ln = struct.unpack('<i', d[s0 - 4:s0])[0]
            if 0 < ln <= 300 and s0 - 4 + ln <= len(d):
                scene_names.add(d[s0:s0 + ln].decode('utf-8', 'replace'))
        del d
    basenames = sorted({s.split('/')[-1][:-6] for s in scene_names})
    print(f'[scenes] {len(scene_names)} scene paths, {len(basenames)} basenames', file=sys.stderr)

    # ---- pass C: identify each scene by name occurrences in its CAB ----
    cranges = [(n[0], n[0] + n[1]) for _i, n in scenecabs]
    ident = {}

    def _ident(i, data):
        _i, n = scenecabs[i]
        hits = []
        for nm in basenames:
            b_ = nm.encode()
            c = data.count(b_)
            if c:
                hits.append((-c, data.find(b_), nm))
        if hits:
            hits.sort()
            ident[n[3]] = [h[2] for h in hits]

    collect(path, blocks, boff, data_start, cranges, on_done=_ident)
    del cranges
    print(f'[pass C] identified {len(ident)} scenes', file=sys.stderr)

    # ---- aggregate ----
    cabs = {}
    for i, ni in enumerate(node_info):
        if ni is None:
            continue
        npath = ni['m']['npath']
        key = npath[:-len('.sharedAssets')] if npath.endswith('.sharedAssets') else npath
        rec = cabs.setdefault(key, {'bytes': 0, 'objects': 0, 'type_bytes': {}, 'types': {},
                                    'nodes': [], 'comp': 0.0, 'cands': []})
        rec['bytes'] += ni['total']
        rec['objects'] += ni['objects']
        rec['nodes'].append(npath)
        rec['comp'] += comp_bytes.get(npath, 0)
        for k, v in ni['type_bytes'].items():
            rec['type_bytes'][k] = rec['type_bytes'].get(k, 0) + v
        for k, v in ni['types'].items():
            rec['types'][k] = rec['types'].get(k, 0) + v
        if npath in ident:
            rec['cands'] = ident[npath]

    out = {'bundle': os.path.basename(path), 'raw_bytes': os.path.getsize(path),
           'node_count': len(nodes), 'scene_names': sorted(scene_names), 'cabs': {}}
    for cab, m in cabs.items():
        out['cabs'][cab] = {'bytes': m['bytes'], 'comp_bytes': round(m['comp']),
                            'objects': m['objects'], 'nodes': m['nodes'],
                            'candidates': m['cands'], 'type_bytes': m['type_bytes'],
                            'types': m['types']}
    with open(outpath, 'w') as fh:
        json.dump(out, fh)
    tot = sum(m['bytes'] for m in cabs.values())
    tc = sum(m['comp'] for m in cabs.values())
    print(f'{len(cabs)} cabs, total {tot/1048576:.1f}MB decompressed / {tc/1048576:.1f}MB compressed')
    for cab, m in sorted(cabs.items(), key=lambda x: -x[1]['bytes'])[:25]:
        nm = m['cands'][0] if m['cands'] else '?'
        print(f'  {m["bytes"]/1048576:>9.2f} MB raw {m["comp"]/1048576:>8.2f} MB zip  {nm}')


if __name__ == '__main__':
    main()
