# Offline testy: python3 -m unittest tools.test_streamlet -v  (z adresára doplnku)
import gzip
import io
import json
import os
import shutil
import sys
import tempfile
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import streamlet  # noqa: E402

SAMPLE_CHANNELS = 30


def make_playlist(n=SAMPLE_CHANNELS, suffix='@SD'):
    rows = ['#EXTM3U']
    for i in range(n):
        rows.append('#EXTINF:-1 tvg-id="Kanal%d.cz%s",Kanál %d' % (i, suffix, i))
        rows.append('http://example.invalid/%d.m3u8' % i)
    return '\n'.join(rows) + '\n'


def make_epg(n=3000):
    rows = ['<?xml version="1.0" encoding="UTF-8"?>', '<tv generator-info-name="test">',
            '<channel id="HBO.cz"><display-name>HBO</display-name></channel>']
    for i in range(n):
        rows.append('<programme start="20261112%04d00 +0100" stop="20261112%04d30 +0100" channel="HBO.cz">'
                    '<title>Porad %d s dlhším popisom, aby súbor mal rozumnú veľkosť</title></programme>'
                    % (i % 2400, i % 2400, i))
    rows.append('</tv>')
    return '\n'.join(rows).encode('utf-8')


LIMIT_PAGE = (b'<span style="font-size: 14px;color:red">You reached the download limit for this file '
              b'for today! Try again tomorrow...<br /></span>')


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def opener_for(data):
    if isinstance(data, str):
        data = data.encode('utf-8')

    def opener(req, timeout=0):
        return FakeResponse(data)
    return opener


def failing_opener(req, timeout=0):
    raise OSError('bez siete')


class FixPlaylist(unittest.TestCase):
    def test_strips_suffix(self):
        text = ('#EXTINF:-1 tvg-id="HBO.cz@SD",HBO\nu\n'
                '#EXTINF:-1 tvg-id="Cinemax.cz@Czechia",Cinemax\nu\n'
                '#EXTINF:-1 tvg-id="Sport1.cz@HD",S\nu\n')
        out, changed = streamlet.fix_playlist(text)
        self.assertEqual(changed, 3)
        self.assertIn('tvg-id="HBO.cz"', out)
        self.assertIn('tvg-id="Cinemax.cz"', out)
        self.assertNotIn('@', out)

    def test_leaves_rest_untouched(self):
        text = '#EXTINF:-1 tvg-logo="http://x/a@b.png" tvg-id="A.cz@SD",Meno @ test\nhttp://u/a@b\n'
        out, _ = streamlet.fix_playlist(text)
        self.assertIn('tvg-logo="http://x/a@b.png"', out)
        self.assertIn(',Meno @ test', out)
        self.assertIn('http://u/a@b', out)

    def test_idempotent_and_no_id(self):
        text = '#EXTINF:-1,Bez ID\nu\n#EXTINF:-1 tvg-id="A.cz",A\nu\n'
        out, changed = streamlet.fix_playlist(text)
        self.assertEqual((out, changed), (text, 0))


