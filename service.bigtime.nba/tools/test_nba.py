# Offline testy: python3 -m unittest tools.test_nba -v  (z adresára doplnku)
import io
import os
import shutil
import sys
import tempfile
import types
import unittest
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import nba  # noqa: E402

FILLER = 250

SPORT = [
    ('tvg-id="ESPN2.us@HD"', 'ESPN 2 HD (720p)', 'http://e2.invalid/index.m3u8'),
    ('tvg-id="FoxSports1.us@HD"', 'Fox Sports 1 (720p)', 'http://fs1.invalid/index.m3u8'),
    ('tvg-id="NBATV.us@SD"', 'NBA TV (720p)', 'http://nba1.invalid/index.m3u8'),
    ('tvg-id="NBATV.us@SD"', 'NBA TV (1080p) [Geo-blocked]', 'http://nba2.invalid/index.m3u8'),
    ('tvg-id="ESPN.us@HD"', 'ESPN HD (720p)', 'http://espn.invalid/index.m3u8'),
    ('tvg-id="TNT.us@East"', 'TNT (720p)', 'http://tnt.invalid/index.m3u8'),
    ('tvg-id="NBCSportsNOW.us@SD"', 'NBC Sports NOW (1080p)', 'http://nbcs.invalid/master.m3u8'),
    ('tvg-id="NBCSportsNOW.us@SD"', 'NBC Sports NOW (720p)', 'http://nbcs2.invalid/master.m3u8'),
    ('tvg-id=""', 'TNT Novelas (1080p)', 'http://novelas.invalid/index.m3u8'),
    ('tvg-id="ABCNews.us@SD"', 'ABC News Live', 'http://abcn.invalid/index.m3u8'),
    ('tvg-id="ESPNU.us@HD"', 'ESPNU HD (720p)', 'http://espnu.invalid/index.m3u8'),
    ('tvg-id="ESPNEWS.us@HD"', 'ESPNews HD', 'http://espnews.invalid/index.m3u8'),
    ('tvg-id="ESPND.us@HD"', 'ESPN Deportes HD (720p)', 'http://espnd.invalid/index.m3u8'),
]


def make_us(extra=''):
    rows = ['#EXTM3U']
    for i in range(FILLER):
        rows.append('#EXTINF:-1 tvg-id="Kanal%d.us@SD",Kanál %d' % (i, i))
        rows.append('http://filler.invalid/%d.m3u8' % i)
    for tvg, name, url in SPORT:
        rows.append('#EXTINF:-1 tvg-logo="http://l/x.png" %s,%s' % (tvg, name))
        rows.append(url)
    rows.append('#EXTINF:-1 tvg-id="ESPNX.us@SD",ESPN S hlavičkou')
    rows.append('#EXTVLCOPT:http-user-agent=Mozilla')
    rows.append('http://hdr.invalid/index.m3u8')
    return '\n'.join(rows) + '\n' + extra


def make_epg(n=3000):
    rows = ['<?xml version="1.0" encoding="UTF-8"?>', '<tv generator-info-name="test">']
    for cid in ('NBATV.us', 'ESPN.us', 'ESPN2.us', 'FS1.us', 'ABC.us'):
        rows.append('<channel id="%s"><display-name>%s</display-name></channel>' % (cid, cid))
    for i in range(n):
        rows.append('<programme start="20261112%04d00 +0100" stop="20261112%04d30 +0100" channel="NBATV.us">'
                    '<title>Porad %d s dlhším popisom, aby súbor mal rozumnú veľkosť</title></programme>'
                    % (i % 2400, i % 2400, i))
    rows.append('</tv>')
    return '\n'.join(rows).encode('utf-8')


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def http_error(url, code=403):
    return urllib.error.HTTPError(url, code, 'x', {}, None)


SEGMENT = b'\x47' + b'\x00' * 1999


def streams_opener(down=(), plain=()):
    """Opener: každý host z SPORT má media playlist; hosty v `plain` majú master
    playlist, hosty v `down` vrátia 403."""
    def opener(req, timeout=0):
        url = req.full_url
        host = url.split('/')[2]
        if any(host.startswith(d) for d in down):
            raise http_error(url)
        if url.endswith('seg.ts'):
            return FakeResponse(SEGMENT)
        if url.endswith('media.m3u8'):
            return FakeResponse(b'#EXTM3U\n#EXTINF:6,\nseg0.ts\n#EXTINF:6,\nseg.ts\n')
        if url.endswith('master.m3u8'):
            return FakeResponse(b'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nmedia.m3u8\n')
        return FakeResponse(b'#EXTM3U\n#EXTINF:6,\nseg.ts\n')
    return opener


