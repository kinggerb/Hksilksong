#!/usr/bin/env python3
"""Cut the embedded .mp4 cutscene media out of packed-cinematics.

The game plays StreamingAssets/VideoClip/<name>.webm on WebGL (see
`CinematicStreamingAssets.ShouldUseStreamingAssetsVideo` in the IL2CPP data),
so the .mp4 blobs that Unity embeds in the bundle's .resource node are dead
weight on the web build.

This keeps every object (VideoClip / AudioClip / MonoBehaviour) and every
asset key intact, so the Addressables catalogue and all script references
keep working.  Only the media payload of the VideoClips is dropped, while the
cutscene dialogue audio (AudioClip) is kept and re-packed.

Usage: cut_cinematics.py <bundle> <out-bundle>
"""
import struct
import sys

sys.path.insert(0, __file__.rsplit('/', 1)[0])
import unityfs as U


def find_i64_pair(data, offset, size):
    """Locate the (m_Offset, m_Size) int64 pair inside an object payload."""
    pat = struct.pack('<qq', offset, size)
    idx = data.find(pat)
    return idx


def main():
    src, dst = sys.argv[1], sys.argv[2]
    h = U.read_header(src)
    assert len(h.nodes) == 2, h.nodes
    cab_idx = 0
    res_idx = 1
    cab = U.read_node(src, h, cab_idx)
    info = U.parse_objects(cab)
    print(f'objects: {len(info["objects"])}  metadata_size={info["metadata_size"]}')

    # --- 1. work out which media blobs belong to which object -------------
    res = U.read_node(src, h, res_idx)
    print(f'resource node: {len(res)/1048576:.2f} MB')

    vids, auds = [], []
    for o in info['objects']:
        if o['type_name'] in ('VideoClip', 'AudioClip'):
            (vids if o['type_name'] == 'VideoClip' else auds).append(o)
    print(f'VideoClips={len(vids)} AudioClips={len(auds)}')

    # --- 2. read the media references out of every object ------------------
    import UnityPy
    env = UnityPy.load(open(src, 'rb').read())
    fields = {}
    for o in env.objects:
        if o.type.name == 'VideoClip':
            t = o.read_typetree()
            fields[o.path_id] = ('V', t['m_ExternalResources']['m_Offset'],
                                 t['m_ExternalResources']['m_Size'])
        elif o.type.name == 'AudioClip':
            t = o.read_typetree()
            fields[o.path_id] = ('A', t['m_Resource']['m_Offset'],
                                 t['m_Resource']['m_Size'])

    # --- 3. build the new .resource node (audio only) ----------------------
    new_res = bytearray()
    remap = {}
    for o in auds:
        kind, off, size = fields[o['path_id']]
        remap[o['path_id']] = (len(new_res), size)
        new_res += res[off:off + size]
    print(f'new resource node: {len(new_res)/1048576:.2f} MB '
          f'(was {len(res)/1048576:.2f} MB)')

    # --- 4. patch the CAB node --------------------------------------------
    cab_new = bytearray(cab)
    for o in info['objects']:
        if o['type_name'] not in ('VideoClip', 'AudioClip'):
            continue
        kind, off, size = fields[o['path_id']]
        start = o['byte_start']
        blob = cab_new[start:start + o['byte_size']]
        if kind == 'V':
            new_off, new_size = 0, 0
        else:
            new_off, new_size = remap[o['path_id']]
        pat = struct.pack('<qq', off, size)
        i = blob.find(pat)
        assert i >= 0, (o['type_name'], off, size)
        assert blob.find(pat, i + 1) < 0, 'ambiguous media reference'
        blob[i:i + 16] = struct.pack('<qq', new_off, new_size)
        cab_new[start:start + o['byte_size']] = blob
    cab_new = bytes(cab_new)

    # --- 5. write the new bundle ------------------------------------------
    payloads = [
        (h.nodes[cab_idx][3], h.nodes[cab_idx][2], cab_new),
        (h.nodes[res_idx][3], h.nodes[res_idx][2], bytes(new_res)),
    ]
    size = U.write_bundle(dst, h.version_player, h.version_engine, payloads)
    print(f'wrote {dst}: {size/1048576:.2f} MB (was {h.size/1048576:.2f} MB)')

    # --- 6. verify ---------------------------------------------------------
    env2 = UnityPy.load(open(dst, 'rb').read())
    objs = list(env2.objects)
    print(f'verified: {len(objs)} objects')
    v = [o for o in objs if o.type.name == 'VideoClip']
    a = [o for o in objs if o.type.name == 'AudioClip']
    assert len(v) == len(vids) and len(a) == len(auds)
    for o in v:
        t = o.read_typetree()
        assert t['m_ExternalResources']['m_Size'] == 0
    for o in a:
        t = o.read_typetree()
        off = t['m_Resource']['m_Offset']
        size_ = t['m_Resource']['m_Size']
        assert off + size_ <= len(new_res), (off, size_)
    print('all VideoClip media removed, all AudioClip media intact')


if __name__ == '__main__':
    main()
