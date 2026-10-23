#!/usr/bin/env python3
"""What is actually inside the addressables container?

For every bundle in the container:
  * unpack it (xz) and walk its UnityFS blocks once, streaming;
  * split it into nodes (CAB-... serialized files vs .resS/.resource payloads);
  * parse each serialized file's object table and sum byte sizes per class;
  * hash 64 KiB chunks of the decompressed bundle to find duplicate content.

Output: per-class raw bytes, per-bundle compressed bytes, and the same figures
apportioned to the bytes a player downloads (each bundle's xz size is shared
out in proportion to its raw content).
"""
import hashlib
import json
import lzma
import os
import struct
import sys
import time
from collections import Counter, defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'Hksilksong', 'tools'))
import ufs  # noqa: E402
from UnityPy.files import SerializedFile  # noqa: E402
from UnityPy.files.ObjectReader import ObjectReader  # noqa: E402
from UnityPy.streams import EndianBinaryReader  # noqa: E402


class _SkippedAssetBundle:
    """Stand-in when the AssetBundle object's data is not in the prefix we read.

    SerializedFile parses the AssetBundle object during __init__ just to get the
    container list; the object lives near the end of the payload, which we are
    not reading.  Only the object table is needed here."""
    m_PreloadTable = []
    m_Container = []


_orig_parse = ObjectReader.parse_as_object


def _tolerant_parse(self, *a, **k):
    try:
        return _orig_parse(self, *a, **k)
    except Exception:  # noqa: BLE001
        return _SkippedAssetBundle()


ObjectReader.parse_as_object = _tolerant_parse

CONTAINER = '/tmp/assets.hkar'
WORK = '/tmp/ana/cur.bundle'
PREFIX = 32 << 20        # bytes of a node we need to see the object table
CHUNK = 64 << 10

FOOT = struct.Struct('<8sIIQQ')
ENT = struct.Struct('<HBBIQQQI')


def container_index(path):
    size = os.path.getsize(path)
    with open(path, 'rb') as fh:
        fh.seek(size - 32)
        magic, count, _r, index_off, index_len = FOOT.unpack(fh.read(32))
        assert magic == b'HKAR0001'
        fh.seek(index_off)
        blob = fh.read(index_len)
    out, p = {}, 0
    for _ in range(count):
        nl, codec, _a, _b, off, comp, raw, crc = ENT.unpack_from(blob, p)
        p += ENT.size
        name = blob[p:p + nl].decode()
        p += nl
        out[name] = dict(off=off, comp=comp, raw=raw, codec=codec)
    return out


def parse_node(prefix, label):
    """Object table of one serialized file, or None."""
    try:
        sf = SerializedFile(EndianBinaryReader(prefix))
        classes = Counter()
        counts = Counter()
        for obj in sf.objects.values():
            name = obj.type.name
            classes[name] += obj.byte_size
            counts[name] += 1
        return classes, counts
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


def parse_node_isolated(prefix):
    """Parse one serialized file's object table in a forked child.

    UnityPy's typetree reader is a C extension: on one of the scene bundles it
    segfaults on a truncated buffer, and a segfault cannot be caught.  Forking
    costs a few ms and turns "the whole report dies" into "this node is
    unclassified".
    """
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        try:
            out = parse_node(prefix, '')
            payload = json.dumps([dict(out[0]) if out[0] else None,
                                  dict(out[1]) if isinstance(out[1], Counter) else out[1]])
        except Exception as exc:  # noqa: BLE001
            payload = json.dumps([None, str(exc)[:80]])
        try:
            os.write(w, payload.encode())
        finally:
            os.close(w)
            os._exit(0)
    os.close(w)
    chunks = []
    while True:
        b = os.read(r, 1 << 16)
        if not b:
            break
        chunks.append(b)
    os.close(r)
    _pid, status = os.waitpid(pid, 0)
    if not chunks or os.WIFSIGNALED(status):
        return None, f'signal {os.WTERMSIG(status)}' if os.WIFSIGNALED(status) else 'no output'
    classes, counts = json.loads(b''.join(chunks))
    if classes is None:
        return None, counts
    return Counter(classes), Counter(counts)