def opener_for(data):
    if isinstance(data, str):
        data = data.encode('utf-8')

    def opener(req, timeout=0):
        return FakeResponse(data)
    return opener


class Select(unittest.TestCase):
    def setUp(self):
        self.chosen = nba.candidates(make_us())
        self.names = [nba.entry_name(e) for e, _o, _u in self.chosen]

    def test_picks_sports_only(self):
        self.assertIn('NBA TV (720p)', self.names)
        self.assertIn('ESPN 2 HD (720p)', self.names)
        self.assertIn('Fox Sports 1 (720p)', self.names)
        self.assertIn('TNT (720p)', self.names)
        self.assertIn('ESPNU HD (720p)', self.names)
        self.assertNotIn('ESPNews HD', self.names)
        self.assertNotIn('ESPN Deportes HD (720p)', self.names)
        self.assertFalse([n for n in self.names if n.startswith('Kanál')])

    def test_excludes_novelas_and_news(self):
        self.assertNotIn('TNT Novelas (1080p)', self.names)
        self.assertNotIn('ABC News Live', self.names)

    def test_skips_entries_with_own_headers(self):
        self.assertNotIn('ESPN S hlavičkou', self.names)

    def test_duplicate_names_keep_first(self):
        nbcs = [n for n in self.names if n.startswith('NBC Sports NOW')]
        self.assertEqual(nbcs, ['NBC Sports NOW (1080p)'])
        self.assertEqual(self.chosen[[n for n in self.names].index('NBC Sports NOW (1080p)')][2],
                         'http://nbcs.invalid/master.m3u8')

    def test_tvg_id_suffix_and_alias(self):
        text = nba.serialize(self.chosen)
        for line in text.splitlines():
            if line.startswith('#EXTINF'):
                self.assertNotIn('@', line)
        self.assertIn('tvg-id="FS1.us"', text)
        self.assertNotIn('FoxSports1', text)
        self.assertIn('tvg-id="NBATV.us"', text)
        self.assertIn('tvg-logo="http://l/x.png"', text)

    def test_rejects_non_playlist(self):
        with self.assertRaises(nba.PlaylistError):
            nba.candidates('<html>nope</html>')
        with self.assertRaises(nba.PlaylistError):
            nba.candidates('#EXTM3U\n#EXTINF:-1,ESPN\nhttp://x/y.m3u8\n')

    def test_rejects_without_sport(self):
        rows = ['#EXTM3U'] + ['#EXTINF:-1,Kanal %d\nhttp://x/%d' % (i, i) for i in range(300)]
        with self.assertRaises(nba.PlaylistError):
            nba.candidates('\n'.join(rows))


class Render(unittest.TestCase):
    def test_group_and_numbering(self):
        chosen = nba.candidates(make_us())
        text = nba.render(chosen, 201)
        items = nba.parse(text)
        self.assertEqual(len(items), len(chosen))
        for extinf, _o, _u in items:
            self.assertEqual(extinf.count('group-title="NBA"'), 1)
            self.assertEqual(extinf.count('tvg-chno='), 1)
        by_name = {nba.entry_name(e): e for e, _o, _u in items}
        self.assertIn('tvg-chno="201"', by_name['NBA TV (720p)'])      # NBA TV prvá
        espn = [n for n in by_name if n.startswith('ESPN')]
        self.assertTrue(all(int(by_name[n].split('tvg-chno="')[1].split('"')[0]) > 201 for n in espn))
        nums = sorted(int(e.split('tvg-chno="')[1].split('"')[0]) for e in by_name.values())
        self.assertEqual(nums, list(range(201, 201 + len(items))))

    def test_idempotent_and_renumber(self):
        text = nba.render(nba.candidates(make_us()), 201)
        again = nba.render(nba.parse(text), 201)
        self.assertEqual(text, again)
        moved = nba.render(nba.parse(text), 500)
        self.assertIn('tvg-chno="500"', moved)
        self.assertNotIn('tvg-chno="201"', moved)
        self.assertEqual(moved.count('group-title='), text.count('group-title='))

    def test_comma_in_attribute_does_not_split_name(self):
        line = '#EXTINF:-1 tvg-logo="http://x/a,b.png" tvg-id="NBATV.us",NBA TV'
        out = nba.with_attrs(line, 'NBA', 7)
        self.assertTrue(out.endswith(',NBA TV'))
        self.assertIn('tvg-logo="http://x/a,b.png"', out)
        self.assertIn('group-title="NBA"', out)
        self.assertIn('tvg-chno="7"', out)

    def test_existing_attrs_replaced(self):
        line = '#EXTINF:-1 group-title="Sport" tvg-chno="3" tvg-id="X",ESPN'
        out = nba.with_attrs(line, 'NBA', 9)
        self.assertEqual(out.count('group-title='), 1)
        self.assertEqual(out.count('tvg-chno='), 1)
        self.assertIn('group-title="NBA"', out)


