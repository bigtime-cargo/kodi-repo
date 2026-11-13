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


# --- skupiny, číslovanie, skrytie bez EPG -----------------------------------------

GROUPS_SAMPLE = (
    '#EXTM3U\n'
    '#EXTINF:-1 tvg-id="Sport1.cz",Sport1\nhttp://a/s1.m3u8\n'
    '#EXTINF:-1 tvg-logo="http://x/l,b.png" tvg-id="HBO.cz",HBO\n'
    '#EXTVLCOPT:http-user-agent=Mozilla/5.0 (X11, Linux x86_64) AppleWebKit, Gecko\n'
    'http://a/hbo.m3u8\n'
    '#EXTINF:-1 tvg-id="Markiza.cz",Markíza\nhttp://a/m.m3u8\n'
    '#EXTINF:-1 tvg-id="Jednotka.cz",Jednotka\nhttp://a/j.m3u8\n'
    '#EXTINF:-1 tvg-id="CT24.cz",ČT24\nhttp://a/ct24.m3u8\n'
)


def extinf_rows(text):
    return [l for l in text.split('\n') if l.startswith('#EXTINF')]


def attr(line, name):
    import re
    return re.findall(r'(?:^|\s)%s="([^"]*)"' % re.escape(name), line)


class Groups(unittest.TestCase):
    def test_split_extinf_name_is_after_comma_outside_quotes(self):
        self.assertEqual(streamlet.split_extinf('#EXTINF:-1 tvg-id="A",Meno'),
                         ('#EXTINF:-1 tvg-id="A"', 'Meno'))
        self.assertEqual(streamlet.split_extinf('#EXTINF:-1 tvg-name="A, B" tvg-id="A",Meno, dalsie'),
                         ('#EXTINF:-1 tvg-name="A, B" tvg-id="A"', 'Meno, dalsie'))
        self.assertEqual(streamlet.split_extinf('#EXTINF:-1,Len meno'), ('#EXTINF:-1', 'Len meno'))
        self.assertEqual(streamlet.split_extinf('#EXTINF:-1'), ('#EXTINF:-1', ''))
        # nevyvážené úvodzovky: záloha = posledná čiarka
        self.assertEqual(streamlet.split_extinf('#EXTINF:-1 tvg-id="A,Meno'), ('#EXTINF:-1 tvg-id="A', 'Meno'))

    def test_with_attrs_adds_both(self):
        out = streamlet.with_attrs('#EXTINF:-1 tvg-id="A.cz",Meno', 'Šport', 105)
        self.assertEqual(out, '#EXTINF:-1 tvg-id="A.cz" group-title="Šport" tvg-chno="105",Meno')

    def test_with_attrs_replaces_existing_never_duplicates(self):
        line = ('#EXTINF:-1 group-title="Stara" tvg-id="A.cz" tvg-chno="7" tvg-chno="8" '
                'group-title="Iná",Meno')
        out = streamlet.with_attrs(line, 'Deti', 120)
        self.assertEqual(attr(out, 'group-title'), ['Deti'])
        self.assertEqual(attr(out, 'tvg-chno'), ['120'])
        self.assertEqual(attr(out, 'tvg-id'), ['A.cz'])
        self.assertTrue(out.endswith(',Meno'))

    def test_with_attrs_keeps_crlf_and_comma_in_name(self):
        out = streamlet.with_attrs('#EXTINF:-1 tvg-id="A",Meno, dalsie\r', 'Hudba', 1)
        self.assertTrue(out.endswith(',Meno, dalsie\r'))
        self.assertEqual(attr(out, 'group-title'), ['Hudba'])

    def test_add_groups_only_touches_extinf_lines(self):
        out, n = streamlet.add_groups(GROUPS_SAMPLE, 101)
        self.assertEqual(n, 5)
        before, after = GROUPS_SAMPLE.split('\n'), out.split('\n')
        self.assertEqual(len(before), len(after))
        for b, a in zip(before, after):
            if not b.startswith('#EXTINF'):
                self.assertEqual(a, b)

    def test_extvlcopt_with_commas_is_not_a_name(self):
        out, _ = streamlet.add_groups(GROUPS_SAMPLE, 101)
        self.assertIn('#EXTVLCOPT:http-user-agent=Mozilla/5.0 (X11, Linux x86_64) AppleWebKit, Gecko', out)
        hbo = [l for l in extinf_rows(out) if l.endswith(',HBO')][0]
        self.assertEqual(attr(hbo, 'group-title'), ['Filmy a seriály'])
        self.assertIn('tvg-logo="http://x/l,b.png"', hbo)

    def test_numbers_follow_theme_then_name(self):
        out, _ = streamlet.add_groups(GROUPS_SAMPLE, 101)
        rows = {l.rsplit(',', 1)[1]: (attr(l, 'tvg-chno')[0], attr(l, 'group-title')[0])
                for l in extinf_rows(out)}
        self.assertEqual(rows['Jednotka'], ('101', 'Všeobecné'))
        self.assertEqual(rows['Markíza'], ('102', 'Všeobecné'))
        self.assertEqual(rows['ČT24'], ('103', 'Správy'))
        self.assertEqual(rows['HBO'], ('104', 'Filmy a seriály'))
        self.assertEqual(rows['Sport1'], ('105', 'Šport'))
        # poradie riadkov v súbore ostalo
        self.assertEqual([l.rsplit(',', 1)[1] for l in extinf_rows(out)],
                         ['Sport1', 'HBO', 'Markíza', 'Jednotka', 'ČT24'])

    def test_custom_start_and_stable_ties(self):
        text = '#EXTM3U\n' + ''.join('#EXTINF:-1 tvg-id="X%d",Rovnaký\nhttp://a/%d\n' % (i, i)
                                      for i in range(3))
        out, _ = streamlet.add_groups(text, 5000)
        self.assertEqual([attr(l, 'tvg-chno')[0] for l in extinf_rows(out)], ['5000', '5001', '5002'])
        self.assertEqual([attr(l, 'tvg-id')[0] for l in extinf_rows(out)], ['X0', 'X1', 'X2'])

    def test_idempotent(self):
        once, _ = streamlet.add_groups(GROUPS_SAMPLE, 101)
        twice, _ = streamlet.add_groups(once, 101)
        self.assertEqual(once, twice)

    def test_no_channels(self):
        self.assertEqual(streamlet.add_groups('#EXTM3U\n', 101), ('#EXTM3U\n', 0))


