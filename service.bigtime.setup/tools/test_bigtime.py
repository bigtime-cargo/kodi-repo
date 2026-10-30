# Offline testy: python3 -m unittest tools.test_bigtime -v  (z adresára doplnku)
import copy
import hashlib
import json
import os
import shutil
import sys
import tempfile
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import iptv  # noqa: E402
import patcher  # noqa: E402

SRC = os.path.expanduser('~/kontrola-TCL/nastroje/zaplaty')
PATCHES = os.path.join(ROOT, 'patches')


# --- stuby Kodi pre testy služby ---------------------------------------------

class KodiStub:
    """Zhromažďuje volania, ktoré by išli do Kodi."""

    def __init__(self):
        self.playing = False
        self.notifications = []
        self.rpc = []
        self.logs = []
        self.enabled = {'plugin.video.youtube', 'service.sc.helper', 'pvr.iptvsimple'}
        self.installed = set(self.enabled)


KODI = KodiStub()


def _install_stubs():
    xbmc = types.ModuleType('xbmc')
    xbmc.LOGINFO, xbmc.LOGWARNING, xbmc.LOGERROR = 1, 2, 3
    xbmc.log = lambda msg, level=1: KODI.logs.append(msg)
    xbmc.sleep = lambda ms: None

    class Player:
        def isPlaying(self):
            return KODI.playing
    xbmc.Player = Player

    def rpc(payload):
        # doplnok nesmie cez JSON-RPC nič robiť (prepínanie doplnkov zhodí Kodi)
        KODI.rpc.append(payload)
        return json.dumps({'id': 1, 'jsonrpc': '2.0', 'result': 'OK'})
    xbmc.executeJSONRPC = rpc

    def cond(expr):
        name = expr[expr.index('(') + 1:-1]
        if expr.startswith('System.AddonIsEnabled'):
            return name in KODI.enabled
        if expr.startswith('System.HasAddon'):
            return name in KODI.installed
        return False
    xbmc.getCondVisibility = cond

    class Monitor:
        def __init__(self, aborts_after=1):
            self.waits = []
            self.left = aborts_after

        def abortRequested(self):
            return False

        def waitForAbort(self, t):
            self.waits.append(t)
            self.left -= 1
            return self.left < 0
    xbmc.Monitor = Monitor

    xbmcgui = types.ModuleType('xbmcgui')
    xbmcgui.NOTIFICATION_INFO, xbmcgui.NOTIFICATION_WARNING = 'i', 'w'

    class Dialog:
        def notification(self, heading, message, icon='i', time=0):
            KODI.notifications.append(message)
    xbmcgui.Dialog = Dialog

    xbmcvfs = types.ModuleType('xbmcvfs')
    xbmcvfs.translatePath = lambda p: p
    for n, m in (('xbmc', xbmc), ('xbmcgui', xbmcgui), ('xbmcvfs', xbmcvfs)):
        sys.modules[n] = m


_install_stubs()
import bigtime_core as core  # noqa: E402
import service  # noqa: E402


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(data)


def read(path):
    with open(path, 'rb') as f:
        return f.read()


def make_entry(orig, new, rel='lib/mod.py', addon='plugin.test', patches_dir=None):
    """Vyrobí manifest záznam aj .patch súbor pre dvojicu obsahov."""
    diff = patcher.make_unified(orig, new, rel)
    pdir = patches_dir
    write(os.path.join(pdir, addon, '1.0', 'mod.py.patch'), diff.encode('utf-8'))
    return {'addon_id': addon, 'version': '1.0', 'rel_path': rel,
            'orig_sha256': patcher.sha256(orig), 'patched_sha256': patcher.sha256(new),
            'patch': '%s/1.0/mod.py.patch' % addon, 'popis': 'test'}


ORIG = ''.join('riadok %d\n' % i for i in range(1, 41)).encode()
NEW = ORIG.replace(b'riadok 20\n', b'riadok 20\nvlozeny 1\nvlozeny 2\n').replace(b'riadok 5\n', b'')


