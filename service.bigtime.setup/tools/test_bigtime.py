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
        self.settings = {}


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

    xbmcaddon = types.ModuleType('xbmcaddon')

    class Addon:
        def __init__(self, _id=None):
            pass

        def getSetting(self, key):
            return KODI.settings.get(key, '')
    xbmcaddon.Addon = Addon

    xbmcgui = types.ModuleType('xbmcgui')
    xbmcgui.NOTIFICATION_INFO, xbmcgui.NOTIFICATION_WARNING = 'i', 'w'

    class Dialog:
        def notification(self, heading, message, icon='i', time=0):
            KODI.notifications.append(message)
    xbmcgui.Dialog = Dialog

    xbmcvfs = types.ModuleType('xbmcvfs')
    xbmcvfs.translatePath = lambda p: p
    for n, m in (('xbmc', xbmc), ('xbmcaddon', xbmcaddon), ('xbmcgui', xbmcgui), ('xbmcvfs', xbmcvfs)):
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
        self.assertEqual(len(self.entries), 4)
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


class IptvMigracia(Iptv):
    """Plán a prepis starých samostatných inštancií (bez Kodi)."""

    def test_plan_vyberie_o2_potom_magio_potom_streamlet(self):
        write(self.playlist, self.O2)
        magio = os.path.join(self.tmp, 'm', 'playlist.m3u')
        write(magio, b'#EXTM3U\n#EXTINF:-1,A\nplugin://plugin.video.magiogo/?x=1\n')
        st = os.path.join(self.tmp, 'addon_data', 'service.bigtime.streamlet', 'streamlet-cz.m3u')
        write(st, b'#EXTM3U\n#EXTINF:-1,B\nhttp://x/b.m3u8\n')
        tr = lambda p: p.replace('special://userdata', self.tmp)  # noqa: E731
        self.instance(1, st)
        self.instance(2, magio)
        self.instance(3, self.playlist)
        plan = iptv.plan_migration(self.sdir, tr)
        self.assertEqual(plan['primary'][0], 3)            # O2 TV má prednosť pred nižším číslom
        self.assertEqual(sorted(n for n, _p, _r in plan['others']), [1, 2])
        self.assertEqual(plan['roles'], ['o2tv', 'magiogo', 'streamlet'])

    def test_plan_bez_nasich_instancii_je_none(self):
        foreign = os.path.join(self.tmp, 'cudzi.m3u')
        write(foreign, b'#EXTM3U\nhttp://example.invalid/s\n')
        self.instance(1, foreign)
        self.assertIsNone(iptv.plan_migration(self.sdir))
        self.assertIsNone(iptv.plan_migration(os.path.join(self.tmp, 'nic')))

    def test_plan_ignoruje_uz_spojenu_instanciu(self):
        merged = os.path.join(self.tmp, 'bigtime.m3u')
        write(merged, b'#EXTM3U\n#BIGTIME-MERGED\n')
        self.instance(1, merged)
        self.assertEqual(iptv.instance_roles(self.sdir), [])

    def test_render_instance_zachova_ostatne_a_zahodi_default(self):
        path = os.path.join(self.sdir, 'instance-settings-1.xml')
        write(path, b'<settings version="2">\n<setting id="m3uPath" default="true">a</setting>\n'
                    b'<setting id="vlastne">x</setting>\n<setting id="prazdne" default="true" />\n</settings>\n')
        text = iptv.render_instance(path, (('m3uPath', 'b&c'), ('nove', '1')))
        root = iptv.ET.fromstring(text)
        vals = {e.get('id'): (e.text, e.get('default')) for e in root.iter('setting')}
        self.assertEqual(vals['m3uPath'], ('b&c', None))
        self.assertEqual(vals['vlastne'], ('x', None))
        self.assertEqual(vals['nove'], ('1', None))
        self.assertEqual(vals['prazdne'][1], 'true')
        self.assertEqual(read(path).count(b'<setting '), 3)     # čítanie nič nezapísalo


