#!/usr/bin/env python3
# Vygeneruje patches/manifest.json a patches/<addon>/<verzia>/<názov>.patch
# zo zdrojového adresára záplat (predvolene ~/kontrola-TCL/nastroje/zaplaty).
#
# Očakáva adresáre <addon_id>-<verzia>/ so súbormi <názov> (naša záplata)
# a <názov>.orig (originál). Súbory s inou príponou (napr. *.orig-zaplata-<dátum>)
# sa ignorujú. Nová záplata = pridať adresár do zaplaty/, doplniť TARGETS a spustiť.
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import patcher  # noqa: E402

DEFAULT_SRC = os.path.expanduser('~/kontrola-TCL/nastroje/zaplaty')

# (addon_id, názov súboru) -> (cesta v doplnku, popis)
TARGETS = {
    ('plugin.video.youtube', 'http_server.py'): (
        'resources/lib/youtube_plugin/kodion/network/http_server.py',
        'YouTube proxy: server_close zatvára počúvajúci socket a dokončené handlery sa uvoľňujú'),
    ('plugin.video.youtube', 'provider.py'): (
        'resources/lib/youtube_plugin/youtube/provider.py',
        'YouTube tokeny: zrušený refresh token zneplatní len svoj slot, nie všetky tri'),
    ('service.sc.helper', 'service.py'): (
        'service.py',
        'SC helper: session expiruje po 12 h namiesto 30 min (reconnect po pauze už nedá 404)'),
}


def read(path):
    with open(path, 'rb') as f:
        return f.read()


def main(src):
    out_dir = os.path.join(ROOT, 'patches')
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir)
    entries = []
    for d in sorted(os.listdir(src)):
        full = os.path.join(src, d)
        if not os.path.isdir(full) or '-' not in d:
            continue
        addon_id, version = d.rsplit('-', 1)
        for name in sorted(os.listdir(full)):
            if name.endswith('.orig') or '.orig' in name:
                continue
            orig_path = os.path.join(full, name + '.orig')
            if not os.path.isfile(orig_path):
                continue
            if (addon_id, name) not in TARGETS:
                sys.exit('chýba záznam v TARGETS pre %s / %s' % (addon_id, name))
            rel_path, popis = TARGETS[(addon_id, name)]
            orig, new = read(orig_path), read(os.path.join(full, name))
            diff = patcher.make_unified(orig, new, rel_path)
            # overenie hneď pri generovaní: diff musí dať presne našu záplatu
            if patcher.apply_unified(orig, diff) != new:
                sys.exit('diff pre %s / %s nedáva pôvodnú záplatu' % (addon_id, name))
            rel_patch = '%s/%s/%s.patch' % (addon_id, version, name)
            target = os.path.join(out_dir, *rel_patch.split('/'))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, 'wb') as f:
                f.write(diff.encode('utf-8'))
            entries.append({
                'addon_id': addon_id,
                'version': version,
                'rel_path': rel_path,
                'orig_sha256': patcher.sha256(orig),
                'patched_sha256': patcher.sha256(new),
                'patch': rel_patch,
                'popis': popis,
            })
            print('%s %s %s' % (addon_id, version, rel_path))
    with open(os.path.join(out_dir, 'manifest.json'), 'w', encoding='utf-8', newline='\n') as f:
        json.dump({'format': 1, 'patches': entries}, f, indent=2, ensure_ascii=False)
        f.write('\n')
    print('hotovo: %d záplat' % len(entries))


if __name__ == '__main__':
    main(os.path.expanduser(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SRC)
