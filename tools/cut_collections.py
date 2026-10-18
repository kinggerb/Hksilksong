#!/usr/bin/env python3
"""Replace the Collection (bestiary) gallery textures with tiny placeholders.

Every Texture2D in packed-collections is a 4096x4096 (16 MB) or 8192x4096
(32 MB) atlas used only by the in-game Collection screen.  Each one is
rewritten as an 8x8 RGBA32 placeholder while keeping the object, its name,
its pathID, all Sprite references and every Addressables key untouched - so
nothing that loads this bundle can fail; the gallery just shows blank tiles.

Usage: cut_collections.py <bundle> <out-bundle>
"""
import sys

sys.path.insert(0, __file__.rsplit('/', 1)[0])
import unityfs as U

PLACEHOLDER = 8
RGBA32 = 4


def main():
    src, dst = sys.argv[1], sys.argv[2]

    # UnityPy needs an mmap-backed block stream for this 334 MB bundle
    import tools_bigbundle  # noqa: F401  (installs the streaming read_fs)
    import UnityPy

    h = U.read_header(src)
    node_idx = 0
    node = U.read_node_range(src, h, node_idx, 0, 8 << 20)
    info = U.parse_objects(node)
    data_offset = info['data_offset']
    print(f'objects={len(info["objects"])} metadata={info["metadata_size"]}')

    env = UnityPy.load(open(src, 'rb').read())
    new_payload = {}
    before = after = 0
    for o in env.objects:
        if o.type.name != 'Texture2D':
            continue
        t = o.read_typetree()
        before += o.byte_size
        data = bytes(PLACEHOLDER * PLACEHOLDER * 4)
        t['m_Width'] = PLACEHOLDER
        t['m_Height'] = PLACEHOLDER
        t['m_CompleteImageSize'] = len(data)
        t['m_TextureFormat'] = RGBA32
        t['m_MipCount'] = 1
        t['m_MipsStripped'] = 0
        t['image data'] = data
        t['m_StreamData']['path'] = ''
        t['m_StreamData']['offset'] = 0
        t['m_StreamData']['size'] = 0
        blob = o.save_typetree(t)
        # pad so every following object keeps its 8-byte alignment
        pad = (-len(blob)) % 8
        new_payload[o.path_id] = (blob + b'\x00' * pad, len(blob))
        after += len(blob)
    print(f'{len(new_payload)} textures: {before/1048576:.1f} MB -> {after/1024:.0f} kB')

    # ---- new layout: identical order, sizes shifted by the deltas ----------
    objs = sorted(info['objects'], key=lambda x: x['byte_start'])
    delta = {}
    shift = 0
    layout = []
    for o in objs:
        new_start = o['byte_start'] + shift
        if o['path_id'] in new_payload:
            padded, real = new_payload[o['path_id']]
            new_size = len(padded)
            layout.append((o, new_start, real))
            shift += new_size - o['byte_size']
        else:
            layout.append((o, new_start, o['byte_size']))

    # ---- patch the metadata: object table byte_start + header file_size ----
    meta = bytearray(U.read_node_range(src, h, node_idx, 0, data_offset))
    for o, new_start, nsz in layout:
        # the table stores the offset *relative* to data_offset
        meta[o['bs_field']:o['bs_field'] + 8] = (new_start - data_offset).to_bytes(8, 'little')
        if o['path_id'] in new_payload:
            meta[o['bs_field'] + 8:o['bs_field'] + 12] = nsz.to_bytes(4, 'little')
    new_file_size = h.nodes[node_idx][1] + shift
    # SerializedFile v22 header layout:
    #   0..8 zero, 8..12 version, 12..16 zero, 16..20 endian, 20..24 metadata_size,
    #   24..32 file_size, 32..40 data_offset, 40..48 unknown
    # NB: the 48-byte SerializedFile header is always big-endian; only the
    # metadata that follows it uses the file's endian flag.
    meta[24:32] = (new_file_size).to_bytes(8, 'big')

    reps = [(0, data_offset, bytes(meta))]
    for o, new_start, _nsz in layout:
        if o['path_id'] in new_payload:
            reps.append((o['byte_start'], o['byte_start'] + o['byte_size'],
                         new_payload[o['path_id']][0]))

    new_node = U.rewrite_node(src, h, node_idx, reps)
    print(f'new node {len(new_node)/1048576:.2f} MB (was {h.nodes[node_idx][1]/1048576:.2f} MB)')
    assert len(new_node) == new_file_size, (len(new_node), new_file_size)

    size = U.write_bundle(dst, h.version_player, h.version_engine,
                          [(h.nodes[node_idx][3], h.nodes[node_idx][2], new_node)])
    print(f'wrote {dst}: {size/1048576:.2f} MB (was {h.size/1048576:.2f} MB)')

    # ---- verify ------------------------------------------------------------
    env2 = UnityPy.load(open(dst, 'rb').read())
    n = 0
    for o in env2.objects:
        if o.type.name == 'Texture2D':
            t = o.read_typetree()
            assert (t['m_Width'], t['m_Height']) == (PLACEHOLDER, PLACEHOLDER), t['m_Name']
            assert t['m_CompleteImageSize'] == PLACEHOLDER * PLACEHOLDER * 4, t['m_Name']
            assert t['image data'] == bytes(256), t['m_Name']
            n += 1
    print(f'verified {n} textures at {PLACEHOLDER}x{PLACEHOLDER}, '
          f'{len(list(env2.objects))} objects total')


if __name__ == '__main__':
    main()