class ManifestPatches(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(PATCHES, 'manifest.json'), encoding='utf-8') as f:
            self.entries = json.load(f)['patches']

    def test_manifest_nie_je_prazdny(self):
        self.assertEqual(len(self.entries), 3)
        for e in self.entries:
            patcher.validate_entry(e)

    def test_aplikator_dava_nasu_zaplatu(self):
        if not os.path.isdir(SRC):
            self.skipTest('zdrojové záplaty nie sú k dispozícii')
        for e in self.entries:
            d = os.path.join(SRC, '%s-%s' % (e['addon_id'], e['version']))
            name = os.path.basename(e['rel_path'])
            orig, ours = read(os.path.join(d, name + '.orig')), read(os.path.join(d, name))
            self.assertEqual(patcher.sha256(orig), e['orig_sha256'])
            result = patcher.apply_unified(orig, patcher.read_patch(e))
            self.assertEqual(result, ours, name)
            self.assertEqual(patcher.sha256(result), e['patched_sha256'])

    def test_zip_nenesie_cele_cudzie_subory(self):
        for e in self.entries:
            size = os.path.getsize(os.path.join(PATCHES, *e['patch'].split('/')))
            self.assertLess(size, 4000)


class Aplikator(unittest.TestCase):
    def test_zakladny_priklad(self):
        diff = patcher.make_unified(ORIG, NEW, 'x')
        self.assertEqual(patcher.apply_unified(ORIG, diff), NEW)

    def test_pozmeneny_original_je_odmietnuty(self):
        diff = patcher.make_unified(ORIG, NEW, 'x')
        for old, new in ((b'riadok 4\n', b'riadok 4 zmena\n'),     # kontext
                         (b'riadok 5\n', b'riadok 5 zmena\n'),     # odstránený riadok
                         (b'riadok 21\n', b'riadok 21 zmena\n')):  # kontext za vložením
            changed = ORIG.replace(old, new)
            with self.assertRaises(patcher.PatchError):
                patcher.apply_unified(changed, diff)

    def test_posun_riadkov_bez_fuzz(self):
        diff = patcher.make_unified(ORIG, NEW, 'x')
        with self.assertRaises(patcher.PatchError):
            patcher.apply_unified(b'navyse\n' + ORIG, diff)

    def test_zly_diff(self):
        for bad in ('', 'nic\n', '@@ -1,2 +1,2 @@\n a\n', '@@ -1,1 +1,1 @@\n+a\n+b\n'):
            with self.assertRaises(patcher.PatchError):
                patcher.apply_unified(b'a\nb\n', bad)

    def test_bez_koncoveho_riadku_a_crlf_a_utf8(self):
        orig = 'a\r\nb č\r\nc'.encode()
        new = 'a\r\nB č\r\nc\nd'.encode()
        diff = patcher.make_unified(orig, new, 'x')
        self.assertIn('No newline', diff)
        self.assertEqual(patcher.apply_unified(orig, diff), new)
        with self.assertRaises(patcher.PatchError):
            patcher.apply_unified('a\r\nb č\r\nc\n'.encode(), diff)


