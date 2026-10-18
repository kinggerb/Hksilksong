#!/usr/bin/env python3
"""Low-memory per-asset analysis of a giant UnityFS bundle.

UnityPy's BundleFile.read_fs does `b"".join(all decompressed blocks)`, which
needs ~2x the bundle size in RAM.  The scenes bundle is 481 MB, so that blows
up a small box.  This patches read_fs to spill the decompressed blocks to a
temp file and hand back an mmap-backed reader, then reuses UnityPy's normal
SerializedFile parsing on top of it.

Usage: tools_bigbundle.py <bundle-file-on-disk> <out.json>
"""
import sys, os, json, mmap, tempfile, collections

import UnityPy
from UnityPy import config as up_config
up_config.SERIALIZED_FILE_PARSE_TYPETREE = False
from UnityPy.files.BundleFile import BundleFile, BlockInfo, DirectoryInfoFS
from UnityPy.streams import EndianBinaryReader
from UnityPy.enums.BundleFile import ArchiveFlags, ArchiveFlagsOld
from UnityPy.helpers import CompressionHelper


def read_fs_streaming(self, reader):
    """Same as BundleFile.read_fs but the decompressed blocks land in a temp
    file that is mmap'd, so RAM stays flat regardless of bundle size."""
    size = reader.read_long()
    compressedSize = reader.read_u_int()
    uncompressedSize = reader.read_u_int()
    dataflagsValue = reader.read_u_int()

    version = self.parse_version()
    if (
        version < (2020,)
        or (version[0] == 2020 and version < (2020, 3, 34))
        or (version[0] == 2021 and version < (2021, 3, 2))
        or (version[0] == 2022 and version < (2022, 1, 1))
    ):
        self.dataflags = ArchiveFlagsOld(dataflagsValue)
    else:
        self.dataflags = ArchiveFlags(dataflagsValue)

    if self.version >= 7 or (version[0] == 2019 and version >= (2019, 4, 15)):
        reader.align_stream(16)
        self._uses_block_alignment = True

    start = reader.Position
    if self.dataflags & ArchiveFlags.BlocksInfoAtTheEnd:
        reader.Position = reader.Length - compressedSize
        blocksInfoBytes = reader.read_bytes(compressedSize)
        reader.Position = start
    else:
        blocksInfoBytes = reader.read_bytes(compressedSize)

    blocksInfoBytes = self.decompress_data(blocksInfoBytes, uncompressedSize, self.dataflags)
    blocksInfoReader = EndianBinaryReader(blocksInfoBytes, offset=start)

    blocksInfoReader.read_bytes(16)
    blocksInfoCount = blocksInfoReader.read_int()
    m_BlocksInfo = [
        BlockInfo(
            blocksInfoReader.read_u_int(),
            blocksInfoReader.read_u_int(),
            blocksInfoReader.read_u_short(),
        )
        for _ in range(blocksInfoCount)
    ]

    nodesCount = blocksInfoReader.read_int()
    m_DirectoryInfo = [
        DirectoryInfoFS(
            blocksInfoReader.read_long(),
            blocksInfoReader.read_long(),
            blocksInfoReader.read_u_int(),
            blocksInfoReader.read_string_to_null(),
        )
        for _ in range(nodesCount)
    ]

    if m_BlocksInfo:
        self._block_info_flags = m_BlocksInfo[0].flags

    if isinstance(self.dataflags, ArchiveFlags) and self.dataflags & ArchiveFlags.BlockInfoNeedPaddingAtStart:
        reader.align_stream(16)

    tmp = tempfile.NamedTemporaryFile(prefix="unityblk_", suffix=".bin", delete=False)
    try:
        for i, blockInfo in enumerate(m_BlocksInfo):
            tmp.write(
                self.decompress_data(
                    reader.read_bytes(blockInfo.compressedSize),
                    blockInfo.uncompressedSize,
                    blockInfo.flags,
                    i,
                )
            )
        tmp.flush()
        sz = tmp.tell()
        mm = mmap.mmap(tmp.fileno(), 0, access=mmap.ACCESS_READ)
        tmp.close()
        blocksReader = EndianBinaryReader(memoryview(mm), offset=blocksInfoReader.real_offset())
        print(f'[stream] decompressed {sz/1048576:.1f}MB in {len(m_BlocksInfo)} blocks '
              f'-> {tmp.name}', file=sys.stderr)
    except Exception:
        tmp.close()
        raise

    return m_DirectoryInfo, blocksReader


BundleFile.read_fs = read_fs_streaming


def _analyse(path, outpath):
    """Entry point used by both the CLI and importers."""
    env = UnityPy.load(path)
    bf = list(env.files.values())[0]
    if not hasattr(bf, 'files'):
        raise SystemExit('not a bundle')

    result = {
        'bundle': os.path.basename(path),
        'raw_bytes': os.path.getsize(path),
        'by_type': {},
        'by_folder': {},
        'assets': [],
        'nodes': [],
    }

    by_type = collections.Counter()
    by_type_b = collections.Counter()
    by_folder = collections.Counter()
    assets = []

    for key, f in bf.files.items():
        if key.endswith('.resource'):
            result['nodes'].append({'path': key, 'size': f.header.file_size if hasattr(f, 'header') else 0})
            continue
        result['nodes'].append({'path': key, 'size': f.header.file_size if hasattr(f, 'header') else 0})
        container = {}
        try:
            for p, ptr in f.container.items():
                container[ptr.m_PathID] = p
        except Exception as e:
            print('container err', e, file=sys.stderr)
        for path_id, oi in f.objects.items():
            t = oi.type.name
            b = oi.byte_size
            by_type[t] += 1
            by_type_b[t] += b
            cpath = container.get(path_id, '')
            assets.append({'path': cpath, 'type': t, 'bytes': b})
            folder = os.path.dirname(cpath) if cpath else '(unpathed)'
            by_folder[folder] += b

    result['by_type'] = {k: {'n': by_type[k], 'bytes': by_type_b[k]} for k in by_type}
    result['by_folder'] = dict(by_folder)
    result['assets'] = assets
    result['object_count'] = len(assets)

    with open(outpath, 'w') as fh:
        json.dump(result, fh)

    print(f'objects {len(assets)}  nodes {result["nodes"]}', file=sys.stderr)
    for t, c in by_type.most_common(15):
        print(f'   {t:<26} n={c:<7} {by_type_b[t]/1048576:>9.2f} MB')


if __name__ == '__main__':
    _analyse(sys.argv[1], sys.argv[2])
