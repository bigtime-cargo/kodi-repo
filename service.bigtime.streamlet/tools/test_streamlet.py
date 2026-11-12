# Offline testy: python3 -m unittest tools.test_streamlet -v  (z adresára doplnku)
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
        n, changed, written = streamlet.refresh('https://x/p.m3u', self.path, opener=opener_for(make_playlist()))
        self.assertEqual((n, changed, written), (SAMPLE_CHANNELS, SAMPLE_CHANNELS, True))
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


# --- služba so stubmi Kodi -----------------------------------------------------

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

        def fake_refresh(url, path, timeout=30, opener=None):
            self.calls += 1
            if self.fail:
                raise OSError('bez siete')
            return self.real_refresh(url, path, opener=opener_for(make_playlist()))
        streamlet.refresh = fake_refresh

    def tearDown(self):
        streamlet.refresh = self.real_refresh
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

    def test_status_lines(self):
        self.core.cycle()
        text = '\n'.join(self.core.status_lines())
        self.assertIn('30 kanálov', text)
        self.assertIn('nastavená', text)


if __name__ == '__main__':
    unittest.main()
