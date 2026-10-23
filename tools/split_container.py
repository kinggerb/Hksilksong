#!/usr/bin/env python3
"""Split a .hkar container into the fixed-size parts the loader downloads.

The downloader fetches `assets.hkar.partN` in parallel and writes each part at
its own offset, so the parts only have to be byte ranges of the container in
order.  Prints the `partBytes` array that index.html needs.

Usage: split_container.py assets.hkar StreamingAssets/aa [--part-size 16777216]
"""
import argparse
import json
import os


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('archive')
    ap.add_argument('outdir')
    ap.add_argument('--part-size', type=int, default=16 << 20)
    ap.add_argument('--prefix', default='assets.hkar')
    args = ap.parse_args()

    total = os.path.getsize(args.archive)
    n = (total + args.part_size - 1) // args.part_size
    sizes = []
    with open(args.archive, 'rb') as src:
        for i in range(n):
            want = min(args.part_size, total - i * args.part_size)
            path = os.path.join(args.outdir, f'{args.prefix}.part{i + 1}')
            with open(path, 'wb') as out:
                left = want
                while left:
                    chunk = src.read(min(left, 1 << 20))
                    if not chunk:
                        raise SystemExit(f'truncated while writing {path}')
                    out.write(chunk)
                    left -= len(chunk)
            sizes.append(want)
            print(f'  {path}  {want} bytes')

    js = (f"new Array({n - 1}).fill({args.part_size}).concat([{sizes[-1]}])"
          if n > 1 else f"[{total}]")
    print(f'\n{n} parts, {total} bytes')
    print('partBytes: ' + js)
    json.dump({'parts': n, 'total': total, 'part_size': args.part_size,
               'sizes': sizes}, open(os.path.join(args.outdir, args.prefix + '.json'), 'w'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