def walk_bundle(path):
    """Stream a UnityFS bundle once and describe where its bytes go."""
    hdr = ufs.parse_header(path)
    nodes = sorted(hdr.nodes, key=lambda n: n[0])
    classes = Counter()
    counts = Counter()
    kinds = Counter()          # 'serialized' / 'resource'
    unparsed = []
    buf = bytearray()
    base = 0                   # file offset of buf[0]
    pos = 0
    ni = 0
    with open(path, 'rb') as fh:
        fh.seek(hdr.data_start)
        for _i, (usz, csz, codec) in enumerate(hdr.blocks):
            blk = ufs.decode(fh.read(csz), codec & ufs.MASK_INFO_COMPRESSION, usz)
            if not blk:
                continue
            buf += blk
            del blk
            pos += usz
            # do every node whose prefix we now have in full
            while ni < len(nodes):
                noff, nsize, _f, name = nodes[ni]
                want_end = noff + min(nsize, PREFIX)
                if pos < want_end:
                    break
                start = noff - base
                stop = start + min(nsize, PREFIX)
                if nsize and not (name.endswith('.resS') or name.endswith('.resource')):
                    got, cnt = parse_node_isolated(bytes(buf[start:stop]))
                    if got is None:
                        unparsed.append((name, cnt))
                    else:
                        classes.update(got)
                        counts.update(cnt)
                        kinds['serialized'] += nsize
                else:
                    kinds['resource'] += nsize
                ni += 1
            # drop what no future node needs
            if ni < len(nodes):
                keep_from = max(0, nodes[ni][0] - base)
            else:
                keep_from = len(buf)
            keep_from = min(keep_from, len(buf))
            if keep_from > 0:
                del buf[:keep_from]
                base += keep_from
    for noff, nsize, _f, name in nodes[ni:]:      # ran out of data
        if name.endswith('.resS') or name.endswith('.resource'):
            kinds['resource'] += nsize
        else:
            kinds['serialized'] += nsize
            unparsed.append((name, 'past end of blocks'))
    return dict(classes=classes, counts=counts, kinds=kinds,
                unparsed=unparsed, payload=sum(b[0] for b in hdr.blocks))


def dup_chunks(path):
    seen = set()
    dup = 0
    total = 0
    with open(path, 'rb') as fh:
        while True:
            b = fh.read(CHUNK)
            if not b:
                break
            total += len(b)
            if len(b) < CHUNK:
                break
            h = hashlib.blake2b(b, digest_size=16).digest()
            if h in seen:
                dup += len(b)
            else:
                seen.add(h)
    return total, dup, len(seen)


def main():
    idx = container_index(CONTAINER)
    names = sorted(idx)
    if len(sys.argv) > 1:
        keep = tuple(sys.argv[1].split(','))
        names = [n for n in names if n.startswith(keep)]

    report = {}
    t0 = time.time()
    for name in names:
        e = idx[name]
        with open(CONTAINER, 'rb') as fh:
            fh.seek(e['off'])
            blob = fh.read(e['comp'])
        with open(WORK, 'wb') as out:
            out.write(lzma.decompress(blob))
        del blob
        info = walk_bundle(WORK)
        total, dup, uniq = dup_chunks(WORK)
        info['dup_bytes'] = dup
        info['dup_chunks'] = total // CHUNK - uniq
        info['comp'] = e['comp']
        info['raw_bundle'] = e['raw']
        report[name] = info
        top = ', '.join(f'{k} {v >> 20}MB' for k, v in info['classes'].most_common(3))
        print(f'{name[:46]:<48} comp {e["comp"]/1048576:7.1f} '
              f'payload {info["payload"]/1048576:7.0f}MB  dup {dup/1048576:6.1f}MB  {top}',
              flush=True)
        if info['unparsed']:
            print(f'    !! {len(info["unparsed"])} nodes not parsed: {info["unparsed"][:2]}', flush=True)
        with open('/tmp/ana/compose.json', 'w') as fh:
            json.dump({k: dict(classes=dict(v['classes']), counts=dict(v['counts']),
                               kinds=dict(v['kinds']), unparsed=v['unparsed'][:10],
                               payload=v['payload'], comp=v['comp'],
                               raw_bundle=v['raw_bundle'], dup_bytes=v['dup_bytes'],
                               dup_chunks=v['dup_chunks']) for k, v in report.items()},
                      fh, indent=1)

    json.dump({k: dict(classes=dict(v['classes']), counts=dict(v['counts']),
                       kinds=dict(v['kinds']), unparsed=v['unparsed'][:10],
                       payload=v['payload'], comp=v['comp'], raw_bundle=v['raw_bundle'],
                       dup_bytes=v['dup_bytes'], dup_chunks=v['dup_chunks'])
               for k, v in report.items()},
              open('/tmp/ana/compose.json', 'w'), indent=1)
    print(f'\n{len(report)} bundles in {time.time() - t0:.0f}s')


if __name__ == '__main__':
    main()