def make_epg_ids(ids, filler=3000):
    ids = list(ids)
    rows = ['<?xml version="1.0" encoding="UTF-8"?>', '<tv generator-info-name="test">']
    for i in ids:
        rows.append('<channel id="%s"><display-name lang="cs">%s</display-name></channel>' % (i, i))
    for n in range(filler):
        rows.append('<programme start="2026111200%04d +0100" stop="2026111201%04d +0100" channel="%s">'
                    '<title>Porad %d s dlhším popisom, aby súbor mal rozumnú veľkosť</title></programme>'
                    % (n % 6000, n % 6000, ids[n % len(ids)], n))
    rows.append('</tv>')
    return '\n'.join(rows).encode('utf-8')


class EpgIds(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, 'epg.xml')

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def write(self, data):
        with open(self.path, 'wb') as f:
            f.write(data)

    def test_reads_ids_case_insensitive_unescaped(self):
        self.write(make_epg_ids(['HBO.cz', 'A&amp;B.cz', 'ČT1.cz']))
        self.assertEqual(streamlet.epg_channel_ids(self.path), {'hbo.cz', 'a&b.cz', 'čt1.cz'})

    def test_programme_channel_attribute_is_not_an_id(self):
        self.write(b'<tv><programme channel="Len.cz" start="1"></programme><channel id="Ano.cz"></channel></tv>')
        self.assertEqual(streamlet.epg_channel_ids(self.path), {'ano.cz'})

    def test_single_quotes_and_other_attributes_first(self):
        self.write(b"<tv><channel lang='cs' id='Jedna.cz'></channel><channel\n id = \"Dve.cz\"></channel></tv>")
        self.assertEqual(streamlet.epg_channel_ids(self.path), {'jedna.cz', 'dve.cz'})

    def test_tag_split_on_chunk_boundary(self):
        ids = ['Kanal%03d.cz' % i for i in range(60)]
        self.write(make_epg_ids(ids, filler=40))
        for chunk in (1, 2, 3, 7, 8, 9, 50, 64, 100, 1000):
            with self.subTest(chunk=chunk):
                self.assertEqual(streamlet.epg_channel_ids(self.path, chunk),
                                 {i.casefold() for i in ids})

    def test_missing_file_is_none(self):
        self.assertIsNone(streamlet.epg_channel_ids(os.path.join(self.tmp, 'nie.xml')))

    def test_garbage_gives_empty_set(self):
        self.write(b'\x00\xff<channel id=\xff\xfe' * 100)
        self.assertEqual(streamlet.epg_channel_ids(self.path), set())

    def test_signature_changes_with_file(self):
        self.assertEqual(streamlet.epg_signature(self.path), 'none')
        self.write(b'x' * 10)
        a = streamlet.epg_signature(self.path)
        self.write(b'x' * 11)
        self.assertNotEqual(a, streamlet.epg_signature(self.path))