class StavyAApply(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addons = os.path.join(self.tmp, 'addons')
        self.pdir = os.path.join(self.tmp, 'patches')
        self.entry = make_entry(ORIG, NEW, patches_dir=self.pdir)
        self.path = os.path.join(self.addons, 'plugin.test', 'lib', 'mod.py')

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_stavy(self):
        self.assertEqual(patcher.check(self.addons, self.entry), patcher.NEPRITOMNY)
        os.makedirs(os.path.join(self.addons, 'plugin.test'))
        self.assertEqual(patcher.check(self.addons, self.entry), patcher.NEPRITOMNY)
        write(self.path, ORIG)
        self.assertEqual(patcher.check(self.addons, self.entry), patcher.CHYBA)
        write(self.path, NEW)
        self.assertEqual(patcher.check(self.addons, self.entry), patcher.OK)
        write(self.path, ORIG + b'cudzia zmena\n')
        self.assertEqual(patcher.check(self.addons, self.entry), patcher.INE)

    def test_apply_zalohuje_maze_pyc_a_je_atomicky(self):
        write(self.path, ORIG)
        cache = os.path.join(os.path.dirname(self.path), '__pycache__')
        write(os.path.join(cache, 'mod.cpython-311.pyc'), b'x')
        write(os.path.join(cache, 'mod.cpython-313.pyc'), b'x')
        write(os.path.join(cache, 'modx.cpython-311.pyc'), b'iny modul')
        os.chmod(self.path, 0o640)
        self.assertEqual(patcher.apply(self.addons, self.entry, self.pdir), patcher.NASADENE)
        self.assertEqual(read(self.path), NEW)
        self.assertEqual(read(self.path + '.orig-bigtime'), ORIG)
        self.assertEqual(sorted(os.listdir(cache)), ['modx.cpython-311.pyc'])
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o640)
        self.assertEqual(sorted(os.listdir(os.path.dirname(self.path))),
                         ['__pycache__', 'mod.py', 'mod.py.orig-bigtime'])
        self.assertEqual(patcher.apply(self.addons, self.entry, self.pdir), patcher.OK)

    def test_zaloha_sa_neprepisuje(self):
        write(self.path, ORIG)
        write(self.path + '.orig-bigtime', b'stara zaloha')
        patcher.apply(self.addons, self.entry, self.pdir)
        self.assertEqual(read(self.path + '.orig-bigtime'), b'stara zaloha')

    def test_ine_sa_neprepise(self):
        other = ORIG + b'nova verzia\n'
        write(self.path, other)
        self.assertEqual(patcher.apply(self.addons, self.entry, self.pdir), patcher.INE)
        self.assertEqual(read(self.path), other)
        self.assertFalse(os.path.exists(self.path + '.orig-bigtime'))

    def test_zly_vysledok_nezapise_nic(self):
        write(self.path, ORIG)
        bad = dict(self.entry, patched_sha256='0' * 64)
        with self.assertRaises(patcher.PatchError):
            patcher.apply(self.addons, bad, self.pdir)
        self.assertEqual(read(self.path), ORIG)
        self.assertEqual(sorted(os.listdir(os.path.dirname(self.path))), ['mod.py'])

    def test_poskodena_zaplata_nezapise_nic(self):
        write(self.path, ORIG)
        pf = os.path.join(self.pdir, *self.entry['patch'].split('/'))
        write(pf, read(pf).replace(b' riadok 18', b' riadok 99'))
        with self.assertRaises(patcher.PatchError):
            patcher.apply(self.addons, self.entry, self.pdir)
        self.assertEqual(read(self.path), ORIG)

    def test_odmieta_cesty_mimo_doplnku(self):
        write(self.path, ORIG)
        outside = os.path.join(self.addons, 'cudzi.py')
        write(outside, ORIG)
        for rel in ('../cudzi.py', 'lib/../../cudzi.py', '/etc/passwd', 'lib//mod.py',
                    './lib/mod.py', 'lib\\mod.py', ''):
            e = dict(self.entry, rel_path=rel)
            with self.assertRaises(patcher.PatchError, msg=rel):
                patcher.apply(self.addons, e, self.pdir)
        for aid in ('..', '../x', 'a/b'):
            with self.assertRaises(patcher.PatchError, msg=aid):
                patcher.check(self.addons, dict(self.entry, addon_id=aid))
        self.assertEqual(read(outside), ORIG)

    def test_odmieta_symlink(self):
        real = os.path.join(self.tmp, 'mimo.py')
        write(real, ORIG)
        os.makedirs(os.path.dirname(self.path))
        os.symlink(real, self.path)
        with self.assertRaises(patcher.PatchError):
            patcher.apply(self.addons, self.entry, self.pdir)
        with self.assertRaises(patcher.PatchError):
            patcher.check(self.addons, self.entry)
        self.assertEqual(read(real), ORIG)
        self.assertTrue(os.path.islink(self.path))

    def test_odmieta_symlink_adresara(self):
        os.makedirs(os.path.join(self.tmp, 'mimo'))
        write(os.path.join(self.tmp, 'mimo', 'mod.py'), ORIG)
        os.makedirs(os.path.join(self.addons, 'plugin.test'))
        os.symlink(os.path.join(self.tmp, 'mimo'), os.path.join(self.addons, 'plugin.test', 'lib'))
        with self.assertRaises(patcher.PatchError):
            patcher.apply(self.addons, self.entry, self.pdir)
        self.assertEqual(read(os.path.join(self.tmp, 'mimo', 'mod.py')), ORIG)

    def test_odmieta_patch_mimo_patches(self):
        write(self.path, ORIG)
        with self.assertRaises(patcher.PatchError):
            patcher.apply(self.addons, dict(self.entry, patch='../x.patch'), self.pdir)


