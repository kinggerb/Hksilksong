#!/usr/bin/env python3
"""Sanity-check every bundle in the rebuilt WebGL.zip.

Each bundle must still parse as UnityFS, every node must parse as a
SerializedFile, and the object counts are compared with a baseline JSON so a
cut can never silently drop objects.

Usage: verify_zip.py <zip> [baseline.json] [--write-baseline]
"""
import json
import os
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import unityfs as U


def bundle_stats(path):
    h = U.read_header(path)
    stats = {'size': os.path.getsize(path), 'nodes': [], 'objects': 0}
    for i, node in enumerate(h.nodes):
        if node[3].endswith(('.resource', '.resS')):
            stats['nodes'].append({'name': node[3], 'size': node[1],
                                   'objects': 0, 'raw': True})
            continue
        try:
            node_bytes = U.read_node_range(path, h, i, 0, min(node[1], 32 << 20))
            info = U.parse_objects(node_bytes)
            stats['objects'] += len(info['objects'])
            stats['nodes'].append({'name': node[3], 'size': node[1],
                                   'objects': len(info['objects'])})
        except Exception as e:
            stats['nodes'].append({'name': node[3], 'size': node[1],
                                   'error': str(e)[:120]})
    return stats


def main():
    zpath = sys.argv[1]
    baseline_path = None
    write_baseline = '--write-baseline' in sys.argv
    for a in sys.argv[2:]:
        if not a.startswith('--'):
            baseline_path = a

    baseline = {}
    if baseline_path and os.path.exists(baseline_path):
        baseline = json.load(open(baseline_path))

    z = zipfile.ZipFile(zpath)
    result = {}
    total = 0
    print(f'{"bundle":<46} {"MB":>9} {"objects":>9}  nodes')
    for zi in z.infolist():
        total += zi.compress_size
        with tempfile.NamedTemporaryFile(suffix='.bundle', delete=False) as tmp:
            tmp.write(z.read(zi.filename))
            tmp_path = tmp.name
        try:
            st = bundle_stats(tmp_path)
        finally:
            os.remove(tmp_path)
        result[zi.filename] = st
        errs = [n for n in st['nodes'] if 'error' in n]
        print(f'{zi.filename[:44]:<46} {zi.compress_size/1048576:>9.2f} '
              f'{st["objects"]:>9}  {len(st["nodes"])}'
              + ('  ERRORS!' if errs else ''))
        if errs:
            for e in errs:
                print('    ', e['name'], e['error'])
    print(f'TOTAL {total/1048576:.2f} MB, {len(z.infolist())} entries')

    if baseline:
        ok = True
        for name, st in result.items():
            base = baseline.get(name)
            if not base:
                print(f'  [new] {name}')
                continue
            if st['objects'] != base['objects']:
                print(f'  [OBJECT COUNT CHANGED] {name}: {base["objects"]} -> {st["objects"]}')
                ok = False
            if any('error' in n for n in base['nodes']):
                continue
            if len(st['nodes']) != len(base['nodes']):
                print(f'  [NODE COUNT CHANGED] {name}')
                ok = False
        print('baseline check:', 'OK' if ok else 'PROBLEMS')
    if write_baseline and baseline_path:
        json.dump(result, open(baseline_path, 'w'), indent=1)
        print('baseline written to', baseline_path)


if __name__ == '__main__':
    main()