def playlist_with_ids(ids, names=None):
    rows = ['#EXTM3U']
    for n, i in enumerate(ids):
        name = names[n] if names else 'Kanál %d' % n
        tvg = ' tvg-id="%s"' % i if i is not None else ''
        rows.append('#EXTINF:-1%s,%s' % (tvg, name))
        rows.append('#EXTVLCOPT:http-user-agent=A, B')
        rows.append('http://example.invalid/%d.m3u8' % n)
    return '\n'.join(rows) + '\n'


EPG_IDS = {('k%d.cz' % i) for i in range(30)}


class HideNoEpg(unittest.TestCase):
    def ids(self, n):
        return ['K%d.cz' % i for i in range(n)]

    def test_removes_channels_without_epg(self):
        text = playlist_with_ids(self.ids(30) + ['Nema.cz', 'Ani.cz'])
        out, removed, note = streamlet.hide_no_epg(text, EPG_IDS)
        self.assertIsNone(note)
        self.assertEqual(removed, ['Kanál 30', 'Kanál 31'])
        self.assertEqual(streamlet.count_channels(out), 30)
        self.assertNotIn('Nema.cz', out)
        self.assertNotIn('/30.m3u8', out)       # adresa a #EXTVLCOPT odišli so záznamom

    def test_suffix_and_case_ignored(self):
        text = playlist_with_ids(['K1.cz@SD', 'k2.CZ'] + self.ids(30)[3:])
        out, removed, _ = streamlet.hide_no_epg(text, EPG_IDS)
        self.assertEqual(removed, [])
        self.assertEqual(out, text)

    def test_channel_without_tvg_id_is_removed(self):
        text = playlist_with_ids(self.ids(30) + [None, ''])
        out, removed, note = streamlet.hide_no_epg(text, EPG_IDS)
        self.assertIsNone(note)
        self.assertEqual(removed, ['Kanál 30', 'Kanál 31'])

    def test_missing_unreadable_or_small_epg_removes_nothing(self):
        text = playlist_with_ids(self.ids(30) + ['Nema.cz'])
        for ids in (None, set(), {'k1.cz'}, {('k%d.cz' % i) for i in range(9)}):
            with self.subTest(ids=ids):
                out, removed, note = streamlet.hide_no_epg(text, ids)
                self.assertEqual((out, removed), (text, []))
                self.assertTrue(note)

    def test_exactly_min_epg_ids_is_enough(self):
        text = playlist_with_ids(self.ids(10) + ['Nema.cz'] * 20)
        # 10 ID stačí, ale vyradilo by to 20 z 30 (67 %) a ostalo by menej než MIN_CHANNELS
        out, removed, note = streamlet.hide_no_epg(text, {('k%d.cz' % i) for i in range(10)})
        self.assertEqual(removed, [])
        self.assertNotIn('ID kanálov', note)

    def test_more_than_70_percent_removes_nothing(self):
        text = playlist_with_ids(self.ids(60))
        out, removed, note = streamlet.hide_no_epg(text, {('k%d.cz' % i) for i in range(10)})
        self.assertEqual((out, removed), (text, []))
        self.assertIn('50 z 60', note)

    def test_exactly_70_percent_is_still_allowed(self):
        text = playlist_with_ids(self.ids(100))
        ids = {('k%d.cz' % i) for i in range(30)}
        out, removed, note = streamlet.hide_no_epg(text, ids)
        self.assertIsNone(note)
        self.assertEqual(len(removed), 70)
        self.assertEqual(streamlet.count_channels(out), 30)

    def test_result_below_min_channels_removes_nothing(self):
        text = playlist_with_ids(self.ids(25) + ['Nema.cz'] * 5)
        ids = {('k%d.cz' % i) for i in range(19)}               # ostalo by 19 < MIN_CHANNELS
        out, removed, note = streamlet.hide_no_epg(text, ids)
        self.assertEqual((out, removed), (text, []))
        self.assertIn('19', note)

    def test_empty_playlist(self):
        out, removed, note = streamlet.hide_no_epg('#EXTM3U\n', EPG_IDS)
        self.assertEqual((out, removed, note), ('#EXTM3U\n', [], None))