class IptvMagio(Iptv):
    """Magio GO: vlastná značka, vlastný playlist, obnova každých 15 min."""
    MAGIO = (b'#EXTM3U\n#EXTINF:-1 tvg-id="4621",AMC\n'
             b'plugin://plugin.video.magiogo/?action=play&cid=4621\n')

    def test_znacky_sa_nemiesaju(self):
        write(self.playlist, self.O2)
        self.assertEqual(iptv.status(self.sdir, self.playlist, marker=iptv.MAGIO_MARKER), iptv.CAKA)
        write(self.playlist, self.MAGIO)
        self.assertEqual(iptv.status(self.sdir, self.playlist, marker=iptv.MAGIO_MARKER), iptv.TREBA)
        self.assertEqual(iptv.status(self.sdir, self.playlist), iptv.CAKA)

    def test_o2_instancia_nenastavi_magio(self):
        write(self.playlist, self.O2)
        self.instance(1, self.playlist)
        self.assertTrue(iptv.is_configured(self.sdir))
        self.assertFalse(iptv.is_configured(self.sdir, marker=iptv.MAGIO_MARKER))

    def test_magio_instancia_ma_obnovu_15_min(self):
        write(self.playlist, self.MAGIO)
        path = iptv.create_instance(self.sdir, self.playlist, self.epg, iptv.MAGIO_INSTANCE_NAME,
                                    iptv.MAGIO_EXTRA)
        vals = {s.get('id'): s.text for s in iptv.ET.parse(path).getroot().iter('setting')}
        self.assertEqual(vals['kodi_addon_instance_name'], 'Magio GO (Bigtime)')
        self.assertEqual(vals['m3uRefreshMode'], '1')
        self.assertEqual(vals['m3uRefreshIntervalMins'], '15')
        self.assertEqual(vals['epgPath'], self.epg)
        self.assertEqual(vals['catchupEnabled'], 'true')
        self.assertEqual(iptv.status(self.sdir, self.playlist, marker=iptv.MAGIO_MARKER),
                         iptv.NASTAVENY)

    def test_o2_instancia_bez_extra_je_nezmenena(self):
        xml = iptv.build_settings_xml('m', 'e')
        self.assertNotIn('m3uRefresh', xml)