class Check(unittest.TestCase):
    def test_media_playlist_ok(self):
        self.assertEqual(nba.check_stream('http://a.invalid/index.m3u8', opener=streams_opener()),
                         (True, 'ok'))

    def test_master_playlist_ok(self):
        self.assertTrue(nba.check_stream('http://a.invalid/master.m3u8', opener=streams_opener())[0])

    def test_http_error_fails(self):
        ok, why = nba.check_stream('http://down.invalid/index.m3u8', opener=streams_opener(down=('down',)))
        self.assertFalse(ok)
        self.assertEqual(why, 'HTTPError')

    def test_html_page_is_not_hls(self):
        ok, why = nba.check_stream('http://a/b.m3u8', opener=opener_for('<html>blokované</html>'))
        self.assertFalse(ok)

    def test_html_segment_fails(self):
        def op(req, timeout=0):
            if req.full_url.endswith('seg.ts'):
                return FakeResponse(b'<html>' + b'x' * 300)
            return FakeResponse(b'#EXTM3U\n#EXTINF:6,\nseg.ts\n')
        self.assertFalse(nba.check_stream('http://a/b.m3u8', opener=op)[0])

    def test_empty_manifest_fails(self):
        self.assertFalse(nba.check_stream('http://a/b.m3u8', opener=opener_for('#EXTM3U\n'))[0])

    def test_network_error_fails(self):
        def op(req, timeout=0):
            raise OSError('bez siete')
        self.assertFalse(nba.check_stream('http://a/b.m3u8', opener=op)[0])

    def test_check_all_and_abort(self):
        chosen = nba.candidates(make_us())
        res = nba.check_all(chosen, opener=streams_opener(down=('espn.',)))
        self.assertEqual(len(res), len(chosen))
        self.assertFalse(res['http://espn.invalid/index.m3u8'][0])
        self.assertTrue(res['http://nba1.invalid/index.m3u8'][0])
        self.assertEqual(nba.check_all(chosen, opener=streams_opener(), abort=lambda: True), {})


class Hysteresis(unittest.TestCase):
    def setUp(self):
        self.items = nba.candidates(make_us())
        self.bad = 'http://espn.invalid/index.m3u8'

    def results(self, bad_ok=False):
        return {u: (bad_ok or u != self.bad, '') for _e, _o, u in self.items}

    def test_first_failure_keeps_channel(self):
        kept, fails, dropped = nba.keep_working(self.items, self.results())
        self.assertEqual(len(kept), len(self.items))
        self.assertEqual(fails, {self.bad: 1})
        self.assertEqual(dropped, [])

    def test_second_failure_drops(self):
        kept, fails, dropped = nba.keep_working(self.items, self.results(), {self.bad: 1})
        self.assertEqual(len(kept), len(self.items) - 1)
        self.assertEqual(dropped, ['ESPN HD (720p)'])
        self.assertEqual(fails, {self.bad: 2})

    def test_recovery_resets_counter(self):
        kept, fails, dropped = nba.keep_working(self.items, self.results(True), {self.bad: 1})
        self.assertEqual(fails, {})
        self.assertEqual(len(kept), len(self.items))

    def test_dropped_channel_returns_when_working(self):
        kept, fails, _d = nba.keep_working(self.items, self.results(True), {self.bad: 5})
        self.assertIn(self.bad, [u for _e, _o, u in kept])

    def test_never_verified_failing_channel_dropped_at_once(self):
        known = {u for _e, _o, u in self.items if u != self.bad}
        kept, fails, dropped = nba.keep_working(self.items, self.results(), {}, known)
        self.assertEqual(dropped, ['ESPN HD (720p)'])
        self.assertNotIn(self.bad, [u for _e, _o, u in kept])

    def test_known_channel_gets_one_grace(self):
        known = {u for _e, _o, u in self.items}
        kept, fails, dropped = nba.keep_working(self.items, self.results(), {}, known)
        self.assertEqual(dropped, [])
        self.assertEqual(fails, {self.bad: 1})

    def test_missing_result_keeps_and_counter_unchanged(self):
        kept, fails, dropped = nba.keep_working(self.items, {}, {self.bad: 1})
        self.assertEqual(len(kept), len(self.items))
        self.assertEqual(fails, {self.bad: 1})


