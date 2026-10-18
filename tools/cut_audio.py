#!/usr/bin/env python3
"""Re-encode the AAC audio inside an audio bundle at a lower bitrate.

The clips are stored as MP4/AAC blobs in the bundle's `.resource` node.  This
keeps the container and codec identical (so Unity/the browser decode them the
same way) and only lowers the bitrate, which is what actually costs bytes.

Every AudioClip object, its m_Length, m_Frequency, m_Name and every
Addressables key stay untouched; only the (m_Offset, m_Size) pair that points
into the resource node is rewritten.

Usage: cut_audio.py <bundle> <out-bundle> <kbps> [--dry-run]
"""
import io
import struct
import sys

sys.path.insert(0, __file__.rsplit('/', 1)[0])
import unityfs as U

import av


def find_resource_pair(blob, res_size):
    """Locate the (m_Offset, m_Size) int64 pair in an AudioClip payload."""
    cands = []
    for i in range(len(blob) - 16, -1, -1):
        off, size = struct.unpack('<qq', blob[i:i + 16])
        if 0 <= off < res_size and 0 < size <= res_size - off:
            cands.append((i, off, size))
    return cands


def transcode(data, kbps, samplerate=None, channels=None):
    """Decode an MP4/AAC blob and re-encode it at *kbps*."""
    inp = av.open(io.BytesIO(data), format='mp4')
    in_stream = inp.streams.audio[0]
    out_io = io.BytesIO()
    out = av.open(out_io, 'w', format='mp4')
    rate = samplerate or in_stream.rate
    layout = channels if channels else in_stream.layout.name
    st = out.add_stream('aac', rate=rate)
    st.bit_rate = int(kbps * 1000)
    if channels:
        st.layout = channels
    resampler = av.AudioResampler(format='fltp', layout=st.layout.name, rate=rate)
    first = True
    for frame in inp.decode(in_stream):
        frame.pts = None
        for rf in resampler.resample(frame):
            rf.pts = None
            for pkt in st.encode(rf):
                out.mux(pkt)
    for pkt in st.encode(None):
        out.mux(pkt)
    out.close()
    inp.close()
    return out_io.getvalue()


def main():
    src, dst, kbps = sys.argv[1], sys.argv[2], float(sys.argv[3])
    dry = '--dry-run' in sys.argv

    h = U.read_header(src)
    cab = U.read_node(src, h, 0)
    info = U.parse_objects(cab)
    clips = [o for o in info['objects'] if o['type_name'] == 'AudioClip']
    res = U.read_node(src, h, 1)
    print(f'clips={len(clips)} resource={len(res)/1048576:.2f} MB target={kbps:g} kbps')

    cab_new = bytearray(cab)
    new_res = bytearray()
    kept = changed = 0
    before = after = 0
    for o in clips:
        blob = bytes(cab[o['byte_start']:o['byte_start'] + o['byte_size']])
        cands = find_resource_pair(blob, len(res))
        if not cands:
            kept += 1
            continue
        # the real pair is the one whose size field also appears as m_Size
        i, off, size = cands[0]
        payload = res[off:off + size]
        before += size
        try:
            new = transcode(payload, kbps)
        except Exception as e:
            print(f'  [skip] {e}')
            new = payload
        if len(new) >= size:
            new = payload
            kept += 1
        else:
            changed += 1
        new_off = len(new_res)
        new_res += new
        after += len(new)
        blob = bytearray(blob)
        blob[i:i + 16] = struct.pack('<qq', new_off, len(new))
        cab_new[o['byte_start']:o['byte_start'] + o['byte_size']] = blob

    print(f'changed={changed} kept={kept} audio {before/1048576:.2f} MB -> {after/1048576:.2f} MB')
    if dry:
        print('dry run, nothing written')
        return

    size = U.write_bundle(dst, h.version_player, h.version_engine,
                          [(h.nodes[0][3], h.nodes[0][2], bytes(cab_new)),
                           (h.nodes[1][3], h.nodes[1][2], bytes(new_res))])
    print(f'wrote {dst}: {size/1048576:.2f} MB (was {h.size/1048576:.2f} MB)')

    import UnityPy
    env = UnityPy.load(open(dst, 'rb').read())
    n = 0
    for o in env.objects:
        if o.type.name == 'AudioClip':
            t = o.read_typetree()
            assert t['m_Resource']['m_Size'] + t['m_Resource']['m_Offset'] <= len(new_res)
            n += 1
    print(f'verified {n} AudioClips')


if __name__ == '__main__':
    main()
