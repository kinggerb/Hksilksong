#!/usr/bin/env python3
"""Per-asset breakdown of one Addressables bundle.

Usage: tools_analyze.py <bundle-name> <out.json>

Emits JSON:
{
  "bundle": ..., "zip_bytes":..., "raw_bytes":...,
  "resource_bytes": <size of the CAB-*.resource payload>,
  "by_type": {type: {"n":..,"bytes":..}},
  "by_folder": {folder: bytes},
  "assets": [{path, type, bytes, ...}],
  "audio": [{path, bytes, seconds, channels, freq}]
}
"""
import zipfile, json, sys, os, collections
import UnityPy

name = sys.argv[1]
outpath = sys.argv[2]

z = zipfile.ZipFile('/tmp/WebGL.zip')
info = z.getinfo(name)
data = z.read(name)

env = UnityPy.load(data)
bf = list(env.files.values())[0]

result = {
    'bundle': name,
    'zip_bytes': info.compress_size,
    'raw_bytes': info.file_size,
    'by_type': {},
    'by_folder': {},
    'assets': [],
    'audio': [],
}

# --- resource payload (streamed samples live here, outside the object table) ---
res_bytes = 0
for k, f in bf.files.items():
    if k.endswith('.resource'):
        res_bytes += len(f.get_raw_data()) if hasattr(f, 'get_raw_data') else 0
result['resource_bytes'] = res_bytes

by_type = collections.Counter()
by_type_b = collections.Counter()
by_folder = collections.Counter()

sf = None
for k, f in bf.files.items():
    if not k.endswith('.resource'):
        sf = f
        break

if sf is not None:
    for path_id, oi in sf.objects.items():
        t = oi.type.name
        b = oi.byte_size
        by_type[t] += 1
        by_type_b[t] += b
        cpath = oi.container or ''
        # folder = everything up to the file name
        folder = os.path.dirname(cpath) if cpath else '(no path)'
        by_folder[folder] += b
        rec = {'path': cpath, 'type': t, 'bytes': b}
        if t == 'AudioClip':
            try:
                ac = oi.read()
                res = getattr(ac, 'm_Resource', None)
                if res is not None:
                    rec['res_offset'] = getattr(res, 'm_Offset', 0)
                    rec['res_size'] = getattr(res, 'm_Size', 0)
                rec['channels'] = getattr(ac, 'm_Channels', 0)
                rec['freq'] = getattr(ac, 'm_Frequency', 0)
                rec['samples'] = getattr(ac, 'm_Samples', 0)
                rec['compression'] = getattr(ac, 'm_CompressionFormat', None)
                if rec['freq']:
                    rec['seconds'] = round(rec['samples'] / rec['freq'], 2)
            except Exception as e:
                rec['err'] = str(e)
            result['audio'].append(rec)
        result['assets'].append(rec)

result['by_type'] = {k: {'n': by_type[k], 'bytes': by_type_b[k]} for k in by_type}
result['by_folder'] = dict(by_folder)

with open(outpath, 'w') as f:
    json.dump(result, f)
print(f'{name}: raw {info.file_size/1048576:.1f}MB zip {info.compress_size/1048576:.1f}MB '
      f'resource {res_bytes/1048576:.1f}MB assets {len(result["assets"])}')
