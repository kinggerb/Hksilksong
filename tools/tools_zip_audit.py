#!/usr/bin/env python3
"""Read the central directory of StreamingAssets/aa/WebGL.zip (split across 100
.partN files) without materialising the 2 GB archive."""
import os, re, struct, sys, json
from collections import defaultdict

D = 'StreamingAssets/aa'
parts = {}
for f in os.listdir(D):
    m = re.match(r'WebGL\.zip\.part(\d+)$', f)
    if m:
        parts[int(m.group(1))] = os.path.join(D, f)
keys = sorted(parts)
sizes = [os.path.getsize(parts[k]) for k in keys]
offsets = []
acc = 0
for s in sizes:
    offsets.append(acc)
    acc += s
TOTAL = acc


class SpanFile:
    """File-like view over the concatenated parts."""
    def __init__(self):
        self.pos = 0

    def _map(self, pos):
        # binary search over offsets
        lo, hi = 0, len(keys) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if offsets[mid] <= pos:
                lo = mid
            else:
                hi = mid - 1
        return lo, pos - offsets[lo]

    def seek(self, pos, whence=0):
        if whence == 0:
            self.pos = pos
        elif whence == 1:
            self.pos += pos
        else:
            self.pos = TOTAL + pos
        return self.pos

    def tell(self):
        return self.pos

    def read(self, n=-1):
        if n < 0:
            n = TOTAL - self.pos
        out = bytearray()
        remaining = n
        while remaining > 0:
            idx, inner = self._map(self.pos)
            if inner >= sizes[idx]:
                # sitting exactly on a part boundary -> move to next part
                idx += 1
                if idx >= len(keys):
                    break
                inner = 0
            f = open(parts[keys[idx]], 'rb')
            f.seek(inner)
            chunk = f.read(min(remaining, sizes[idx] - inner))
            f.close()
            if not chunk:
                break
            out += chunk
            self.pos += len(chunk)
            remaining -= len(chunk)
        return bytes(out)


f = SpanFile()

# --- locate End of Central Directory ---
f.seek(max(0, TOTAL - 65557))
tail = f.read(min(65557, TOTAL))
eocd = tail.rfind(b'PK\x05\x06')
if eocd < 0:
    sys.exit('no EOCD found')
cd_size, cd_off = struct.unpack('<II', tail[eocd + 12:eocd + 20])
n_entries = struct.unpack('<H', tail[eocd + 10:eocd + 12])[0]
print(f'# entries={n_entries}  central dir @ {cd_off} size {cd_size}', file=sys.stderr)

f.seek(cd_off)
cd = f.read(cd_size)

entries = []
p = 0
while p < len(cd) and cd[p:p + 4] == b'PK\x01\x02':
    (ver, vneed, flags, method, mtime, mdate, crc, csize, usize,
     nlen, elen, clen, dstart, iattr, eattr, lho) = struct.unpack('<HHHHHHIIIHHHHHII', cd[p + 4:p + 46])
    name = cd[p + 46:p + 46 + nlen].decode('utf-8', 'replace')
    entries.append(dict(name=name, csize=csize, usize=usize, method=method,
                        lho=lho, csize32=csize))
    p += 46 + nlen + elen + clen

out = {
    'total_zip_bytes': TOTAL,
    'total_zip_mb': round(TOTAL / 1048576, 2),
    'entries': entries,
}
with open('zip_audit.json', 'w') as fh:
    json.dump(out, fh)

# --- report ---
print(f'\n{"name":<70} {"compressed":>12} {"uncompressed":>14}')
tot_c = tot_u = 0
for e in sorted(entries, key=lambda x: -x['csize']):
    tot_c += e['csize']
    tot_u += e['usize']
    print(f'{e["name"]:<70} {e["csize"]/1048576:>11.2f}M {e["usize"]/1048576:>13.2f}M')
print(f'\nTOTAL: {len(entries)} entries, compressed {tot_c/1048576:.2f} MB, uncompressed {tot_u/1048576:.2f} MB')