class Iptv(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sdir = os.path.join(self.tmp, 'pvr.iptvsimple')
        self.playlist = os.path.join(self.tmp, 'o2', 'playlist.m3u')
        self.epg = os.path.join(self.tmp, 'o2', 'epg.xml')

    def tearDown(self):
        shutil.rmtree(self.tmp)

    O2 = b'#EXTM3U\n#EXTINF:-1,Jednotka\nplugin://plugin.video.o2tv/?channel=1\n'

    def instance(self, n, m3u, name='x'):
        write(os.path.join(self.sdir, 'instance-settings-%d.xml' % n),
              iptv.build_settings_xml(m3u, self.epg, name).encode())

    def test_nakonfigurovane_nic_nemeni(self):
        write(self.playlist, self.O2)
        other = os.path.join(self.tmp, 'Movies', 'playlist.m3u')
        write(other, self.O2)
        self.instance(1, other)
        before = read(os.path.join(self.sdir, 'instance-settings-1.xml'))
        self.assertEqual(iptv.status(self.sdir, self.playlist), iptv.NASTAVENY)
        self.assertEqual(os.listdir(self.sdir), ['instance-settings-1.xml'])
        self.assertEqual(read(os.path.join(self.sdir, 'instance-settings-1.xml')), before)

    def test_special_cesta_sa_prelozi(self):
        write(self.playlist, self.O2)
        self.instance(1, 'special://x/playlist.m3u')
        tr = lambda p: self.playlist if p.startswith('special://') else p  # noqa: E731
        self.assertTrue(iptv.is_configured(self.sdir, tr))
        self.assertFalse(iptv.is_configured(self.sdir))

    def test_instancia_s_cudzim_playlistom_nie_je_nastavena(self):
        foreign = os.path.join(self.tmp, 'cudzi.m3u')
        write(foreign, b'#EXTM3U\nhttp://example.invalid/stream\n')
        self.instance(1, foreign)
        self.instance(2, os.path.join(self.tmp, 'neexistuje.m3u'))
        self.assertFalse(iptv.is_configured(self.sdir))

    def test_prazdny_adresar_a_playlist_vytvori_instanciu_1(self):
        write(self.playlist, self.O2)
        self.assertEqual(iptv.status(self.sdir, self.playlist), iptv.TREBA)
        path = iptv.create_instance(self.sdir, self.playlist, self.epg)
        self.assertEqual(os.path.basename(path), 'instance-settings-1.xml')
        root = iptv.ET.parse(path).getroot()
        self.assertEqual(root.tag, 'settings')
        self.assertEqual(root.get('version'), '2')
        vals = {s.get('id'): s.text for s in root.iter('setting')}
        self.assertEqual(vals, {
            'kodi_addon_instance_name': 'O2 TV (Bigtime)', 'm3uPathType': '0',
            'm3uPath': self.playlist, 'epgPathType': '0', 'epgPath': self.epg,
            'epgTSOverride': 'true', 'catchupEnabled': 'true', 'catchupDays': '7'})
        self.assertEqual(iptv.status(self.sdir, self.playlist), iptv.NASTAVENY)
        self.assertEqual([f for f in os.listdir(self.sdir) if f.startswith('.')], [])

    def test_xml_escapuje_specialne_znaky(self):
        path = iptv.create_instance(self.sdir, '/a&b/<x>/p.m3u', '/e"p".xml')
        vals = {s.get('id'): s.text for s in iptv.ET.parse(path).getroot().iter('setting')}
        self.assertEqual(vals['m3uPath'], '/a&b/<x>/p.m3u')
        self.assertEqual(vals['epgPath'], '/e"p".xml')

    def test_cudzia_instancia_ostane_a_nova_je_cislo_2(self):
        write(self.playlist, self.O2)
        foreign = os.path.join(self.tmp, 'cudzi.m3u')
        write(foreign, b'#EXTM3U\nhttp://example.invalid/stream\n')
        self.instance(1, foreign, 'Cudzia')
        p1 = os.path.join(self.sdir, 'instance-settings-1.xml')
        before = read(p1)
        path = iptv.create_instance(self.sdir, self.playlist, self.epg)
        self.assertEqual(os.path.basename(path), 'instance-settings-2.xml')
        self.assertEqual(read(p1), before)

    def test_medzera_v_cislach(self):
        write(os.path.join(self.sdir, 'instance-settings-1.xml'), b'<settings/>')
        write(os.path.join(self.sdir, 'instance-settings-3.xml'), b'<settings/>')
        self.assertEqual(iptv.free_number(self.sdir), 2)

    def test_playlist_este_neexistuje(self):
        self.assertEqual(iptv.status(self.sdir, self.playlist), iptv.CAKA)
        write(self.playlist, b'')
        self.assertEqual(iptv.status(self.sdir, self.playlist), iptv.CAKA)
        self.assertFalse(os.path.exists(self.sdir))

    def test_poskodena_instancia_sa_ignoruje(self):
        write(self.playlist, self.O2)
        write(os.path.join(self.sdir, 'instance-settings-1.xml'), b'<settings><')
        self.assertEqual(iptv.status(self.sdir, self.playlist), iptv.TREBA)

    def test_znacka_na_hranici_bloku(self):
        write(self.playlist, b'x' * ((1 << 20) - 5) + iptv.MARKER + b'y')
        self.assertTrue(iptv.file_has_marker(self.playlist))


class ZakazPrepinania(unittest.TestCase):
    def test_v_kode_nie_je_prepinanie_doplnkov(self):
        for name in ('bigtime_core.py', 'service.py', 'default.py', 'patcher.py', 'iptv.py'):
            with open(os.path.join(ROOT, name), encoding='utf-8') as f:
                text = f.read()
            self.assertNotIn('SetAddonEnabled', text, name)
            self.assertNotIn('executeJSONRPC', text, name)


class Sluzba(unittest.TestCase):
    """Logika služby so stubmi Kodi, nad dočasným adresárom."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addons = os.path.join(self.tmp, 'addons')
        self.pdir = os.path.join(self.tmp, 'patches')
        self.data = os.path.join(self.tmp, 'data')
        self.sdir = os.path.join(self.tmp, 'pvr')
        self.playlist = os.path.join(self.tmp, 'o2', 'playlist.m3u')
        self.e1 = make_entry(ORIG, NEW, addon='plugin.a', patches_dir=self.pdir)
        self.e2 = make_entry(ORIG, NEW, addon='service.b', patches_dir=self.pdir)
        write(os.path.join(self.pdir, 'manifest.json'),
              json.dumps({'patches': [self.e1, self.e2]}).encode())
        self.p1 = os.path.join(self.addons, 'plugin.a', 'lib', 'mod.py')
        self.p2 = os.path.join(self.addons, 'service.b', 'lib', 'mod.py')
        KODI.__init__()
        KODI.enabled |= {'plugin.a', 'service.b'}
        self.saved = (core.addons_dir, core.data_dir, core.iptv_dir, core.translate,
                      patcher.PATCHES_DIR, iptv.DEFAULT_PLAYLIST, iptv.DEFAULT_EPG)
        core.addons_dir = lambda: self.addons
        core.data_dir = lambda: self.data
        core.iptv_dir = lambda: self.sdir
        core.translate = lambda p: self.playlist if p == iptv.DEFAULT_PLAYLIST else p
        patcher.PATCHES_DIR = self.pdir

    def tearDown(self):
        (core.addons_dir, core.data_dir, core.iptv_dir, core.translate,
         patcher.PATCHES_DIR, iptv.DEFAULT_PLAYLIST, iptv.DEFAULT_EPG) = self.saved
        shutil.rmtree(self.tmp)

    def test_nasadi_bez_jsonrpc_a_jeden_toast_restart(self):
        write(self.p1, ORIG)
        write(self.p2, ORIG)
        res = dict((e['addon_id'], s) for e, s in core.patch_cycle())
        self.assertEqual(res, {'plugin.a': patcher.NASADENE, 'service.b': patcher.NASADENE})
        self.assertEqual(read(self.p1), NEW)
        self.assertEqual(read(self.p2), NEW)
        self.assertEqual(KODI.rpc, [])
        self.assertEqual(KODI.notifications,
                         ['Záplaty pre plugin.a, service.b nasadené — reštartuj Kodi, aby sa použili.'])
        self.assertEqual(core.load_state()['reload_needed'], ['plugin.a', 'service.b'])
        # ďalšie kolá s OK súbormi: ticho, žiadny ďalší toast
        core.patch_cycle()
        core.patch_cycle()
        self.assertEqual(len(KODI.notifications), 1)
        self.assertEqual(KODI.rpc, [])
        self.assertEqual(core.load_state()['reload_needed'], ['plugin.a', 'service.b'])

    def test_reload_needed_sa_po_starte_vycisti(self):
        write(self.p1, ORIG)
        core.patch_cycle()
        write(self.p2, ORIG + b'cudzia\n')
        core.patch_cycle()
        self.assertEqual(core.load_state()['reload_needed'], ['plugin.a'])
        core.reset_pending()
        state = core.load_state()
        self.assertNotIn('reload_needed', state)
        self.assertEqual(len(state['warned']), 1)       # stav INE ostáva
        self.assertNotIn('čaká na reštart', ' '.join(core.status_lines()))

    def test_dialog_ukaze_cakanie_na_restart(self):
        write(self.p1, ORIG)
        write(self.p2, NEW)
        core.patch_cycle()
        lines = core.status_lines()
        self.assertIn('OK (čaká na reštart Kodi)', lines[0])
        self.assertTrue(lines[1].endswith(': OK'))

    def test_pri_prehravani_sa_nic_nenasadi(self):
        write(self.p1, ORIG)
        write(self.p2, ORIG)
        KODI.playing = True
        res = [s for _e, s in core.patch_cycle()]
        self.assertEqual(res, [core.ODLOZENE, core.ODLOZENE])
        self.assertEqual(read(self.p1), ORIG)
        self.assertEqual(read(self.p2), ORIG)
        self.assertEqual(KODI.rpc, [])
        self.assertEqual(KODI.notifications, [])
        KODI.playing = False
        core.patch_cycle()
        self.assertEqual(read(self.p1), NEW)

    def test_ine_toast_len_raz(self):
        write(self.p1, ORIG + b'nova verzia\n')
        write(self.p2, NEW)
        core.patch_cycle()
        self.assertEqual(KODI.notifications,
                         ['Záplata pre plugin.a 1.0 nesedí (nová verzia?)'])
        core.patch_cycle()
        core.patch_cycle()
        self.assertEqual(len(KODI.notifications), 1)
        self.assertEqual(read(self.p1), ORIG + b'nova verzia\n')
        # po novej zmene (iný sha) toast znova; stav prežije aj „reštart“ (nový načítaný state)
        write(self.p1, ORIG + b'este novsia\n')
        core.patch_cycle()
        self.assertEqual(len(KODI.notifications), 2)
        self.assertEqual(len(core.load_state()['warned']), 2)

    def test_vynimka_v_jednej_polozke_nezhodi_ostatne(self):
        write(self.p1, ORIG)
        write(self.p2, ORIG)
        orig_apply = patcher.apply

        def apply(ad, entry, pd=None):
            if entry['addon_id'] == 'plugin.a':
                raise RuntimeError('simulovana chyba')
            return orig_apply(ad, entry, pd)
        patcher.apply = apply
        try:
            res = dict((e['addon_id'], s) for e, s in core.patch_cycle())
        finally:
            patcher.apply = orig_apply
        self.assertEqual(res, {'plugin.a': core.VYNIMKA, 'service.b': patcher.NASADENE})
        self.assertEqual(read(self.p1), ORIG)
        self.assertEqual(read(self.p2), NEW)

    def test_chybny_manifest_nezhodi_sluzbu(self):
        write(os.path.join(self.pdir, 'manifest.json'), b'{nie json')
        self.assertEqual(core.patch_cycle(), [])
        core.cycle()

    def test_bez_doplnkov_je_ticho(self):
        core.cycle()
        self.assertEqual(KODI.notifications, [])
        self.assertEqual(KODI.rpc, [])

    # IPTV

    def test_iptv_vytvori_instanciu_bez_jsonrpc(self):
        write(self.playlist, Iptv.O2)
        core.iptv_cycle()
        self.assertTrue(os.path.isfile(os.path.join(self.sdir, 'instance-settings-1.xml')))
        self.assertEqual(KODI.rpc, [])
        self.assertEqual(KODI.notifications, ['IPTV Simple nastavený pre O2 TV — reštartuj Kodi.'])
        self.assertEqual(core.load_state()['reload_needed'], ['pvr.iptvsimple'])
        self.assertIn('nastavený (čaká na reštart Kodi)', core.status_lines()[-1])
        core.iptv_cycle()
        self.assertEqual(KODI.rpc, [])
        self.assertEqual(len(KODI.notifications), 1)
        self.assertEqual(len(os.listdir(self.sdir)), 1)

    def test_iptv_pri_prehravani_tiez_zapise_bez_jsonrpc(self):
        write(self.playlist, Iptv.O2)
        KODI.playing = True
        core.iptv_cycle()
        self.assertEqual(KODI.rpc, [])
        self.assertEqual(len(os.listdir(self.sdir)), 1)

    def test_iptv_bez_playlistu_nic(self):
        core.iptv_cycle()
        self.assertFalse(os.path.exists(self.sdir))
        self.assertEqual(KODI.rpc, [])

    def test_iptv_nenainstalovany_alebo_vypnuty(self):
        write(self.playlist, Iptv.O2)
        KODI.enabled.discard('pvr.iptvsimple')
        self.assertEqual(core.iptv_cycle(), core.NEDOSTUPNY)
        KODI.enabled.add('pvr.iptvsimple')
        KODI.installed.discard('pvr.iptvsimple')
        self.assertEqual(core.iptv_cycle(), core.NEDOSTUPNY)
        self.assertFalse(os.path.exists(self.sdir))
        self.assertEqual(KODI.rpc, [])

    def test_iptv_nakonfigurovane_nemeni(self):
        write(self.playlist, Iptv.O2)
        other = os.path.join(self.tmp, 'Movies', 'playlist.m3u')
        write(other, Iptv.O2)
        write(os.path.join(self.sdir, 'instance-settings-1.xml'),
              iptv.build_settings_xml(other, 'e').encode())
        core.iptv_cycle()
        self.assertEqual(os.listdir(self.sdir), ['instance-settings-1.xml'])
        self.assertEqual(KODI.rpc, [])

    def test_stav_a_dialog(self):
        write(self.p1, ORIG)
        write(self.p2, ORIG + b'x')
        lines = core.status_lines()
        self.assertEqual(len(lines), 3)
        self.assertIn('CHÝBA', lines[0])
        self.assertIn('INÉ', lines[1])
        self.assertIn('čaká na export', lines[2])
        self.assertEqual(read(self.p1), ORIG)   # status nič nemení

    def test_sluzba_slucka(self):
        write(self.p1, ORIG)
        mon = sys.modules['xbmc'].Monitor(aborts_after=3)
        service.run(mon)
        self.assertEqual(mon.waits[0], service.START_DELAY)
        self.assertEqual(read(self.p1), NEW)

    def test_sluzba_prezije_vynimku_v_kole(self):
        orig_cycle = core.cycle
        calls = []

        def boom(patches=True):
            calls.append(patches)
            raise RuntimeError('x')
        core.cycle = boom
        try:
            service.run(sys.modules['xbmc'].Monitor(aborts_after=3))
        finally:
            core.cycle = orig_cycle
        self.assertEqual(calls, [True, False, False])

    def test_sluzba_koniec_pocas_cakania_na_start(self):
        mon = sys.modules['xbmc'].Monitor(aborts_after=-1)
        service.run(mon)
        self.assertEqual(mon.waits, [service.START_DELAY])


if __name__ == '__main__':
    unittest.main()