class Epg(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dest = os.path.join(self.tmp, 'epg.xml')

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_download_ok(self):
        size = nba.download_epg('http://x/e.xml', self.dest, opener=opener_for(make_epg()))
        self.assertGreater(size, nba.MIN_EPG_BYTES)
        self.assertTrue(nba.epg_valid(self.dest))

    def test_limit_page_rejected_and_old_kept(self):
        nba.download_epg('http://x/e.xml', self.dest, opener=opener_for(make_epg()))
        before = open(self.dest, 'rb').read()
        page = b'<span>You reached the download limit for this file for today!</span>'
        with self.assertRaises(nba.EpgError):
            nba.download_epg('http://x/e.xml', self.dest, opener=opener_for(page))
        self.assertEqual(open(self.dest, 'rb').read(), before)
        self.assertEqual([n for n in os.listdir(self.tmp) if n.startswith('.')], [])

    def test_truncated_rejected(self):
        with self.assertRaises(nba.EpgError):
            nba.download_epg('http://x/e.xml', self.dest, opener=opener_for(make_epg()[:-100]))

    def test_channel_ids_and_coverage(self):
        nba.download_epg('http://x/e.xml', self.dest, opener=opener_for(make_epg()))
        ids = nba.epg_channel_ids(self.dest)
        self.assertEqual(ids, {'nbatv.us', 'espn.us', 'espn2.us', 'fs1.us', 'abc.us'})
        text = nba.render(nba.candidates(make_us()))
        have, total = nba.epg_coverage(text, ids)
        self.assertEqual(total, 7)
        self.assertEqual(have, 4)          # NBA TV, ESPN, ESPN 2, FS1 (ESPNU, TNT a NBC Sports nie)

    def test_missing_file(self):
        self.assertIsNone(nba.epg_channel_ids(os.path.join(self.tmp, 'nie.xml')))
        self.assertFalse(nba.epg_valid(os.path.join(self.tmp, 'nie.xml')))


class Instances(unittest.TestCase):
    LOCAL = '/home/x/addon_data/service.bigtime.nba/nba-us.m3u'
    URL = nba.DEFAULT_EPG_URL
    EPG = 'special://userdata/addon_data/service.bigtime.nba/nba-epg.xml'

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_status_and_create(self):
        self.assertEqual(nba.status(self.tmp, self.LOCAL), nba.TREBA)
        target = nba.create_instance(self.tmp, self.LOCAL, self.URL)
        self.assertEqual(os.path.basename(target), 'instance-settings-1.xml')
        self.assertEqual(nba.status(self.tmp, self.LOCAL), nba.NASTAVENY)
        self.assertEqual(nba.read_setting(target, 'kodi_addon_instance_name'), 'NBA (Bigtime)')
        self.assertEqual(nba.read_setting(target, 'epgPathType'), '1')

    def test_does_not_touch_foreign_instances(self):
        foreign = os.path.join(self.tmp, 'instance-settings-1.xml')
        body = ('<settings version="2"><setting id="m3uPath">/iny.m3u</setting>'
                '<setting id="epgPathType">1</setting><setting id="epgUrl">%s</setting></settings>' % self.URL)
        open(foreign, 'w').write(body)
        self.assertEqual(nba.status(self.tmp, self.LOCAL), nba.TREBA)
        self.assertIsNone(nba.migrate_epg(self.tmp, self.LOCAL, self.URL, self.EPG))
        self.assertEqual(open(foreign).read(), body)
        target = nba.create_instance(self.tmp, self.LOCAL, self.URL)
        self.assertEqual(os.path.basename(target), 'instance-settings-2.xml')
        self.assertEqual(open(foreign).read(), body)

    def test_migrate_own_instance(self):
        target = nba.create_instance(self.tmp, self.LOCAL, self.URL)
        changed = nba.migrate_epg(self.tmp, self.LOCAL, self.URL, self.EPG)
        self.assertEqual(changed, target)
        self.assertEqual(nba.read_setting(target, 'epgPathType'), '0')
        self.assertEqual(nba.read_setting(target, 'epgPath'), self.EPG)
        self.assertIsNone(nba.migrate_epg(self.tmp, self.LOCAL, self.URL, self.EPG))

    def test_create_with_local_epg(self):
        target = nba.create_instance(self.tmp, self.LOCAL, self.URL, epg_path=self.EPG)
        self.assertEqual(nba.read_setting(target, 'epgPathType'), '0')
        self.assertEqual(nba.read_setting(target, 'epgPath'), self.EPG)
        self.assertIsNone(nba.read_setting(target, 'epgUrl'))


class KodiStub:
    def __init__(self):
        self.iptv_enabled = True
        self.settings = {}
        self.toasts = []
        self.logs = []


KODI = KodiStub()


def _install_stubs(tmp):
    xbmc = types.ModuleType('xbmc')
    xbmc.LOGINFO, xbmc.LOGWARNING, xbmc.LOGERROR = 1, 2, 3
    xbmc.log = lambda msg, level=1: KODI.logs.append(msg)
    xbmc.getCondVisibility = lambda expr: KODI.iptv_enabled

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
        sys.modules.pop('nba_core', None)
        import nba_core
        self.core = nba_core
        KODI.__init__()
        self.down = ()
        self.us = make_us()
        self.epg_data = make_epg()
        self.epg_net_fail = False
        self.real = (nba.download, nba.check_all, nba.download_epg)

        real_download, real_check_all, real_download_epg = self.real

        def fake_download(url, timeout=30, opener=None):
            return real_download(url, timeout, opener=opener_for(self.us))

        def fake_check_all(items, **kw):
            kw['opener'] = streams_opener(down=self.down)
            return real_check_all(items, **kw)

        def fake_download_epg(url, dest, timeout=90, opener=None):
            if self.epg_net_fail:
                raise OSError('bez siete')
            return real_download_epg(url, dest, opener=opener_for(self.epg_data))
        nba.download, nba.check_all, nba.download_epg = fake_download, fake_check_all, fake_download_epg

    def tearDown(self):
        nba.download, nba.check_all, nba.download_epg = self.real
        shutil.rmtree(self.tmp)

    def instances(self):
        return [n for n, _ in nba.list_instances(self.core.iptv_dir())]

    def local_names(self):
        return [nba.entry_name(e) for e, _o, _u in nba.parse(nba.read_text(self.core.local_path()))]

    def test_first_cycle(self):
        state = self.core.cycle()
        self.assertTrue(os.path.isfile(self.core.local_path()))
        self.assertTrue(nba.epg_valid(self.core.epg_path()))
        self.assertEqual(self.instances(), [1])
        self.assertEqual(state['channels'], len(self.local_names()))
        self.assertEqual(nba.read_setting(
            nba.list_instances(self.core.iptv_dir())[0][1], 'epgPathType'), '0')
        self.assertEqual(len(KODI.toasts), 1)
        self.assertIn('group-title="NBA"', nba.read_text(self.core.local_path()))

    def test_second_cycle_does_nothing(self):
        self.core.cycle()
        mtime = os.path.getmtime(self.core.local_path())
        KODI.toasts.clear()
        self.core.cycle()
        self.assertEqual(os.path.getmtime(self.core.local_path()), mtime)
        self.assertEqual(KODI.toasts, [])
        self.assertEqual(self.instances(), [1])

    def test_dead_stream_dropped_after_two_failures(self):
        self.core.cycle()
        before = len(self.local_names())
        self.down = ('espn.',)
        self.core.cycle(force=True)
        self.assertEqual(len(self.local_names()), before)              # prvé zlyhanie
        state = self.core.cycle(force=True)
        self.assertEqual(len(self.local_names()), before - 1)
        self.assertNotIn('ESPN HD (720p)', self.local_names())
        self.assertEqual(state['dropped'], ['ESPN HD (720p)'])
        self.down = ()
        self.core.cycle(force=True)
        self.assertIn('ESPN HD (720p)', self.local_names())

    def test_first_cycle_does_not_include_dead_streams(self):
        self.down = ('espn.', 'tnt')
        state = self.core.cycle()
        names = self.local_names()
        self.assertNotIn('ESPN HD (720p)', names)
        self.assertNotIn('TNT (720p)', names)
        self.assertEqual(sorted(state['dropped']), ['ESPN HD (720p)', 'TNT (720p)'])

    def test_state_from_1_0_0_refreshes_and_drops_dead(self):
        # stav z verzie 1.0.0: mŕtvy kanál v playliste s jedným zlyhaním, bez `algo`
        self.core.cycle()
        state = self.core.load_state()
        state.pop('algo')
        state['fails'] = {'http://espn.invalid/index.m3u8': 1}
        self.core.save_state(state)
        self.down = ('espn.',)
        self.assertTrue(self.core.is_due(self.core.load_state()))
        state = self.core.cycle()
        self.assertNotIn('ESPN HD (720p)', self.local_names())
        self.assertEqual(state['algo'], self.core.ALGO)

    def test_aborted_check_keeps_old_playlist(self):
        self.core.cycle()
        old = nba.read_text(self.core.local_path())
        state = self.core.load_state()
        self.core.refresh_playlist(state, abort=lambda: True)
        self.assertEqual(nba.read_text(self.core.local_path()), old)

    def test_all_streams_down_keeps_old_playlist(self):
        self.core.cycle()
        old = nba.read_text(self.core.local_path())
        self.down = ('nba1', 'nba2', 'espn', 'e2', 'fs1', 'tnt', 'nbcs')
        state = self.core.cycle(force=True)
        self.assertEqual(nba.read_text(self.core.local_path()), old)
        self.assertIn('last_fail', state)

    def test_bad_source_keeps_old_playlist(self):
        self.core.cycle()
        old = nba.read_text(self.core.local_path())
        self.us = '<html>chyba</html>'
        state = self.core.cycle(force=True)
        self.assertEqual(nba.read_text(self.core.local_path()), old)
        self.assertIn('last_fail', state)
        self.assertEqual(self.core.is_due(state), False)               # pauza po zlyhaní

    def test_check_disabled_keeps_everything(self):
        KODI.settings['check_streams'] = 'false'
        self.down = ('espn.', 'e2')
        state = self.core.cycle()
        self.assertEqual(state['channels'], state['candidates'])

    def test_epg_limit_page_does_not_break_cycle(self):
        self.epg_data = b'<span>You reached the download limit</span>'
        state = self.core.cycle()
        self.assertIn('epg_fail', state)
        self.assertTrue(os.path.isfile(self.core.local_path()))
        self.assertEqual(self.instances(), [1])
        self.assertEqual(nba.read_setting(
            nba.list_instances(self.core.iptv_dir())[0][1], 'epgPathType'), '1')
        self.assertFalse(self.core.is_epg_due(state))                  # pauza 3 h

    def test_epg_arrives_later_migrates_instance(self):
        self.epg_net_fail = True
        self.core.cycle()
        self.epg_net_fail = False
        state = self.core.load_state()
        state['epg_try'] = 0
        self.core.save_state(state)
        self.core.cycle()
        self.assertEqual(nba.read_setting(
            nba.list_instances(self.core.iptv_dir())[0][1], 'epgPathType'), '0')

    def test_renumber_without_download(self):
        self.core.cycle()
        KODI.settings['chno_start'] = '700'
        self.us = '<html>nesmie sa stiahnut</html>'
        self.core.cycle()
        text = nba.read_text(self.core.local_path())
        self.assertIn('tvg-chno="700"', text)
        self.assertNotIn('tvg-chno="201"', text)

    def test_no_iptv_no_instance(self):
        KODI.iptv_enabled = False
        self.core.cycle()
        self.assertEqual(self.instances(), [])

    def test_status_lines(self):
        self.core.cycle()
        text = '\n'.join(self.core.status_lines())
        self.assertIn('skupine NBA', text)
        self.assertIn('program má', text)
        self.assertIn('nastavená', text)


if __name__ == '__main__':
    unittest.main()
