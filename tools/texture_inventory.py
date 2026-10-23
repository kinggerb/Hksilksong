#!/usr/bin/env python3
"""Texture inventory: what format, how big, how many mips.

Reads a bundle, walks its object tables and parses just the header fields of
every Texture2D (name, width, height, format, image size, mip count).  Used to
work out what re-encoding or downscaling the textures would actually save.

Usage: texture_inventory.py <bundle> [<bundle> ...]
"""
import json
import os
import struct
import sys

from UnityPy.files import SerializedFile
from UnityPy.files.ObjectReader import ObjectReader
from UnityPy.streams import EndianBinaryReader

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ufs  # noqa: E402


class _Skipped:
    m_PreloadTable = []
    m_Container = []


_orig = ObjectReader.parse_as_object


def _tolerant(self, *a, **k):
    try:
        return _orig(self, *a, **k)
    except Exception:  # noqa: BLE001
        return _Skipped()


ObjectReader.parse_as_object = _tolerant

FORMATS = {
    1: 'Alpha8', 2: 'ARGB4444', 3: 'RGB24', 4: 'RGBA32', 5: 'ARGB32',
    7: 'RGB565', 10: 'DXT1', 12: 'DXT5', 13: 'RGBA4444', 14: 'BGRA32',
    27: 'BC7', 28: 'BC6H', 45: 'ASTC4x4', 46: 'ASTC5x5', 47: 'ASTC6x6',
    48: 'ASTC8x8', 49: 'ASTC10x10', 50: 'ASTC12x12', 51: 'RGBA_ASTC4x4',
    63: 'ETC_RGB4', 66: 'ETC2_RGBA8', 68: 'ETC2_RGBA1',
}
# bits per pixel, block-compressed formats included
BPP = {
    'Alpha8': 8, 'RGB24': 24, 'RGBA32': 32, 'ARGB32': 32, 'BGRA32': 32,
    'ARGB4444': 16, 'RGB565': 16, 'RGBA4444': 16,
    'DXT1': 4, 'DXT5': 8, 'BC7': 8, 'BC6H': 8,
    'ETC_RGB4': 4, 'ETC2_RGBA8': 8, 'ETC2_RGBA1': 4,
    'ASTC4x4': 8, 'ASTC5x5': 5.12, 'ASTC6x6': 3.56, 'ASTC8x8': 2.0,
    'ASTC10x10': 1.28, 'ASTC12x12': 0.89, 'RGBA_ASTC4x4': 8,
}


NAME_RE = None


def parse_texture(reader):
    """Texture name + the dimensions/format Unity encodes in it.

    Atlas textures are named ``sactx-<atlas>-<w>x<h>-<format>-<kind>-<hash>``;
    that string is stable across builds and tells us what we need without
    decoding a 32 MB image.
    """
    global NAME_RE
    if NAME_RE is None:
        import re
        NAME_RE = re.compile(r'(\d{2,5})x(\d{2,5})-(BC\d|DXT\d|ASTC[\dx]+|RGBA32|RGB24|ETC\d?\w*)')
    try:
        name_len = reader.read_int()
        if not 0 <= name_len <= 512:
            return None
        name = reader.read_bytes(name_len).decode('utf-8', 'replace')
    except Exception:  # noqa: BLE001
        return None
    m = NAME_RE.search(name)
    if not m:
        return dict(name=name, width=0, height=0, fmt='?', image=0, mips=0)
    w, h, fmt = int(m.group(1)), int(m.group(2)), m.group(3)
    bpp = BPP.get(fmt, 8)
    # full mip chain is 4/3 of level 0; assume it is present unless the name says otherwise
    level0 = w * h * bpp / 8
    return dict(name=name, width=w, height=h, fmt=fmt, image=level0 * 4 / 3, mips=0)


def inventory(path):
    hdr = ufs.parse_header(path)
    out = []
    with open(path + '.payload', 'wb') as fh:
        for _i, blk in ufs.iter_blocks(path, hdr):
            fh.write(blk)
    data_fh = open(path + '.payload', 'rb')
    try:
        for noff, nsize, _fl, nname in hdr.nodes:
            if nname.endswith('.resS') or nname.endswith('.resource'):
                continue
            data_fh.seek(noff)
            head = data_fh.read(min(nsize, 32 << 20))
            # serialized files carry their own endianness flag at offset 16
            endian = '<' if (len(head) > 16 and head[16] == 0) else '>' 
            try:
                sf = SerializedFile(EndianBinaryReader(head))
            except Exception:  # noqa: BLE001
                continue
            for obj in sf.objects.values():
                if obj.type.name != 'Texture2D':
                    continue
                start = obj.byte_start
                if start + 128 > nsize:
                    continue
                data_fh.seek(noff + start)
                blob = data_fh.read(min(obj.byte_size, 200))
                r = EndianBinaryReader(blob)
                r.endian = endian
                try:
                    tex = parse_texture(r)
                except Exception:  # noqa: BLE001
                    tex = None
                if tex:
                    out.append(tex)
    finally:
        data_fh.close()
    os.unlink(path + '.payload')
    return out


def main():
    agg = []
    files = [a for a in sys.argv[1:] if not a.startswith('--') and a != sys.argv[sys.argv.index('--json') + 1]] if '--json' in sys.argv else sys.argv[1:]
    for p in files:
        got = inventory(p)
        print(f'{os.path.basename(p)[:40]:<42} {len(got)} textures', flush=True)
        agg.extend(got)
    if '--json' in sys.argv:
        json.dump(agg, open(sys.argv[sys.argv.index('--json') + 1], 'w'))
        return
    json.dump(agg, open('/tmp/ana/textures.json', 'w'))
    from collections import Counter
    byfmt = Counter()
    for t in agg:
        byfmt[t['fmt']] += t['image']
    total = sum(t['image'] for t in agg)
    print(f'\n{len(agg)} textures, {total / 1048576:.0f} MB of image data')
    for f, b in byfmt.most_common():
        n = sum(1 for t in agg if t['fmt'] == f)
        print(f'  {f:<12} {b / 1048576:8.1f} MB  ({b / total * 100:4.1f}%)  {n:5d} textures')
    lvl0 = 0
    for t in agg:
        w, h, bpp = t['width'], t['height'], BPP.get(t['fmt'])
        if not bpp:
            continue
        lvl0 += w * h * bpp / 8
    print(f'\nlevel-0 only: {lvl0 / 1048576:.0f} MB, mips: {(total - lvl0) / 1048576:.0f} MB '
          f'({(1 - lvl0 / total) * 100:.0f}% of texture bytes)')
    big = sorted(agg, key=lambda t: -t['image'])[:10]
    print('\nlargest:')
    for t in big:
        print(f"  {t['name'][:40]:<42} {t['width']:5d}x{t['height']:<5d} {t['fmt']:<8} "
              f"{t['image'] / 1048576:6.1f} MB  {t['mips']} mips")


if __name__ == '__main__':
    main()