class Validate(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(streamlet.validate(make_playlist()), SAMPLE_CHANNELS)

    def test_html_rejected(self):
        with self.assertRaises(streamlet.PlaylistError):
            streamlet.validate('<html>chyba</html>')

    def test_too_small(self):
        with self.assertRaises(streamlet.PlaylistError):
            streamlet.validate(make_playlist(5))

    def test_shrinks_too_much(self):
        with self.assertRaises(streamlet.PlaylistError):
            streamlet.validate(make_playlist(25), old_count=100)
        self.assertEqual(streamlet.validate(make_playlist(60), old_count=100), 60)


class Refresh(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.path = os.path.join(self.d, 'sub', 'streamlet-cz.m3u')

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_writes_fixed_playlist(self):
        n, changed, written, removed = streamlet.refresh(
            'https://x/p.m3u', self.path, opener=opener_for(make_playlist()))
        self.assertEqual((n, changed, written, removed), (SAMPLE_CHANNELS, SAMPLE_CHANNELS, True, []))
        with open(self.path, encoding='utf-8') as f:
            self.assertNotIn('@', f.read())
        self.assertEqual(oct(os.stat(self.path).st_mode & 0o777), '0o644')

    def test_second_run_no_change(self):
        o = opener_for(make_playlist())
        streamlet.refresh('https://x/p.m3u', self.path, opener=o)
        self.assertFalse(streamlet.refresh('https://x/p.m3u', self.path, opener=o)[2])

    def test_network_error_keeps_old_file(self):
        streamlet.refresh('https://x/p.m3u', self.path, opener=opener_for(make_playlist()))
        before = open(self.path, 'rb').read()
        with self.assertRaises(OSError):
            streamlet.refresh('https://x/p.m3u', self.path, opener=failing_opener)
        self.assertEqual(open(self.path, 'rb').read(), before)

    def test_garbage_keeps_old_file(self):
        streamlet.refresh('https://x/p.m3u', self.path, opener=opener_for(make_playlist()))
        before = open(self.path, 'rb').read()
        with self.assertRaises(streamlet.PlaylistError):
            streamlet.refresh('https://x/p.m3u', self.path, opener=opener_for('<html>503</html>'))
        self.assertEqual(open(self.path, 'rb').read(), before)

    def test_no_tmp_files_left(self):
        streamlet.refresh('https://x/p.m3u', self.path, opener=opener_for(make_playlist()))
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ['streamlet-cz.m3u'])

    def test_rejects_non_http_url(self):
        with self.assertRaises(streamlet.PlaylistError):
            streamlet.download('file:///etc/passwd')

    def test_too_big(self):
        big = '#EXTM3U\n' + 'x' * (streamlet.MAX_BYTES + 10)
        with self.assertRaises(streamlet.PlaylistError):
            streamlet.download('https://x/p.m3u', opener=opener_for(big))


class Instances(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.local = os.path.join(self.d, 'data', 'streamlet-cz.m3u')

    def tearDown(self):
        shutil.rmtree(self.d)

    def put(self, num, **values):
        rows = ''.join('<setting id="%s">%s</setting>' % kv for kv in values.items())
        with open(os.path.join(self.d, 'instance-settings-%d.xml' % num), 'w') as f:
            f.write('<settings version="2">%s</settings>' % rows)

    def test_empty_needs_instance(self):
        self.assertEqual(streamlet.status(self.d, self.local), streamlet.TREBA)

    def test_missing_dir(self):
        self.assertEqual(streamlet.status(os.path.join(self.d, 'nie'), self.local), streamlet.TREBA)

    def test_other_instance_does_not_count(self):
        self.put(1, m3uPath='/niekde/inde.m3u', m3uUrl='http://100.89.178.46:3001/playlist')
        self.assertEqual(streamlet.status(self.d, self.local), streamlet.TREBA)

    def test_ours(self):
        self.put(3, m3uPath=self.local)
        self.assertEqual(streamlet.status(self.d, self.local), streamlet.NASTAVENY)

    def test_ours_via_special(self):
        self.put(1, m3uPath='special://x/streamlet-cz.m3u')
        tr = lambda p: self.local if p.startswith('special://') else p
        self.assertEqual(streamlet.status(self.d, self.local, tr), streamlet.NASTAVENY)

    def test_manual_url_is_reported_not_replaced(self):
        self.put(2, m3uUrl='https://repo.streamlet.info/playlists/cz.m3u')
        self.assertEqual(streamlet.status(self.d, self.local), streamlet.RUCNA)

    def test_ours_wins_over_manual(self):
        self.put(1, m3uUrl='https://repo.streamlet.info/playlists/cz.m3u')
        self.put(2, m3uPath=self.local)
        self.assertEqual(streamlet.status(self.d, self.local), streamlet.NASTAVENY)

    def test_create_uses_free_number_and_keeps_others(self):
        self.put(1, m3uPath='/a.m3u')
        self.put(3, m3uPath='/b.m3u')
        before = {n: open(p, 'rb').read() for n, p in streamlet.list_instances(self.d)}
        target = streamlet.create_instance(self.d, 'special://u/streamlet-cz.m3u', 'https://e/epg.xml')
        self.assertTrue(target.endswith('instance-settings-2.xml'))
        for n, p in streamlet.list_instances(self.d):
            if n in before:
                self.assertEqual(open(p, 'rb').read(), before[n])
        self.assertEqual(streamlet.read_setting(target, 'm3uPath'), 'special://u/streamlet-cz.m3u')
        self.assertEqual(streamlet.read_setting(target, 'epgUrl'), 'https://e/epg.xml')
        self.assertEqual(streamlet.read_setting(target, 'epgPathType'), '1')
        self.assertEqual(streamlet.read_setting(target, 'm3uRefreshMode'), '1')
        self.assertEqual([f for f in os.listdir(self.d) if f.startswith('.')], [])

    def test_create_escapes_xml(self):
        t = streamlet.create_instance(self.d, '/a&b.m3u', 'https://e/x?a=1&b=<2>')
        self.assertEqual(streamlet.read_setting(t, 'epgUrl'), 'https://e/x?a=1&b=<2>')

    def test_create_does_not_overwrite(self):
        orig = streamlet.free_number
        streamlet.free_number = lambda d: 1
        try:
            self.put(1, m3uPath='/a.m3u')
            with self.assertRaises(FileExistsError):
                streamlet.create_instance(self.d, '/x.m3u', 'https://e/')
        finally:
            streamlet.free_number = orig
        self.assertEqual(streamlet.read_setting(os.path.join(self.d, 'instance-settings-1.xml'), 'm3uPath'), '/a.m3u')


# --- filter duplicít s O2 TV -----------------------------------------------------

O2_SAMPLE = """#EXTM3U
#EXTINF:-1 tvg-id="1" tvg-name="Jednotka" tvg-chno="1",Jednotka
plugin://plugin.video.o2tv/?action=play&cid=1
#EXTINF:-1 tvg-id="2" tvg-name="ČT1" tvg-chno="2",ČT1
plugin://plugin.video.o2tv/?action=play&cid=2
#EXTINF:-1 tvg-id="3" tvg-name="Kino Barrandov",Kino Barrandov
plugin://plugin.video.o2tv/?action=play&cid=3
#EXTINF:-1 tvg-id="4" tvg-name="Barrandov TV",Barrandov TV
plugin://plugin.video.o2tv/?action=play&cid=4
#EXTINF:-1 tvg-id="5" tvg-name="Prima COOL SK",Prima COOL SK
plugin://plugin.video.o2tv/?action=play&cid=5
#EXTINF:-1 tvg-id="6" tvg-name="Šláger Original",Šláger Original
plugin://plugin.video.o2tv/?action=play&cid=6
#EXTINF:-1 tvg-id="7" tvg-name="KVIFF.TV",KVIFF.TV
plugin://plugin.video.o2tv/?action=play&cid=7
#EXTINF:-1 tvg-id="8" tvg-name="Markíza &amp; spol.",Markíza
plugin://plugin.video.o2tv/?action=play&cid=8
"""

STREAMLET_SAMPLE = """#EXTM3U
#EXTINF:-1 tvg-id="HBO.cz",HBO
http://a/hbo.m3u8
#EXTINF:-1 tvg-id="CT1.cz",ČT 1
#EXTVLCOPT:http-user-agent=Mozilla/5.0
http://a/ct1.m3u8
#EXTINF:-1 tvg-id="KinoBarrandov.cz",Kino Barrandov
http://a/kino.m3u8
#EXTINF:-1 tvg-id="TVBarrandov.cz",TV Barrandov
http://a/tvb.m3u8
#EXTINF:-1 tvg-id="PrimaCool.cz",Prima Cool
http://a/pc.m3u8
#EXTINF:-1 tvg-id="SlagerOriginal.cz",Slager Original
http://a/so.m3u8
#EXTINF:-1 tvg-id="KVIFFTV.cz",KVIFF TV (1080p)
http://a/kv.m3u8
#EXTINF:-1 tvg-id="Jednotka.cz",Jednotka [Geo-blocked]
http://a/j.m3u8
"""


class DedupeO2(unittest.TestCase):
    def keys(self):
        return streamlet.o2_keys(O2_SAMPLE)

    def test_matching_rules(self):
        k = self.keys()
        for name in ('ČT 1', 'Kino Barrandov', 'TV Barrandov', 'Slager Original',
                     'KVIFF TV (1080p)', 'Jednotka [Geo-blocked]', 'kino  BARRANDOV'):
            self.assertTrue(streamlet.name_keys(name) & k, name)
        for name in ('HBO', 'Prima Cool', 'Prima Krimi', 'ČT 2', 'Sport1'):
            self.assertFalse(streamlet.name_keys(name) & k, name)

    def test_filter_removes_only_duplicates(self):
        out, removed = streamlet.filter_duplicates(STREAMLET_SAMPLE, self.keys())
        self.assertEqual(removed, ['ČT 1', 'Kino Barrandov', 'TV Barrandov', 'Slager Original',
                                   'KVIFF TV (1080p)', 'Jednotka [Geo-blocked]'])
        self.assertEqual(streamlet.count_channels(out), 2)
        self.assertIn('http://a/hbo.m3u8', out)
        self.assertIn('http://a/pc.m3u8', out)
        self.assertTrue(out.startswith('#EXTM3U'))

    def test_filter_drops_attached_lines_with_entry(self):
        out, _ = streamlet.filter_duplicates(STREAMLET_SAMPLE, self.keys())
        self.assertNotIn('EXTVLCOPT', out)         # patril k ČT 1
        self.assertNotIn('ct1.m3u8', out)

    def test_no_keys_no_change(self):
        for keys in (None, set()):
            out, removed = streamlet.filter_duplicates(STREAMLET_SAMPLE, keys)
            self.assertEqual((out, removed), (STREAMLET_SAMPLE, []))

    def test_short_or_broken_o2_playlist_ignored(self):
        self.assertIsNone(streamlet.o2_keys(None))
        self.assertIsNone(streamlet.o2_keys('<html>'))
        self.assertIsNone(streamlet.o2_keys('#EXTM3U\n#EXTINF:-1 tvg-name="Jednotka",Jednotka\nu\n'))

    def test_xml_entities_and_short_names(self):
        self.assertTrue(streamlet.name_keys('Markíza & spol.') & self.keys())
        self.assertEqual(streamlet.name_keys(': 24'), set())     # príliš krátky názov

    def test_signature_changes_with_keys(self):
        a = streamlet.keys_signature(self.keys())
        b = streamlet.keys_signature(streamlet.o2_keys(O2_SAMPLE + '#EXTINF:-1 tvg-name="Nova",Nova\nu\n'))
        self.assertNotEqual(a, b)
        self.assertEqual(streamlet.keys_signature(None), '')

    def test_refresh_writes_raw_and_filtered(self):
        d = tempfile.mkdtemp()
        try:
            local, raw = os.path.join(d, 'l.m3u'), os.path.join(d, 'raw.m3u')
            big = STREAMLET_SAMPLE + make_playlist(25)
            n, _c, written, removed = streamlet.refresh(
                'https://x/p.m3u', local, raw, self.keys(), opener=opener_for(big))
            self.assertEqual(n, 7 + 1 + 25 - 1 + 1)     # 8 ukážkových + 25 vygenerovaných
            self.assertEqual(len(removed), 6)
            self.assertTrue(written)
            self.assertEqual(streamlet.count_channels(open(raw, encoding='utf-8').read()), n)
            self.assertEqual(streamlet.count_channels(open(local, encoding='utf-8').read()), n - 6)
            # zmena zoznamu O2: lokálne prefiltrovanie bez siete
            written, removed = streamlet.refilter(raw, local, None)
            self.assertEqual(removed, [])
            self.assertEqual(streamlet.count_channels(open(local, encoding='utf-8').read()), n)
            self.assertIsNone(streamlet.refilter(os.path.join(d, 'nie.m3u'), local, None))
        finally:
            shutil.rmtree(d)


# --- služba so stubmi Kodi -----------------------------------------------------

class DownloadEpg(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dest = os.path.join(self.tmp, 'sub', 'epg.xml')

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def leftovers(self):
        d = os.path.dirname(self.dest)
        return [n for n in os.listdir(d) if n.endswith('.tmp')] if os.path.isdir(d) else []

    def test_plain_xml(self):
        data = make_epg()
        size = streamlet.download_epg('https://x/epg.xml', self.dest, opener=opener_for(data))
        self.assertEqual(size, len(data))
        self.assertEqual(open(self.dest, 'rb').read(), data)
        self.assertTrue(streamlet.epg_valid(self.dest))
        self.assertEqual(self.leftovers(), [])

    def test_gzip_is_unpacked(self):
        data = make_epg()
        streamlet.download_epg('https://x/epg.xml', self.dest, opener=opener_for(gzip.compress(data)))
        self.assertEqual(open(self.dest, 'rb').read(), data)

    def test_limit_page_rejected_and_old_file_kept(self):
        streamlet.download_epg('https://x/epg.xml', self.dest, opener=opener_for(make_epg()))
        old = open(self.dest, 'rb').read()
        for page in (LIMIT_PAGE, gzip.compress(LIMIT_PAGE)):
            with self.assertRaises(streamlet.EpgError) as cm:
                streamlet.download_epg('https://x/epg.xml', self.dest, opener=opener_for(page))
            self.assertIn('download limit', str(cm.exception))
        self.assertEqual(open(self.dest, 'rb').read(), old)
        self.assertEqual(self.leftovers(), [])

    def test_truncated_xml_rejected(self):
        data = make_epg()[:-300]
        with self.assertRaises(streamlet.EpgError):
            streamlet.download_epg('https://x/epg.xml', self.dest, opener=opener_for(data))
        self.assertFalse(os.path.exists(self.dest))
        self.assertEqual(self.leftovers(), [])

    def test_too_small_rejected(self):
        with self.assertRaises(streamlet.EpgError):
            streamlet.download_epg('https://x/epg.xml', self.dest,
                                   opener=opener_for('<tv>\n</tv>'))

    def test_network_error_keeps_old_file(self):
        streamlet.download_epg('https://x/epg.xml', self.dest, opener=opener_for(make_epg()))
        old = open(self.dest, 'rb').read()
        with self.assertRaises(OSError):
            streamlet.download_epg('https://x/epg.xml', self.dest, opener=failing_opener)
        self.assertEqual(open(self.dest, 'rb').read(), old)
        self.assertEqual(self.leftovers(), [])

    def test_too_big_rejected(self):
        old = streamlet.MAX_EPG_BYTES
        streamlet.MAX_EPG_BYTES = 100 * 1024
        try:
            with self.assertRaises(streamlet.EpgError):
                streamlet.download_epg('https://x/epg.xml', self.dest, opener=opener_for(make_epg()))
        finally:
            streamlet.MAX_EPG_BYTES = old
        self.assertEqual(self.leftovers(), [])

    def test_rejects_non_http_url(self):
        with self.assertRaises(streamlet.EpgError):
            streamlet.download_epg('file:///etc/passwd', self.dest, opener=opener_for(make_epg()))

    def test_epg_valid_missing(self):
        self.assertFalse(streamlet.epg_valid(self.dest))


class MigrateEpg(unittest.TestCase):
    URL = 'https://www.open-epg.com/files/czech.xml'
    LOCAL = '/data/streamlet-cz.m3u'
    EPG = 'special://userdata/addon_data/service.bigtime.streamlet/streamlet-epg.xml'

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def write(self, n, body):
        with open(os.path.join(self.tmp, 'instance-settings-%d.xml' % n), 'w', encoding='utf-8') as f:
            f.write('<settings version="2">\n%s</settings>\n' % body)

    def migrate(self):
        return streamlet.migrate_epg(self.tmp, self.LOCAL, self.URL, self.EPG)

    def test_our_instance_switched_to_file(self):
        self.write(1, '<setting id="m3uPath">%s</setting>\n'
                      '<setting id="epgPathType" default="true">1</setting>\n'
                      '<setting id="epgPath" default="true" />\n'
                      '<setting id="epgUrl">%s</setting>\n' % (self.LOCAL, self.URL))
        self.assertIsNotNone(self.migrate())
        path = os.path.join(self.tmp, 'instance-settings-1.xml')
        self.assertEqual(streamlet.read_setting(path, 'epgPathType'), '0')
        self.assertEqual(streamlet.read_setting(path, 'epgPath'), self.EPG)
        self.assertEqual(streamlet.read_setting(path, 'm3uPath'), self.LOCAL)
        self.assertIsNone(self.migrate())           # druhýkrát už nič

    def test_missing_epg_keys_are_added(self):
        self.write(1, '<setting id="m3uPath">%s</setting>\n'
                      '<setting id="epgUrl">%s</setting>\n' % (self.LOCAL, self.URL))
        self.assertIsNotNone(self.migrate())
        path = os.path.join(self.tmp, 'instance-settings-1.xml')
        self.assertEqual(streamlet.read_setting(path, 'epgPathType'), '0')
        self.assertEqual(streamlet.read_setting(path, 'epgPath'), self.EPG)

    def test_foreign_instance_untouched(self):
        body = ('<setting id="m3uPath">/other/list.m3u</setting>\n'
                '<setting id="epgPathType">1</setting>\n<setting id="epgUrl">%s</setting>\n' % self.URL)
        self.write(1, body)
        before = open(os.path.join(self.tmp, 'instance-settings-1.xml'), 'rb').read()
        self.assertIsNone(self.migrate())
        self.assertEqual(open(os.path.join(self.tmp, 'instance-settings-1.xml'), 'rb').read(), before)

    def test_our_instance_with_other_epg_untouched(self):
        self.write(1, '<setting id="m3uPath">%s</setting>\n'
                      '<setting id="epgPathType">1</setting>\n'
                      '<setting id="epgUrl">https://iny.example/epg.xml</setting>\n' % self.LOCAL)
        self.assertIsNone(self.migrate())

    def test_our_instance_with_local_epg_untouched(self):
        self.write(1, '<setting id="m3uPath">%s</setting>\n'
                      '<setting id="epgPathType">0</setting>\n'
                      '<setting id="epgPath">/moje/epg.xml</setting>\n'
                      '<setting id="epgUrl">%s</setting>\n' % (self.LOCAL, self.URL))
        self.assertIsNone(self.migrate())
        self.assertEqual(streamlet.read_setting(
            os.path.join(self.tmp, 'instance-settings-1.xml'), 'epgPath'), '/moje/epg.xml')

    def test_create_with_local_epg(self):
        target = streamlet.create_instance(self.tmp, self.LOCAL, self.URL, epg_path=self.EPG)
        self.assertEqual(streamlet.read_setting(target, 'epgPathType'), '0')
        self.assertEqual(streamlet.read_setting(target, 'epgPath'), self.EPG)
        self.assertIsNone(streamlet.read_setting(target, 'epgUrl'))


class KodiStub:
    def __init__(self):
        self.iptv_enabled = True
        self.settings = {}
        self.toasts = []
        self.logs = []
        self.rpc = []


KODI = KodiStub()


def _install_stubs(tmp):
    xbmc = types.ModuleType('xbmc')
    xbmc.LOGINFO, xbmc.LOGWARNING, xbmc.LOGERROR = 1, 2, 3
    xbmc.log = lambda msg, level=1: KODI.logs.append(msg)
    xbmc.getCondVisibility = lambda expr: KODI.iptv_enabled
    xbmc.executeJSONRPC = lambda p: KODI.rpc.append(p) or '{}'

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
        def notification(self, heading, message, icon, ms):
            KODI.toasts.append(message)
    xbmcgui.Dialog = Dialog

    xbmcvfs = types.ModuleType('xbmcvfs')
    xbmcvfs.translatePath = lambda p: p.replace('special://userdata', tmp)
    for name, mod in (('xbmc', xbmc), ('xbmcaddon', xbmcaddon), ('xbmcgui', xbmcgui), ('xbmcvfs', xbmcvfs)):
        sys.modules[name] = mod


class Core(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        _install_stubs(self.tmp)
        sys.modules.pop('streamlet_core', None)
        import streamlet_core
        self.core = streamlet_core
        KODI.__init__()
        self.real_refresh = streamlet.refresh
        self.calls = 0
        self.fail = False

        def fake_refresh(url, path, raw_path=None, o2=None, timeout=30, opener=None):
            self.calls += 1
            if self.fail:
                raise OSError('bez siete')
            return self.real_refresh(url, path, raw_path, o2, opener=opener_for(self.playlist))
        self.playlist = make_playlist()
        streamlet.refresh = fake_refresh

        self.real_download_epg = streamlet.download_epg
        self.epg_calls = 0
        self.epg_data = make_epg()

        def fake_download_epg(url, dest, timeout=90, opener=None):
            self.epg_calls += 1
            return self.real_download_epg(url, dest, opener=opener_for(self.epg_data))
        streamlet.download_epg = fake_download_epg

    def tearDown(self):
        streamlet.refresh = self.real_refresh
        streamlet.download_epg = self.real_download_epg
        shutil.rmtree(self.tmp)

    def instance_files(self):
        return [n for n, _ in streamlet.list_instances(self.core.iptv_dir())]

    def test_first_cycle_downloads_and_creates_instance(self):
        state = self.core.cycle()
        self.assertTrue(os.path.isfile(self.core.local_path()))
        self.assertEqual(self.instance_files(), [1])
        self.assertEqual(state['channels'], SAMPLE_CHANNELS)
        self.assertEqual(len(KODI.toasts), 1)

    def test_second_cycle_does_nothing(self):
        self.core.cycle()
        self.core.cycle()
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.instance_files(), [1])
        self.assertEqual(len(KODI.toasts), 1)

    def test_interval_from_settings(self):
        self.core.cycle()
        state = self.core.load_state()
        state['last_ok'] -= 5 * 3600
        KODI.settings['refresh_hours'] = '4'
        self.assertTrue(self.core.is_due(state))
        KODI.settings['refresh_hours'] = '12'
        self.assertFalse(self.core.is_due(state))

    def test_failure_keeps_file_and_waits(self):
        self.core.cycle()
        state = self.core.load_state()
        state['last_ok'] -= 24 * 3600
        self.core.save_state(state)
        self.fail = True
        self.core.cycle()
        self.assertEqual(self.calls, 2)
        self.core.cycle()           # hneď po zlyhaní sa nepokúša znova
        self.assertEqual(self.calls, 2)
        self.assertTrue(os.path.isfile(self.core.local_path()))
        self.assertIn('last_fail', self.core.load_state())

    def test_retry_after_pause(self):
        self.fail = True
        self.core.cycle()
        state = self.core.load_state()
        self.assertFalse(os.path.isfile(self.core.local_path()))
        state['last_try'] -= self.core.RETRY_AFTER + 1
        self.assertTrue(self.core.is_due(state))
        self.fail = False
        self.core.save_state(state)
        self.core.cycle()
        self.assertTrue(os.path.isfile(self.core.local_path()))

    def test_no_instance_without_iptv(self):
        KODI.iptv_enabled = False
        self.core.cycle()
        self.assertTrue(os.path.isfile(self.core.local_path()))
        self.assertEqual(self.instance_files(), [])

    def test_manual_instance_untouched(self):
        d = self.core.iptv_dir()
        os.makedirs(d)
        p = os.path.join(d, 'instance-settings-2.xml')
        data = ('<settings version="2"><setting id="m3uUrl">https://repo.streamlet.info/playlists/cz.m3u'
                '</setting></settings>')
        with open(p, 'w') as f:
            f.write(data)
        self.core.cycle()
        self.assertEqual(self.instance_files(), [2])
        self.assertEqual(open(p).read(), data)
        self.assertEqual(KODI.toasts, [])

    def test_never_toggles_addons(self):
        self.core.cycle()
        self.assertEqual(KODI.rpc, [])
        for fn in ('streamlet.py', 'streamlet_core.py', 'service.py', 'default.py'):
            with open(os.path.join(ROOT, fn), encoding='utf-8') as f:
                src = f.read()
            self.assertNotIn('SetAddonEnabled', src)

    # --- filter duplicít v službe ---

    def put_o2_playlist(self, text=O2_SAMPLE):
        path = self.core.o2_playlist_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)

    def local_names(self):
        text = open(self.core.local_path(), encoding='utf-8').read()
        return [l.rsplit(',', 1)[1] for l in text.split('\n') if l.startswith('#EXTINF')]

    def test_filter_applied_on_download(self):
        self.playlist = STREAMLET_SAMPLE + make_playlist(25)
        self.put_o2_playlist()
        state = self.core.cycle()
        names = self.local_names()
        self.assertNotIn('ČT 1', names)
        self.assertIn('HBO', names)
        self.assertEqual(len(state['removed']), 6)
        self.assertIn('Vynechané', '\n'.join(self.core.status_lines()))

    def test_late_o2_export_refilters_without_download(self):
        self.playlist = STREAMLET_SAMPLE + make_playlist(25)
        self.core.cycle()                           # O2 ešte nemá export
        self.assertIn('ČT 1', self.local_names())
        self.put_o2_playlist()
        self.core.cycle()
        self.assertEqual(self.calls, 1)             # bez nového sťahovania
        self.assertNotIn('ČT 1', self.local_names())
        self.assertEqual(len(self.core.load_state()['removed']), 6)

    def test_setting_off_restores_channels(self):
        self.playlist = STREAMLET_SAMPLE + make_playlist(25)
        self.put_o2_playlist()
        self.core.cycle()
        KODI.settings['dedupe_o2'] = 'false'
        self.core.cycle()
        self.assertIn('ČT 1', self.local_names())
        self.assertEqual(self.calls, 1)
        self.assertIn('vypnutý', '\n'.join(self.core.status_lines()))
        KODI.settings['dedupe_o2'] = 'true'
        self.core.cycle()
        self.assertNotIn('ČT 1', self.local_names())

    def test_unchanged_o2_does_not_rewrite(self):
        self.playlist = STREAMLET_SAMPLE + make_playlist(25)
        self.put_o2_playlist()
        self.core.cycle()
        before = os.stat(self.core.local_path()).st_mtime_ns
        self.core.cycle()
        self.assertEqual(os.stat(self.core.local_path()).st_mtime_ns, before)

    # --- EPG ---

    def epg_state(self):
        return self.core.load_state()

    def test_first_cycle_downloads_epg_and_instance_uses_it(self):
        self.core.cycle()
        self.assertEqual(self.epg_calls, 1)
        self.assertTrue(streamlet.epg_valid(self.core.epg_path()))
        path = os.path.join(self.core.iptv_dir(), 'instance-settings-1.xml')
        self.assertEqual(streamlet.read_setting(path, 'epgPathType'), '0')
        self.assertEqual(streamlet.read_setting(path, 'epgPath'), self.core.epg_special())

    def test_epg_not_downloaded_again_within_interval(self):
        self.core.cycle()
        self.core.cycle()
        self.assertEqual(self.epg_calls, 1)

    def test_epg_refreshed_after_interval(self):
        self.core.cycle()
        state = self.core.load_state()
        state['epg_ok'] -= 13 * 3600
        self.core.save_state(state)
        self.core.cycle()
        self.assertEqual(self.epg_calls, 2)

    def test_limit_page_keeps_instance_on_url_and_waits(self):
        self.epg_data = LIMIT_PAGE
        self.core.cycle()
        self.assertEqual(self.epg_calls, 1)
        self.assertIn('download limit', self.epg_state()['epg_fail'])
        path = os.path.join(self.core.iptv_dir(), 'instance-settings-1.xml')
        self.assertEqual(streamlet.read_setting(path, 'epgPathType'), '1')
        self.assertEqual(streamlet.read_setting(path, 'epgUrl'), streamlet.DEFAULT_EPG_URL)
        self.core.cycle()           # hneď po zlyhaní sa nepokúša znova
        self.assertEqual(self.epg_calls, 1)

    def test_epg_retry_after_pause_then_instance_migrates(self):
        self.epg_data = LIMIT_PAGE
        self.core.cycle()
        state = self.core.load_state()
        state['epg_try'] -= self.core.EPG_RETRY_AFTER + 1
        self.core.save_state(state)
        self.epg_data = make_epg()
        KODI.toasts[:] = []
        self.core.cycle()
        self.assertEqual(self.epg_calls, 2)
        path = os.path.join(self.core.iptv_dir(), 'instance-settings-1.xml')
        self.assertEqual(streamlet.read_setting(path, 'epgPathType'), '0')
        self.assertEqual(len(KODI.toasts), 1)
        self.core.cycle()
        self.assertEqual(len(KODI.toasts), 1)       # upozornenie len raz

    def test_epg_local_switch_off(self):
        KODI.settings['epg_local'] = 'false'
        self.core.cycle()
        self.assertEqual(self.epg_calls, 0)
        path = os.path.join(self.core.iptv_dir(), 'instance-settings-1.xml')
        self.assertEqual(streamlet.read_setting(path, 'epgUrl'), streamlet.DEFAULT_EPG_URL)

    def test_force_retries_epg_despite_failure_wait(self):
        self.epg_data = LIMIT_PAGE
        self.core.cycle()
        self.epg_data = make_epg()
        self.core.cycle(force=True)
        self.assertEqual(self.epg_calls, 2)
        self.assertTrue(streamlet.epg_valid(self.core.epg_path()))

    def test_status_lines_mention_epg(self):
        self.core.cycle()
        self.assertTrue(any(l.startswith('EPG:') for l in self.core.status_lines()))

    def test_status_lines(self):
        self.core.cycle()
        text = '\n'.join(self.core.status_lines())
        self.assertIn('30 kanálov', text)
        self.assertIn('nastavená', text)


if __name__ == '__main__':
    unittest.main()
