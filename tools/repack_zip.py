#!/usr/bin/env python3
"""Rebuild WebGL.zip, replacing some bundles and byte-copying the rest.

Rewriting the whole archive with Python's zlib makes it ~8 MB *larger* than
the original (the shipped archive was produced with a stronger deflate), so
untouched entries are copied as raw local records and only the replaced
bundles are compressed here.

Usage: repack_zip.py <src.zip> <out.zip> name=newfile [name=newfile ...]
"""
import struct
import sys
import zlib

EOCD_SIG = b'PK\x05\x06'
CD_SIG = b'PK\x01\x02'
LFH_SIG = b'PK\x03\x04'


def read_central_directory(path):
    with open(path, 'rb') as fh:
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(max(0, size - 66000))
        tail = fh.read()
        i = tail.rfind(EOCD_SIG)
        if i < 0:
            raise ValueError('no EOCD')
        eocd = tail[i:i + 22]
        count, cd_size, cd_off = struct.unpack('<HII', eocd[10:20])
        fh.seek(cd_off)
        cd = fh.read(cd_size)
    entries = []
    p = 0
    for _ in range(count):
        if cd[p:p + 4] != CD_SIG:
            raise ValueError('bad central directory signature')
        (vmade, vneed, flags, method, mtime, mdate, crc, csize, usize,
         nlen, elen, clen, disk, iattr, eattr, lho) = struct.unpack(
            '<HHHHHHIIIHHHHHII', cd[p + 4:p + 46])
        name = cd[p + 46:p + 46 + nlen].decode('utf-8')
        entries.append(dict(name=name, vmade=vmade, vneed=vneed, flags=flags,
                            method=method, mtime=mtime, mdate=mdate, crc=crc,
                            csize=csize, usize=usize, nlen=nlen,
                            extra=cd[p + 46 + nlen:p + 46 + nlen + elen],
                            comment=cd[p + 46 + nlen + elen:p + 46 + nlen + elen + clen],
                            iattr=iattr, eattr=eattr, lho=lho))
        p += 46 + nlen + elen + clen
    return entries


def local_record(path, entry):
    """Return the raw bytes of an entry's local header + data."""
    with open(path, 'rb') as fh:
        fh.seek(entry['lho'])
        head = fh.read(30)
        if head[:4] != LFH_SIG:
            raise ValueError(f'bad local header for {entry["name"]}')
        nlen, elen = struct.unpack('<HH', head[26:30])
        fh.seek(entry['lho'])
        return fh.read(30 + nlen + elen + entry['csize'])


def make_record(entry, data, level=9):
    crc = zlib.crc32(data) & 0xFFFFFFFF
    comp = zlib.compressobj(level, zlib.DEFLATED, -15)
    payload = comp.compress(data) + comp.flush()
    name = entry['name'].encode('utf-8')
    head = struct.pack('<IHHHHHIIIHH', 0x04034b50, entry['vneed'], entry['flags'],
                       entry['method'], entry['mtime'], entry['mdate'], crc,
                       len(payload), len(data), len(name), 0)
    return head + name + payload, crc, len(payload), len(data)


def main():
    src, dst = sys.argv[1], sys.argv[2]
    repl = {}
    for arg in sys.argv[3:]:
        name, path = arg.split('=', 1)
        repl[name] = path

    entries = read_central_directory(src)
    out = open(dst, 'wb')
    cd = []
    for e in entries:
        if e['name'] in repl:
            data = open(repl[e['name']], 'rb').read()
            rec, crc, csize, usize = make_record(e, data)
            cd.append(dict(e, crc=crc, csize=csize, usize=usize, lho=out.tell(),
                           extra=b'', comment=b''))
            out.write(rec)
            print(f'  replaced {e["name"][:52]:<54} {csize/1048576:8.2f} MB')
        else:
            cd.append(dict(e, lho=out.tell()))
            out.write(local_record(src, e))
    cd_off = out.tell()
    for e in cd:
        name = e['name'].encode('utf-8')
        out.write(struct.pack('<IHHHHHHIIIHHHHHII', 0x02014b50, e['vmade'], e['vneed'],
                              e['flags'], e['method'], e['mtime'], e['mdate'],
                              e['crc'], e['csize'], e['usize'], len(name),
                              len(e['extra']), len(e['comment']), 0, e['iattr'],
                              e['eattr'], e['lho']))
        out.write(name + e['extra'] + e['comment'])
    cd_size = out.tell() - cd_off
    out.write(struct.pack('<IHHHHIIH', 0x06054b50, 0, 0, len(cd), len(cd),
                          cd_size, cd_off, 0))
    out.close()
    total = sum(e['csize'] for e in cd)
    print(f'wrote {dst}: {total/1048576:.2f} MB, {len(cd)} entries')


if __name__ == '__main__':
    main()