class ZakazPrepinania(unittest.TestCase):
    def test_v_kode_nie_je_prepinanie_doplnkov(self):
        for name in ('bigtime_core.py', 'service.py', 'default.py', 'patcher.py', 'iptv.py', 'merge.py'):
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
        self.ud = os.path.join(self.tmp, 'ud')        # special://userdata
        ad = os.path.join(self.ud, 'addon_data')
        self.playlist = os.path.join(ad, 'plugin.video.o2tv', 'playlist.m3u')
        self.o2_epg = os.path.join(ad, 'plugin.video.o2tv', 'epg.xml')
        self.magio_playlist = os.path.join(ad, 'plugin.video.magiogo', 'playlist.m3u')
        self.magio_epg = os.path.join(ad, 'plugin.video.magiogo', 'epg.xml')
        self.st_playlist = os.path.join(ad, 'service.bigtime.streamlet', 'streamlet-cz.m3u')
        self.st_epg = os.path.join(ad, 'service.bigtime.streamlet', 'streamlet-epg.xml')
        self.merged = os.path.join(ad, 'service.bigtime.setup', 'bigtime.m3u')
        self.merged_epg = os.path.join(ad, 'service.bigtime.setup', 'bigtime-epg.xml')
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
        core.translate = lambda p: p.replace('special://userdata', self.ud)
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

    # IPTV: spoločný zoznam kanálov

    O2 = Iptv.O2 + ('#EXTINF:-1 tvg-id="840016" group-title="O2 TV (archív);Šport" tvg-chno="2",Dvojka\n'
                    'plugin://plugin.video.o2tv/?action=play&cid=840016\n').encode()
    MAGIO = ('#EXTM3U\n#EXTINF:-1 tvg-id="4621" group-title="Magio GO (archív);Šport" tvg-chno="2",AMC\n'
             'plugin://plugin.video.magiogo/?action=play&cid=4621\n').encode()
    STREAMLET = ('#EXTM3U\n#EXTINF:-1 tvg-id="HBO.cz" group-title="Šport" tvg-chno="101",HBO\n'
                 'http://x.invalid/hbo.m3u8\n').encode()

    def zdroje(self):
        write(self.playlist, self.O2)
        write(self.o2_epg, b'<?xml version="1.0"?><tv><channel id="840016"/></tv>')
        write(self.magio_playlist, self.MAGIO)
        write(self.magio_epg, b'<?xml version="1.0"?><tv><channel id="4621"/></tv>')
        write(self.st_playlist, self.STREAMLET)

    def instancie(self):
        return sorted(os.listdir(self.sdir)) if os.path.isdir(self.sdir) else []

    def test_iptv_spoji_zdroje_a_vytvori_jednu_instanciu(self):
        self.zdroje()
        self.assertEqual(core.iptv_cycle(), iptv.SPOJENE)
        self.assertEqual(self.instancie(), ['instance-settings-1.xml'])
        vals = {e.get('id'): e.text for e in iptv.ET.parse(
            os.path.join(self.sdir, 'instance-settings-1.xml')).getroot().iter('setting')}
        self.assertEqual(vals['kodi_addon_instance_name'], 'Bigtime TV')
        self.assertEqual(vals['m3uPath'], self.merged)
        self.assertEqual(vals['epgPath'], self.merged_epg)
        self.assertEqual(vals['m3uRefreshIntervalMins'], '2')
        text = read(self.merged).decode()
        self.assertIn('#BIGTIME-MERGED', text)
        self.assertEqual(text.count('#EXTINF'), 4)
        self.assertEqual(KODI.rpc, [])
        self.assertEqual(KODI.notifications,
                         ['IPTV Simple nastavený (spoločný zoznam kanálov) — reštartuj Kodi.'])
        self.assertEqual(core.load_state()['reload_needed'], ['pvr.iptvsimple'])
        self.assertIn('spoločný zoznam kanálov', core.status_lines()[-1])
        self.assertIn('čaká na reštart Kodi', core.status_lines()[-1])
        self.assertEqual(core.iptv_cycle(), iptv.SPOJENE)       # nič nové
        self.assertEqual(self.instancie(), ['instance-settings-1.xml'])
        self.assertEqual(len(KODI.notifications), 1)

    def stara_spolocna(self, interval, m3u=None):
        """Inštancia ako z 1.3.0/1.3.1: spoločný zoznam s obnovou `interval` min a vlastným nastavením."""
        write(os.path.join(self.sdir, 'instance-settings-1.xml'),
              iptv.build_settings_xml(m3u or self.merged, self.merged_epg, iptv.MERGED_NAME, (
                  ('m3uRefreshMode', '1'), ('m3uRefreshIntervalMins', interval),
                  ('mojeNastavenie', 'hodnota'))).encode())

    def test_obnova_spolocnej_instancie_sa_skrati_z_15_na_2(self):
        self.zdroje()
        self.stara_spolocna('15')
        self.assertEqual(core.iptv_cycle(), iptv.SPOJENE)
        v = self.hodnoty(1)
        self.assertEqual(v['m3uRefreshIntervalMins'], '2')
        self.assertEqual(v['mojeNastavenie'], 'hodnota')            # ostatné ostane
        self.assertEqual(v['m3uPath'], self.merged)
        self.assertEqual(KODI.rpc, [])                               # žiadne prepínanie doplnkov
        self.assertEqual(KODI.notifications, [])                     # bez toastu
        self.assertEqual(core.load_state()['reload_needed'], ['pvr.iptvsimple'])
        before = self.subory_instancii()
        self.assertEqual(core.iptv_cycle(), iptv.SPOJENE)           # druhýkrát nič
        self.assertEqual(self.subory_instancii(), before)

    def test_obnova_spolocnej_instancie_sa_skrati_z_5_na_2(self):
        self.zdroje()
        self.stara_spolocna('5')                                     # inštancia z 1.3.2
        self.assertEqual(core.iptv_cycle(), iptv.SPOJENE)
        v = self.hodnoty(1)
        self.assertEqual(v['m3uRefreshIntervalMins'], '2')
        self.assertEqual(v['mojeNastavenie'], 'hodnota')
        self.assertEqual(KODI.rpc, [])
        self.assertEqual(KODI.notifications, [])
        before = self.subory_instancii()
        self.assertEqual(core.iptv_cycle(), iptv.SPOJENE)
        self.assertEqual(self.subory_instancii(), before)

    def test_rucne_nastavena_obnova_ostane(self):
        self.zdroje()
        self.stara_spolocna('30')
        core.iptv_cycle()
        self.assertEqual(self.hodnoty(1)['m3uRefreshIntervalMins'], '30')
        self.assertNotIn('reload_needed', core.load_state())

    def test_obnova_cudzej_instancie_sa_nemeni(self):
        self.zdroje()
        foreign = os.path.join(self.tmp, 'cudzi', 'playlist.m3u')
        write(foreign, b'#EXTM3U\n')
        write(os.path.join(self.sdir, 'instance-settings-2.xml'),
              iptv.build_settings_xml(foreign, self.merged_epg, 'NBA', (
                  ('m3uRefreshIntervalMins', '15'),)).encode())
        self.stara_spolocna('15')
        core.iptv_cycle()
        self.assertEqual(self.hodnoty(1)['m3uRefreshIntervalMins'], '2')
        self.assertEqual(self.hodnoty(2)['m3uRefreshIntervalMins'], '15')

    def test_iptv_pri_prehravani_tiez_zapise_bez_jsonrpc(self):
        self.zdroje()
        KODI.playing = True
        core.iptv_cycle()
        self.assertEqual(KODI.rpc, [])
        self.assertEqual(len(self.instancie()), 1)

    def test_iptv_bez_zdrojov_nic(self):
        self.assertEqual(core.iptv_cycle(), core.CAKA)
        self.assertFalse(os.path.exists(self.sdir))
        self.assertFalse(os.path.exists(self.merged))
        self.assertEqual(KODI.rpc, [])
        self.assertIn('čaká na export', core.status_lines()[-1])

    def test_iptv_staci_jeden_zdroj(self):
        write(self.magio_playlist, self.MAGIO)
        core.iptv_cycle()
        self.assertEqual(len(self.instancie()), 1)
        self.assertEqual(read(self.merged).decode().count('#EXTINF'), 1)

    def test_iptv_nenainstalovany_alebo_vypnuty(self):
        self.zdroje()
        KODI.enabled.discard('pvr.iptvsimple')
        self.assertEqual(core.iptv_cycle(), core.NEDOSTUPNY)
        KODI.enabled.add('pvr.iptvsimple')
        KODI.installed.discard('pvr.iptvsimple')
        self.assertEqual(core.iptv_cycle(), core.NEDOSTUPNY)
        self.assertFalse(os.path.exists(self.sdir))
        self.assertEqual(KODI.rpc, [])

    def test_stare_samostatne_instancie_sa_nemenia_a_nic_sa_nespaja(self):
        KODI.settings['auto_migrate'] = 'false'
        self.zdroje()
        other = os.path.join(self.tmp, 'Movies', 'playlist.m3u')
        write(other, self.O2)                                    # starý export O2 TV
        write(os.path.join(self.sdir, 'instance-settings-1.xml'),
              iptv.build_settings_xml(other, 'e').encode())
        write(os.path.join(self.sdir, 'instance-settings-2.xml'),
              iptv.build_settings_xml(self.st_playlist, 'e').encode())
        before = {n: read(os.path.join(self.sdir, n)) for n in self.instancie()}
        self.assertEqual(core.iptv_cycle(), iptv.SAMOSTATNE)
        self.assertEqual({n: read(os.path.join(self.sdir, n)) for n in self.instancie()}, before)
        self.assertFalse(os.path.exists(self.merged))
        self.assertEqual(KODI.notifications, [])
        self.assertIn('samostatné inštancie', core.status_lines()[-1])

    def test_samostatna_instancia_streamletu_staci_na_samostatny_rezim(self):
        KODI.settings['auto_migrate'] = 'false'
        self.zdroje()
        write(os.path.join(self.sdir, 'instance-settings-1.xml'),
              iptv.build_settings_xml(self.st_playlist, 'e').encode())
        self.assertEqual(core.iptv_cycle(), iptv.SAMOSTATNE)

    def test_cudzia_instancia_nebrani(self):
        self.zdroje()
        foreign = os.path.join(self.tmp, 'cudzi.m3u')
        write(foreign, b'#EXTM3U\nhttp://example.invalid/s\n')
        write(os.path.join(self.sdir, 'instance-settings-1.xml'),
              iptv.build_settings_xml(foreign, 'e', 'NBA').encode())
        self.assertEqual(core.iptv_cycle(), iptv.SPOJENE)
        self.assertEqual(self.instancie(), ['instance-settings-1.xml', 'instance-settings-2.xml'])

    def test_spojena_instancia_sa_pri_zmene_zdroja_obnovi(self):
        self.zdroje()
        core.iptv_cycle()
        first = read(self.merged_epg)
        write(self.magio_epg, b'<?xml version="1.0"?><tv><channel id="4621"/><channel id="9999"/></tv>')
        os.utime(self.magio_epg, (1, 1))
        self.assertEqual(core.iptv_cycle(), iptv.SPOJENE)
        self.assertNotEqual(read(self.merged_epg), first)
        self.assertIn(b'9999', read(self.merged_epg))
        self.assertEqual(len(self.instancie()), 1)

    def test_po_zmazani_starych_instancii_vznikne_spojena(self):
        KODI.settings['auto_migrate'] = 'false'
        self.zdroje()
        write(os.path.join(self.sdir, 'instance-settings-1.xml'),
              iptv.build_settings_xml(self.playlist, 'e').encode())
        self.assertEqual(core.iptv_cycle(), iptv.SAMOSTATNE)
        os.remove(os.path.join(self.sdir, 'instance-settings-1.xml'))
        self.assertEqual(core.iptv_cycle(), iptv.SPOJENE)
        self.assertEqual(self.instancie(), ['instance-settings-1.xml'])

    # IPTV: automatická migrácia starých samostatných inštancií

    def stare_instancie(self, o2=True, streamlet=True, magio=True, foreign=True):
        """1: O2 (starý export mimo addon_data), 2: Streamlet, 3: Magio GO, 4: cudzia (NBA)."""
        other = os.path.join(self.tmp, 'Movies', 'playlist.m3u')
        write(other, self.O2)
        spec = []
        if o2:
            spec.append((1, other, 'O2 TV (Bigtime)'))
        if streamlet:
            spec.append((2, self.st_playlist, 'Streamlet CZ (Bigtime)'))
        if magio:
            spec.append((3, self.magio_playlist, 'Magio GO (Bigtime)'))
        for n, m3u, name in spec:
            xml = iptv.build_settings_xml(m3u, 'e', name).replace(
                '</settings>', '    <setting id="mojeNastavenie">hodnota</setting>\n</settings>')
            write(os.path.join(self.sdir, 'instance-settings-%d.xml' % n), xml.encode())
        if foreign:
            nba = os.path.join(self.tmp, 'nba.m3u')
            write(nba, b'#EXTM3U\nhttp://example.invalid/nba\n')
            write(os.path.join(self.sdir, 'instance-settings-4.xml'),
                  iptv.build_settings_xml(nba, 'e', 'NBA (Bigtime)').encode())

    def hodnoty(self, n):
        root = iptv.ET.parse(os.path.join(self.sdir, 'instance-settings-%d.xml' % n)).getroot()
        return {e.get('id'): e.text for e in root.iter('setting')}

    def subory_instancii(self):
        return {n: read(os.path.join(self.sdir, n)) for n in self.instancie()}

    @property
    def prazdny(self):
        return os.path.join(self.ud, 'addon_data', 'service.bigtime.setup', 'prazdny.m3u')

    def test_migracia_prepise_instancie_zalohuje_a_cudzie_nechá(self):
        self.zdroje()
        self.stare_instancie()
        before = self.subory_instancii()
        self.assertEqual(core.iptv_cycle(), iptv.SPOJENE)
        # O2 → spoločná inštancia, vlastné nastavenia ostanú
        v1 = self.hodnoty(1)
        self.assertEqual(v1['kodi_addon_instance_name'], 'Bigtime TV')
        self.assertEqual(v1['m3uPath'], self.merged)
        self.assertEqual(v1['epgPath'], self.merged_epg)
        self.assertEqual(v1['m3uRefreshIntervalMins'], '2')
        self.assertEqual(v1['mojeNastavenie'], 'hodnota')
        # ostatné naše → prázdny playlist, názov ostáva
        for n, name in ((2, 'Streamlet CZ (Bigtime)'), (3, 'Magio GO (Bigtime)')):
            v = self.hodnoty(n)
            self.assertEqual(v['m3uPath'], self.prazdny)
            self.assertEqual(v['kodi_addon_instance_name'], name)
        self.assertEqual(read(self.prazdny), b'#EXTM3U\n')
        # cudzia inštancia nedotknutá, nič nepribudlo
        self.assertEqual(self.subory_instancii()['instance-settings-4.xml'], before['instance-settings-4.xml'])
        self.assertEqual(len(self.instancie()), 4)
        # záloha pôvodných súborov
        backups = [d for d in os.listdir(self.data) if d.startswith('zaloha-')]
        self.assertEqual(len(backups), 1)
        saved = {n: read(os.path.join(self.data, backups[0], n)) for n in os.listdir(os.path.join(self.data, backups[0]))}
        self.assertEqual(saved, {n: before[n] for n in ('instance-settings-1.xml', 'instance-settings-2.xml',
                                                         'instance-settings-3.xml')})
        # stav, toast, žiadne JSON-RPC
        self.assertEqual(KODI.rpc, [])
        self.assertEqual(KODI.notifications,
                         ['Staré samostatné zoznamy kanálov sú prepísané na jeden spoločný — reštartuj Kodi.'])
        state = core.load_state()
        self.assertEqual(state['migration']['phase'], 'prepared')
        self.assertEqual(state['migration']['emptied'], [2, 3])
        self.assertEqual(state['reload_needed'], ['pvr.iptvsimple'])
        self.assertEqual(iptv.mode(self.sdir, core.translate), iptv.SPOJENE)
        # druhé kolo nič nerobí
        after = self.subory_instancii()
        self.assertEqual(core.iptv_cycle(), iptv.SPOJENE)
        self.assertEqual(self.subory_instancii(), after)
        self.assertEqual(len(KODI.notifications), 1)

    def test_migracia_po_restarte_tise_zmaze_vyprazdnene(self):
        self.zdroje()
        self.stare_instancie()
        core.iptv_cycle()
        core.iptv_cycle()
        self.assertEqual(len(self.instancie()), 4)              # pred reštartom sa nemaže
        core.reset_pending()                                    # štart služby = Kodi sa reštartovalo
        self.assertEqual(core.load_state()['migration']['phase'], 'restarted')
        self.assertEqual(core.iptv_cycle(), iptv.SPOJENE)
        self.assertEqual(self.instancie(), ['instance-settings-1.xml', 'instance-settings-4.xml'])
        self.assertEqual(core.load_state()['migration']['phase'], 'done')
        self.assertEqual(core.load_state()['migration']['removed'], [2, 3])
        self.assertEqual(len(KODI.notifications), 2)            # + oznam o ďalšom reštarte
        self.assertIn('po ďalšom reštarte', KODI.notifications[1])
        self.assertEqual(core.load_state()['reload_needed'], ['pvr.iptvsimple'])
        self.assertEqual(KODI.rpc, [])

    def test_migracia_nezmaze_vyprazdnenu_ked_ju_uzivatel_zmenil(self):
        self.zdroje()
        self.stare_instancie()
        core.iptv_cycle()
        mine = os.path.join(self.tmp, 'moj.m3u')
        write(mine, b'#EXTM3U\nhttp://example.invalid/m\n')
        p2 = os.path.join(self.sdir, 'instance-settings-2.xml')
        write(p2, iptv.build_settings_xml(mine, 'e', 'Moja').encode())
        core.reset_pending()
        core.iptv_cycle()
        self.assertEqual(self.instancie(), ['instance-settings-1.xml', 'instance-settings-2.xml',
                                            'instance-settings-4.xml'])

    def test_migracia_caka_na_export_vsetkych_zdrojov(self):
        self.zdroje()
        os.remove(self.magio_playlist)                          # Magio GO ešte neexportoval
        self.stare_instancie()
        before = self.subory_instancii()
        self.assertEqual(core.iptv_cycle(), iptv.SAMOSTATNE)
        self.assertEqual(self.subory_instancii(), before)
        self.assertEqual(KODI.notifications, [])
        self.assertNotIn('migration', core.load_state())
        write(self.magio_playlist, self.MAGIO)                  # po exporte sa migruje
        self.assertEqual(core.iptv_cycle(), iptv.SPOJENE)
        self.assertEqual(self.hodnoty(1)['m3uPath'], self.merged)

    def test_migracia_nepotrebuje_zdroj_bez_instancie(self):
        self.zdroje()
        os.remove(self.magio_playlist)                          # Magio GO nemá inštanciu, nevadí
        self.stare_instancie(magio=False)
        self.assertEqual(core.iptv_cycle(), iptv.SPOJENE)

    def test_migracia_vypnuta_nastavenim(self):
        self.zdroje()
        self.stare_instancie()
        KODI.settings['auto_migrate'] = 'false'
        before = self.subory_instancii()
        self.assertEqual(core.iptv_cycle(), iptv.SAMOSTATNE)
        self.assertEqual(self.subory_instancii(), before)
        self.assertFalse(os.path.exists(self.merged))
        self.assertEqual(KODI.notifications, [])

    def test_migracia_bez_iptv_simple_nic_nerobi(self):
        self.zdroje()
        self.stare_instancie()
        KODI.enabled.discard('pvr.iptvsimple')
        before = self.subory_instancii()
        self.assertEqual(core.iptv_cycle(), iptv.SAMOSTATNE)
        self.assertEqual(self.subory_instancii(), before)

    def test_migracia_bez_o2_prepise_magio(self):
        self.zdroje()
        self.stare_instancie(o2=False)
        core.iptv_cycle()
        self.assertEqual(self.hodnoty(3)['m3uPath'], self.merged)           # Magio GO je spoločná
        self.assertEqual(self.hodnoty(3)['kodi_addon_instance_name'], 'Bigtime TV')
        self.assertEqual(self.hodnoty(2)['m3uPath'], self.prazdny)          # Streamlet vyprázdnený
        self.assertEqual(core.load_state()['migration']['emptied'], [2])

    def test_migracia_jedinej_instancie(self):
        self.zdroje()
        self.stare_instancie(streamlet=False, magio=False, foreign=False)
        core.iptv_cycle()
        self.assertEqual(self.hodnoty(1)['m3uPath'], self.merged)
        self.assertEqual(core.load_state()['migration']['emptied'], [])
        core.reset_pending()
        core.iptv_cycle()
        self.assertEqual(self.instancie(), ['instance-settings-1.xml'])
        self.assertEqual(core.load_state()['migration']['phase'], 'done')
        self.assertEqual(len(KODI.notifications), 1)            # nič sa nemazalo, tak ani 2. oznam

    def test_migracia_sa_nezopakuje(self):
        self.zdroje()
        self.stare_instancie()
        core.iptv_cycle()
        core.reset_pending()
        core.iptv_cycle()                                       # dokončená
        write(os.path.join(self.sdir, 'instance-settings-2.xml'),
              iptv.build_settings_xml(self.st_playlist, 'e', 'Znova').encode())
        before = self.subory_instancii()
        self.assertEqual(iptv.mode(self.sdir, core.translate), iptv.SPOJENE)   # primárna číta spoločný
        os.remove(os.path.join(self.sdir, 'instance-settings-1.xml'))          # a keď zmizne...
        self.assertEqual(core.iptv_cycle(), iptv.SAMOSTATNE)
        self.assertEqual(len(KODI.notifications), 2)
        self.assertEqual({k: v for k, v in self.subory_instancii().items()},
                         {k: v for k, v in before.items() if k != 'instance-settings-1.xml'})

    def test_migracia_pri_chybe_vrati_povodne_subory(self):
        self.zdroje()
        self.stare_instancie()
        before = self.subory_instancii()
        real = iptv.write_atomic
        calls = []

        def boom(path, text):
            calls.append(path)
            if len(calls) == 3:                                  # prázdny playlist, 1. inštancia, potom pád
                raise OSError('disk plný')
            return real(path, text)
        iptv.write_atomic = boom
        try:
            self.assertEqual(core.iptv_cycle(), iptv.SAMOSTATNE)
        finally:
            iptv.write_atomic = real
        self.assertEqual(self.subory_instancii(), before)
        self.assertEqual(core.load_state()['migration']['phase'], 'failed')
        self.assertEqual(KODI.notifications, [])
        self.assertEqual(core.iptv_cycle(), iptv.SAMOSTATNE)    # po chybe sa neskúša znova
        self.assertEqual(self.subory_instancii(), before)

    def test_dialog_ukazuje_stav_samostatnych_instancii(self):
        KODI.settings['auto_migrate'] = 'false'
        self.zdroje()
        self.stare_instancie()
        self.assertIn('prepíše na jeden spoločný zoznam', core.status_lines()[-1])

    def test_cycle_zavola_iptv_a_prezije_vynimku(self):
        self.zdroje()
        orig = core.iptv_cycle

        def boom():
            raise RuntimeError('x')
        core.iptv_cycle = boom
        try:
            core.cycle(patches=False)
        finally:
            core.iptv_cycle = orig
        self.assertTrue(any('kolo IPTV zlyhalo' in m for m in KODI.logs))

    def test_stav_a_dialog(self):
        write(self.p1, ORIG)
        write(self.p2, ORIG + b'x')
        lines = core.status_lines()
        self.assertEqual(len(lines), 3)
        self.assertIn('CHÝBA', lines[0])
        self.assertIn('INÉ', lines[1])
        self.assertIn('IPTV Simple: čaká na export z doplnkov', lines[2])
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


    # --- zrušený doplnok NBA (1.4.0) ---

    def nba_stav(self, instancia=True, doplnok=True, data=True):
        """Priečinok doplnku NBA, jeho dáta a inštancia 4 s jeho playlistom + cudzia inštancia 5."""
        nba_pl = os.path.join(self.ud, 'addon_data', 'service.bigtime.nba', 'nba-us.m3u')
        if doplnok:
            write(os.path.join(self.addons, 'service.bigtime.nba', 'service.py'), b'x')
        if data:
            write(nba_pl, b'#EXTM3U\nhttp://example.invalid/nba\n')
            write(os.path.join(os.path.dirname(nba_pl), 'nba-epg.xml'), b'<tv/>')
        if instancia:
            write(os.path.join(self.sdir, 'instance-settings-4.xml'),
                  iptv.build_settings_xml(nba_pl, 'e', 'NBA (Bigtime)').encode())
        cudzi = os.path.join(self.tmp, 'cudzi.m3u')
        write(cudzi, b'#EXTM3U\nhttp://example.invalid/c\n')
        write(os.path.join(self.sdir, 'instance-settings-5.xml'),
              iptv.build_settings_xml(cudzi, 'e', 'Cudzia').encode())

    def test_nba_upratanie_prebehne_po_krokoch_s_restartmi(self):
        self.nba_stav()
        cudzia = read(os.path.join(self.sdir, 'instance-settings-5.xml'))
        # 1. kolo: zmaže sa len priečinok doplnku, inštancia a dáta ostanú (služba ešte beží)
        self.assertTrue(core.nba_cleanup_cycle())
        self.assertFalse(os.path.exists(os.path.join(self.addons, 'service.bigtime.nba')))
        self.assertTrue(os.path.exists(os.path.join(self.ud, 'addon_data', 'service.bigtime.nba', 'nba-us.m3u')))
        self.assertEqual(self.hodnoty(4)['m3uPath'].endswith('nba-us.m3u'), True)
        self.assertEqual(core.load_state()['nba_cleanup']['phase'], 'addon_removed')
        self.assertFalse(core.nba_cleanup_cycle())              # pred reštartom nič ďalšie
        self.assertTrue(os.path.exists(os.path.join(self.ud, 'addon_data', 'service.bigtime.nba')))
        # reštart Kodi → 2. krok: inštancia vyprázdnená, dáta zmazané, toast
        core.reset_pending()
        self.assertEqual(core.load_state()['nba_cleanup']['phase'], 'addon_restarted')
        self.assertTrue(core.nba_cleanup_cycle())
        self.assertEqual(self.hodnoty(4)['m3uPath'], self.prazdny)
        self.assertEqual(self.hodnoty(4)['kodi_addon_instance_name'], 'NBA (Bigtime)')
        self.assertFalse(os.path.exists(os.path.join(self.ud, 'addon_data', 'service.bigtime.nba')))
        self.assertEqual(core.load_state()['nba_cleanup']['phase'], 'emptied')
        self.assertEqual(core.load_state()['reload_needed'], ['pvr.iptvsimple'])
        self.assertEqual(len(KODI.notifications), 1)
        self.assertFalse(core.nba_cleanup_cycle())
        # ďalší reštart → 3. krok: vyprázdnená inštancia sa zmaže, cudzia ostane
        core.reset_pending()
        self.assertTrue(core.nba_cleanup_cycle())
        self.assertFalse(os.path.exists(os.path.join(self.sdir, 'instance-settings-4.xml')))
        self.assertEqual(read(os.path.join(self.sdir, 'instance-settings-5.xml')), cudzia)
        self.assertEqual(core.load_state()['nba_cleanup']['phase'], 'done')
        self.assertEqual(core.load_state()['nba_cleanup']['removed'], [4])
        self.assertFalse(core.nba_cleanup_cycle())
        self.assertEqual(KODI.rpc, [])

    def test_nba_bez_priecinka_doplnku_sa_upratuje_hned(self):
        self.nba_stav(doplnok=False)
        self.assertTrue(core.nba_cleanup_cycle())
        self.assertEqual(self.hodnoty(4)['m3uPath'], self.prazdny)
        self.assertEqual(core.load_state()['nba_cleanup']['phase'], 'emptied')

    def test_nba_cista_instalacia_nic_nerobi(self):
        self.assertFalse(core.nba_cleanup_cycle())
        self.assertEqual(core.load_state()['nba_cleanup']['phase'], 'done')
        self.assertEqual(KODI.notifications, [])
        self.nba_stav()                                         # po „done“ sa už nič nehľadá
        self.assertFalse(core.nba_cleanup_cycle())
        self.assertTrue(os.path.exists(os.path.join(self.addons, 'service.bigtime.nba')))

    def test_nba_len_dalsie_zvysky_bez_instancie(self):
        self.nba_stav(instancia=False, doplnok=True)
        core.nba_cleanup_cycle()
        core.reset_pending()
        self.assertTrue(core.nba_cleanup_cycle())
        self.assertFalse(os.path.exists(os.path.join(self.ud, 'addon_data', 'service.bigtime.nba')))
        self.assertEqual(core.load_state()['nba_cleanup']['phase'], 'done')
        self.assertEqual(KODI.notifications, [])

    def test_nba_nezmaze_instanciu_ktoru_uzivatel_zmenil(self):
        self.nba_stav(doplnok=False)
        core.nba_cleanup_cycle()
        mine = os.path.join(self.tmp, 'moj.m3u')
        write(mine, b'#EXTM3U\nhttp://example.invalid/m\n')
        write(os.path.join(self.sdir, 'instance-settings-4.xml'),
              iptv.build_settings_xml(mine, 'e', 'Moja').encode())
        core.reset_pending()
        core.nba_cleanup_cycle()
        self.assertTrue(os.path.exists(os.path.join(self.sdir, 'instance-settings-4.xml')))
        self.assertEqual(core.load_state()['nba_cleanup']['removed'], [])

    def test_nba_cycle_ho_vola_a_chyba_nezhodi_sluzbu(self):
        self.nba_stav()
        core.cycle(patches=False)
        self.assertEqual(core.load_state()['nba_cleanup']['phase'], 'addon_removed')
        real = core.nba_cleanup_cycle
        core.nba_cleanup_cycle = lambda: 1 / 0
        try:
            core.cycle(patches=False)                           # výnimka sa zachytí
        finally:
            core.nba_cleanup_cycle = real


if __name__ == '__main__':
    unittest.main()