class RenderAndSignature(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.epg = os.path.join(self.tmp, 'epg.xml')

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def put_epg(self, ids):
        with open(self.epg, 'wb') as f:
            f.write(make_epg_ids(ids))

    def test_without_options_same_as_before(self):
        text = make_playlist(25)
        self.assertEqual(streamlet.render(text, None, None), streamlet.filter_duplicates(text, None))
        out, _ = streamlet.render(text, None, streamlet.Options())
        self.assertEqual(out, text)

    def test_groups_then_hide_numbers_only_remaining_channels(self):
        ids = ['K%d.cz' % i for i in range(30)]
        self.put_epg(ids[:25])
        text = playlist_with_ids(ids, names=['Film %d' % i for i in range(30)])
        opts = streamlet.Options(groups=True, chno_start=200, hide_no_epg=True, epg_path=self.epg)
        out, removed = streamlet.render(text, None, opts)
        self.assertEqual(removed, [])
        self.assertEqual(len(opts.report['no_epg']), 5)
        self.assertEqual(opts.report['note'], '')
        self.assertEqual(streamlet.count_channels(out), 25)
        self.assertEqual(sorted(int(attr(l, 'tvg-chno')[0]) for l in extinf_rows(out)),
                         list(range(200, 225)))

    def test_hide_skipped_when_epg_missing_but_groups_applied(self):
        text = playlist_with_ids(['K%d.cz' % i for i in range(30)])
        opts = streamlet.Options(groups=True, hide_no_epg=True, epg_path=os.path.join(self.tmp, 'nie.xml'))
        out, _ = streamlet.render(text, None, opts)
        self.assertEqual(streamlet.count_channels(out), 30)
        self.assertEqual(opts.report['no_epg'], [])
        self.assertIn('nedá prečítať', opts.report['note'])
        self.assertTrue(all(attr(l, 'group-title') for l in extinf_rows(out)))

    def test_signature_backward_compatible_when_everything_off(self):
        keys = {'a', 'b'}
        self.assertEqual(streamlet.filter_signature(keys), streamlet.keys_signature(keys))
        self.assertEqual(streamlet.filter_signature(keys, streamlet.Options()), streamlet.keys_signature(keys))
        self.assertEqual(streamlet.filter_signature(None, streamlet.Options()), '')

    def test_signature_reflects_settings(self):
        keys = {'a'}
        base = streamlet.filter_signature(keys, streamlet.Options())
        g101 = streamlet.filter_signature(keys, streamlet.Options(groups=True, chno_start=101))
        g200 = streamlet.filter_signature(keys, streamlet.Options(groups=True, chno_start=200))
        self.assertEqual(len({base, g101, g200}), 3)
        hide_off = streamlet.filter_signature(keys, streamlet.Options(groups=True, hide_no_epg=False,
                                                                      epg_path=self.epg))
        self.assertEqual(hide_off, g101)        # EPG sa do podpisu nedostane, keď je skrytie vypnuté
        hide_on = streamlet.filter_signature(keys, streamlet.Options(groups=True, hide_no_epg=True,
                                                                     epg_path=self.epg))
        self.assertNotEqual(hide_on, g101)

    def test_signature_follows_epg_file_only_when_hiding(self):
        keys = {'a'}

        def sig():
            return streamlet.filter_signature(keys, streamlet.Options(hide_no_epg=True, epg_path=self.epg))
        none_sig = sig()
        self.put_epg(['A.cz'] * 3)
        with_epg = sig()
        self.assertNotEqual(none_sig, with_epg)
        self.assertEqual(with_epg, sig())                       # stabilný
        os.utime(self.epg, (1, 1))
        self.assertNotEqual(with_epg, sig())

    def test_refresh_and_refilter_use_options(self):
        local, raw = os.path.join(self.tmp, 'l.m3u'), os.path.join(self.tmp, 'raw.m3u')
        opts = streamlet.Options(groups=True)
        n, _c, written, _removed = streamlet.refresh(
            'https://x/p.m3u', local, raw, None, opener=opener_for(make_playlist(25)), opts=opts)
        self.assertTrue(written)
        self.assertEqual(n, 25)
        self.assertNotIn('group-title', read_file(raw))      # raw ostáva bez zmien
        self.assertEqual(read_file(local).count('group-title="'), 25)
        self.assertEqual(read_file(local).count('tvg-chno="'), 25)
        # vypnutie skupín vráti pôvodný playlist
        streamlet.refilter(raw, local, None, streamlet.Options())
        self.assertEqual(read_file(local), read_file(raw))


def read_file(path):
    with open(path, encoding='utf-8') as f:
        return f.read()



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

        def fake_refresh(url, path, raw_path=None, o2=None, timeout=30, opener=None, opts=None):
            self.calls += 1
            if self.fail:
                raise OSError('bez siete')
            return self.real_refresh(url, path, raw_path, o2, opener=opener_for(self.playlist),
                                     opts=opts)
        self.playlist = make_playlist()
        streamlet.refresh = fake_refresh

        self.real_download_epg = streamlet.download_epg
        self.epg_calls = 0
        self.epg_net_fail = False
        self.epg_data = make_epg()

        def fake_download_epg(url, dest, timeout=90, opener=None):
            self.epg_calls += 1
            if self.epg_net_fail:
                raise OSError('bez siete')
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

    def test_epg_network_error_retries_after_ten_minutes(self):
        self.epg_net_fail = True
        self.core.cycle()
        self.assertEqual(self.epg_calls, 1)
        self.core.cycle()                       # hneď znova nie
        self.assertEqual(self.epg_calls, 1)
        state = self.core.load_state()
        self.assertEqual(state['epg_wait'], self.core.EPG_NET_RETRY_AFTER)
        state['epg_try'] -= self.core.EPG_NET_RETRY_AFTER + 1
        self.core.save_state(state)
        self.epg_net_fail = False
        self.core.cycle()
        self.assertEqual(self.epg_calls, 2)
        self.assertTrue(streamlet.epg_valid(self.core.epg_path()))
        self.assertNotIn('epg_wait', self.core.load_state())

    def test_epg_server_refusal_waits_three_hours(self):
        self.epg_data = LIMIT_PAGE
        self.core.cycle()
        state = self.core.load_state()
        self.assertEqual(state['epg_wait'], self.core.EPG_RETRY_AFTER)
        state['epg_try'] -= self.core.EPG_NET_RETRY_AFTER + 1     # 10 min nestačí
        self.core.save_state(state)
        self.core.cycle()
        self.assertEqual(self.epg_calls, 1)

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

    # --- skupiny, číslovanie, skrytie bez EPG v službe ---

    def local_text(self):
        return read_file(self.core.local_path())

    def test_groups_on_by_default_and_numbered_from_101(self):
        self.core.cycle()
        rows = extinf_rows(self.local_text())
        self.assertEqual(len(rows), 30)
        self.assertEqual(sorted(int(attr(l, 'tvg-chno')[0]) for l in rows), list(range(101, 131)))
        self.assertNotIn('group-title', read_file(self.core.raw_path()))

    def test_groups_off_gives_plain_playlist(self):
        KODI.settings['groups'] = 'false'
        self.core.cycle()
        self.assertEqual(self.local_text(), read_file(self.core.raw_path()))
        self.assertNotIn('tvg-chno', self.local_text())

    def test_changing_groups_settings_rewrites_without_download(self):
        self.core.cycle()
        self.assertEqual(self.calls, 1)
        KODI.settings['chno_start'] = '300'
        self.core.cycle()
        self.assertEqual(self.calls, 1)
        self.assertEqual(sorted(int(attr(l, 'tvg-chno')[0]) for l in extinf_rows(self.local_text())),
                         list(range(300, 330)))
        KODI.settings['groups'] = 'false'
        self.core.cycle()
        self.assertNotIn('group-title', self.local_text())
        KODI.settings['groups'] = 'true'
        self.core.cycle()
        self.assertIn('group-title', self.local_text())
        self.assertEqual(self.calls, 1)

    def test_chno_start_is_clamped_and_garbage_falls_back(self):
        for raw, expected in (('0', 1), ('-5', 1), ('99999', 9000), ('abc', 101), ('', 101), ('250', 250)):
            KODI.settings['chno_start'] = raw
            self.assertEqual(self.core.chno_start(), expected, raw)

    def test_signature_stored_and_stable(self):
        KODI.settings['groups'] = 'false'
        self.core.cycle()
        self.assertEqual(self.core.load_state()['filter_sig'], streamlet.keys_signature(None))
        KODI.settings['groups'] = 'true'
        self.core.cycle()
        sig = self.core.load_state()['filter_sig']
        before = os.stat(self.core.local_path()).st_mtime_ns
        self.core.cycle()
        self.assertEqual(self.core.load_state()['filter_sig'], sig)
        self.assertEqual(os.stat(self.core.local_path()).st_mtime_ns, before)

    def test_hide_no_epg_waits_for_epg_then_hides(self):
        KODI.settings['hide_no_epg'] = 'true'
        self.epg_data = make_epg_ids(['Kanal%d.cz' % i for i in range(20)])
        self.core.cycle()               # playlist príde skôr než EPG
        self.core.cycle()               # EPG je na disku: lokálny playlist sa prepočíta bez sťahovania
        self.assertEqual(self.calls, 1)
        names = self.local_names()
        self.assertEqual(len(names), 20)
        self.assertNotIn('Kanál 25', names)
        state = self.core.load_state()
        self.assertEqual(len(state['no_epg']), 10)
        self.assertIn('Skryté (bez EPG)', '\n'.join(self.core.status_lines()))
        self.assertEqual(sorted(int(attr(l, 'tvg-chno')[0]) for l in extinf_rows(self.local_text())),
                         list(range(101, 121)))

    def test_hide_no_epg_refreshes_after_epg_update(self):
        KODI.settings['hide_no_epg'] = 'true'
        self.epg_data = make_epg_ids(['Kanal%d.cz' % i for i in range(20)])
        self.core.cycle()
        self.core.cycle()
        self.assertEqual(len(self.local_names()), 20)
        # nové EPG má aj ďalšie kanály
        self.epg_data = make_epg_ids(['Kanal%d.cz' % i for i in range(25)])
        self.core.cycle(force=True)
        self.core.cycle()
        self.assertEqual(len(self.local_names()), 25)

    def test_hide_no_epg_off_by_default(self):
        self.epg_data = make_epg_ids(['Kanal%d.cz' % i for i in range(20)])
        self.core.cycle()
        self.core.cycle()
        self.assertEqual(len(self.local_names()), 30)
        self.assertNotIn('Skryté', '\n'.join(self.core.status_lines()))

    def test_hide_no_epg_unusable_epg_keeps_everything_and_reports(self):
        KODI.settings['hide_no_epg'] = 'true'
        self.core.cycle()               # make_epg() má jediné ID
        self.core.cycle()
        self.assertEqual(len(self.local_names()), 30)
        self.assertIn('iba 1 ID', self.core.load_state()['playlist_note'])
        self.assertTrue(any('iba 1 ID' in m for m in KODI.logs))
        self.assertIn('Upozornenie', '\n'.join(self.core.status_lines()))

    def test_hide_no_epg_needs_local_epg(self):
        KODI.settings['hide_no_epg'] = 'true'
        KODI.settings['epg_local'] = 'false'
        self.core.cycle()
        self.core.cycle()
        self.assertEqual(len(self.local_names()), 30)

    def test_hide_never_leaves_playlist_below_minimum(self):
        KODI.settings['hide_no_epg'] = 'true'
        self.epg_data = make_epg_ids(['Kanal%d.cz' % i for i in range(12)])      # 18 z 30 by vypadlo
        self.core.cycle()
        self.core.cycle()
        self.assertEqual(len(self.local_names()), 30)


if __name__ == '__main__':
    unittest.main()
