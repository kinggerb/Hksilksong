#!/usr/bin/env python3
"""Drop the non-Latin (CJK / Japanese / Korean / Cyrillic / Indic) fonts.

Game text in the Latin alphabet is untouched: the Noto Sans static faces,
Trajan Pro, Perpetua and Amor Serif - and every object, pathID and
Addressables key - are kept exactly as they are.  Only the *source font data*
and SDF atlases for scripts outside the Latin alphabet are emptied, so a
player who selects one of those languages gets blank glyphs instead of
glyphs (no crash: the assets still exist and still load).

Usage: cut_fonts.py <bundle> <out-bundle>
"""
import sys

sys.path.insert(0, __file__.rsplit('/', 1)[0])
import unityfs as U

PLACEHOLDER = 8
RGBA32 = 4

DROP_PREFIXES = (
    'Assets/Fonts/NotoSerifCJKsc',
    'Assets/Fonts/chinese',
    'Assets/Fonts/japanese',
    'Assets/Fonts/korean',
    'Assets/Fonts/Russian',
    'Assets/Fonts/Hannari',
    'Assets/Fonts/UtsukushiFONT',
    'Assets/Fonts/Noto_Sans/Noto_Sans_Bengali',
    'Assets/Fonts/Noto_Sans/Noto_Sans_Khmer',
    'Assets/Fonts/Noto_Sans/Noto_Sans_Thai',
)


def is_dropped(path):
    return any(path.startswith(p) for p in DROP_PREFIXES)


def main():
    src, dst = sys.argv[1], sys.argv[2]
    import UnityPy

    h = U.read_header(src)
    node_idx = 0
    node = U.read_node(src, h, node_idx)
    info = U.parse_objects(node)

    env = UnityPy.load(open(src, 'rb').read())
    patches = {}
    dropped = kept = 0
    before = after = 0
    for o in env.objects:
        path = o.container
        if isinstance(path, dict):
            path = ''
        path = path or ''
        if not is_dropped(path):
            kept += 1
            continue
        if o.type.name == 'Texture2D':
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
            patches[o.path_id] = blob
            after += len(blob)
            dropped += 1
        elif o.type.name == 'Font':
            t = o.read_typetree()
            before += o.byte_size
            t['m_FontData'] = b''
            blob = o.save_typetree(t)
            patches[o.path_id] = blob
            after += len(blob)
            dropped += 1
    print(f'kept {kept} objects, dropped {dropped} non-Latin assets')
    print(f'{before/1048576:.1f} MB -> {after/1024:.1f} kB')

    new_node, new_size = U.patch_node(src, h, node_idx, patches)
    size = U.write_bundle(dst, h.version_player, h.version_engine,
                          [(h.nodes[node_idx][3], h.nodes[node_idx][2], new_node)])
    print(f'wrote {dst}: {size/1048576:.2f} MB (was {h.size/1048576:.2f} MB)')

    env2 = UnityPy.load(open(dst, 'rb').read())
    n = 0
    latin_ok = 0
    leftover = 0
    for o in env2.objects:
        if o.type.name == 'Font':
            t = o.read_typetree()
            p = o.container if not isinstance(o.container, dict) else ''
            if is_dropped(p or ''):
                leftover += len(t['m_FontData'])
            elif t['m_FontData']:
                latin_ok += 1
            n += 1
    assert leftover == 0, leftover
    print(f'verified: {len(list(env2.objects))} objects, {n} fonts, '
          f'{latin_ok} Latin fonts still carry their font data')


if __name__ == '__main__':
    main()
