# Offline testy zlučovača: python3 -m unittest tools.test_merge -v  (z adresára doplnku)
import os
import shutil
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import merge  # noqa: E402


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(data if isinstance(data, bytes) else data.encode('utf-8'))


def read(path):
    with open(path, 'rb') as f:
        return f.read()


O2 = '''#EXTM3U url-tvg="/x/epg.xml"
#EXTINF:-1 tvg-id="840007" tvg-name="Jednotka" group-title="O2 TV (archív);Všeobecné" tvg-chno="1" catchup="default" catchup-days="7" catchup-source="plugin://plugin.video.o2tv/?action=play&cid=840007&start_ts={utc}",Jednotka
plugin://plugin.video.o2tv/?action=play&cid=840007
#EXTINF:-1 tvg-id="840099" group-title="O2 TV (archív);Šport" tvg-chno="43",Šport O2
plugin://plugin.video.o2tv/?action=play&cid=840099
'''

MAGIO = '''#EXTM3U
#EXTINF:-1 tvg-id="4621" tvg-name="AMC HD" group-title="Magio GO (archív);Ostatné" tvg-chno="43" catchup="default" catchup-days="-1",AMC HD
plugin://plugin.video.magiogo/?action=play&cid=4621
#EXTINF:-1 tvg-id="4619" group-title="Magio GO (archív);Šport" tvg-chno="12",Šport, Magio
plugin://plugin.video.magiogo/?action=play&cid=4619
'''

STREAMLET = '''#EXTM3U
#EXTINF:-1 tvg-id="HBO.cz" group-title="Filmy a seriály" tvg-chno="101",HBO
#EXTVLCOPT:http-user-agent=Mozilla/5.0
http://x.invalid/hbo.m3u8
#EXTINF:-1 tvg-id="CTSport.cz" group-title="Šport" tvg-chno="102",ČT Sport
http://x.invalid/ct.m3u8
#EXTINF:-1 tvg-id="CTSport.cz" group-title="Šport" tvg-chno="103",ČT Sport 2
http://x.invalid/ct2.m3u8
#EXTINF:-1 tvg-id="840007" group-title="Všeobecné" tvg-chno="104",Jednotka kópia
http://x.invalid/j.m3u8
'''

O2_EPG = '''<?xml version="1.0" encoding="UTF-8"?>
<tv generator-info-name="o2">
<channel id="840007"><display-name>Jednotka</display-name></channel>
<programme start="20261113120000 +0000" stop="20261113130000 +0000" channel="840007"><title>A</title></programme>
</tv>
'''

MAGIO_EPG = '''<?xml version="1.0" encoding="UTF-8"?>
<tv generator-info-name="magio">
<channel id="4621"><display-name>AMC HD</display-name></channel>
<programme start="20261113120000 +0000" stop="20261113130000 +0000" channel="4621"><title>B</title></programme>
</tv>
'''

ST_EPG = '''<?xml version="1.0" encoding="UTF-8" ?>
<tv generator-info-name="open-epg">
<channel id="hbo.CZ"><display-name>HBO</display-name></channel>
<channel id="Ostatne.cz"><display-name>Cudzi</display-name></channel>
<channel id="Ina.cz"><display-name>ČT Sport 2</display-name></channel>
<programme start="20261113120000 +0000" stop="20261113130000 +0000" channel="hbo.CZ">
<title lang="cs">Film &amp; spol.</title>
<desc>viac riadkov</desc>
</programme>
<programme start="20261113120000 +0000" stop="20261113130000 +0000" channel="Ostatne.cz"><title>X</title></programme>
<programme start="20261113120000 +0000" stop="20261113130000 +0000" channel="Ina.cz"><title>Y</title></programme>
</tv>
'''


class Parsovanie(unittest.TestCase):
    def test_entry(self):
        e = merge.parse_playlist(O2)
        self.assertEqual(len(e), 2)
        self.assertEqual(e[0]['attrs']['tvg-id'], '840007')
        self.assertEqual(e[0]['name'], 'Jednotka')
        self.assertEqual(e[0]['url'], 'plugin://plugin.video.o2tv/?action=play&cid=840007')

    def test_nazov_s_ciarkou_a_extras(self):
        e = merge.parse_playlist(MAGIO + STREAMLET)
        self.assertEqual(e[1]['name'], 'Šport, Magio')
        hbo = [x for x in e if x['attrs']['tvg-id'] == 'HBO.cz'][0]
        self.assertEqual(hbo['extras'], ['#EXTVLCOPT:http-user-agent=Mozilla/5.0'])

    def test_riadok_bez_adresy_sa_zahodi(self):
        e = merge.parse_playlist('#EXTM3U\n#EXTINF:-1 tvg-id="a",A\n#EXTINF:-1 tvg-id="b",B\nhttp://b\n')
        self.assertEqual([x['attrs']['tvg-id'] for x in e], ['b'])

    def test_chno(self):
        self.assertEqual(merge.set_chno('#EXTINF:-1 tvg-id="a" tvg-chno="5",A", B', 9),
                         '#EXTINF:-1 tvg-id="a" tvg-chno="9",A", B')
        self.assertEqual(merge.set_chno('#EXTINF:-1 tvg-id="a",A, B', 9),
                         '#EXTINF:-1 tvg-id="a" tvg-chno="9",A, B')
        self.assertEqual(merge.set_chno('#EXTINF:-1 tvg-name="a,b",A', 9),
                         '#EXTINF:-1 tvg-name="a,b" tvg-chno="9",A')


class Spajanie(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.out = os.path.join(self.tmp, 'out')
        self.m3u = os.path.join(self.out, 'bigtime.m3u')
        self.epg = os.path.join(self.out, 'bigtime-epg.xml')
        self.state = os.path.join(self.out, 'merge-state.json')
        self.logs = []
        p = lambda *a: os.path.join(self.tmp, *a)  # noqa: E731
        self.src = [
            merge.Source('o2tv', 'O2', p('o2.m3u'), p('o2.xml'), False),
            merge.Source('streamlet', 'S', p('st.m3u'), p('st.xml'), True),
            merge.Source('magiogo', 'M', p('mg.m3u'), p('mg.xml'), False),
        ]
        write(p('o2.m3u'), O2)
        write(p('o2.xml'), O2_EPG)
        write(p('st.m3u'), STREAMLET)
        write(p('st.xml'), ST_EPG)
        write(p('mg.m3u'), MAGIO)
        write(p('mg.xml'), MAGIO_EPG)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_merge(self):
        return merge.merge(self.src, self.m3u, self.epg, self.state, self.out, self.logs.append)

    def entries(self):
        return merge.parse_playlist(read(self.m3u).decode('utf-8'))

    def test_playlist_obsahuje_vsetko_a_znacku(self):
        self.assertTrue(self.run_merge())
        text = read(self.m3u).decode('utf-8')
        self.assertTrue(text.startswith('#EXTM3U\n#BIGTIME-MERGED o2tv=2 streamlet=3 magiogo=2'))
        names = [e['name'] for e in self.entries()]
        self.assertEqual(names, ['Jednotka', 'Šport O2', 'HBO', 'ČT Sport', 'ČT Sport 2',
                                 'AMC HD', 'Šport, Magio'])

    def test_riadky_a_skupiny_ostanu(self):
        self.run_merge()
        text = read(self.m3u).decode('utf-8')
        self.assertIn('group-title="O2 TV (archív);Šport"', text)
        self.assertIn('group-title="Magio GO (archív);Šport"', text)
        self.assertIn('catchup="default" catchup-days="7" catchup-source="plugin://plugin.video.o2tv/'
                      '?action=play&cid=840007&start_ts={utc}"', text)
        self.assertIn('#EXTVLCOPT:http-user-agent=Mozilla/5.0\nhttp://x.invalid/hbo.m3u8', text)

    def test_cisla_kanalov_su_jedinecne(self):
        self.run_merge()
        nums = [int(e['attrs']['tvg-chno']) for e in self.entries()]
        self.assertEqual(len(nums), len(set(nums)))
        by_name = {e['name']: int(e['attrs']['tvg-chno']) for e in self.entries()}
        self.assertEqual(by_name['Jednotka'], 1)
        self.assertEqual(by_name['Šport O2'], 43)           # O2 má prednosť
        self.assertEqual(by_name['HBO'], 101)
        self.assertEqual(by_name['Šport, Magio'], 12)       # voľné číslo ostane
        self.assertEqual(by_name['AMC HD'], merge.FIRST_FREE)   # 43 je obsadené

    def test_duplicitne_id_z_ineho_zdroja_sa_vynechaju_v_ramci_zdroja_nie(self):
        self.run_merge()
        names = [e['name'] for e in self.entries()]
        self.assertNotIn('Jednotka kópia', names)             # tvg-id 840007 už má O2 TV
        self.assertIn('ČT Sport 2', names)                    # rovnaké tvg-id v rámci Streamletu
        self.assertTrue(any('840007' in m for m in self.logs))

    def test_epg_je_platny_xml_a_obsahuje_zdroje(self):
        self.run_merge()
        root = ET.fromstring(read(self.epg))
        self.assertEqual(root.tag, 'tv')
        ids = sorted(c.get('id') for c in root.findall('channel'))
        self.assertEqual(ids, ['4621', '840007', 'Ina.cz', 'hbo.CZ'])
        titles = sorted(p.find('title').text for p in root.findall('programme'))
        self.assertEqual(titles, ['A', 'B', 'Film & spol.', 'Y'])

    def test_epg_streamletu_sa_orezava_podla_id_a_nazvu(self):
        self.run_merge()
        text = read(self.epg).decode('utf-8')
        self.assertIn('hbo.CZ', text)               # id bez ohľadu na veľkosť písmen
        self.assertIn('Ina.cz', text)               # zhoda len podľa display-name
        self.assertNotIn('Ostatne.cz', text)
        self.assertNotIn('<title>X</title>', text)

    def test_bez_zmeny_sa_nic_neprepisuje(self):
        self.assertTrue(self.run_merge())
        mt = os.stat(self.m3u).st_mtime_ns
        self.assertFalse(self.run_merge())
        self.assertEqual(os.stat(self.m3u).st_mtime_ns, mt)

    def test_zmena_zdroja_prepise_a_orezanie_sa_nezopakuje(self):
        self.run_merge()
        calls = []
        orig = merge.filter_epg
        merge.filter_epg = lambda *a, **k: calls.append(1) or orig(*a, **k)
        try:
            write(self.src[2].epg, MAGIO_EPG.replace('<title>B</title>', '<title>B2</title>'))
            self.assertTrue(self.run_merge())
            self.assertIn(b'B2', read(self.epg))
            self.assertEqual(calls, [])                       # Streamlet z cache
            write(self.src[1].epg, ST_EPG.replace('Film &amp; spol.', 'Film 2'))
            self.assertTrue(self.run_merge())
            self.assertEqual(calls, [1])
            self.assertIn(b'Film 2', read(self.epg))
        finally:
            merge.filter_epg = orig

    def test_chybajuci_zdroj_sa_preskoci(self):
        os.remove(self.src[1].playlist)
        os.remove(self.src[1].epg)
        self.run_merge()
        names = [e['name'] for e in self.entries()]
        self.assertEqual(names, ['Jednotka', 'Šport O2', 'AMC HD', 'Šport, Magio'])
        self.assertNotIn(b'hbo.CZ', read(self.epg))

    def test_bez_zdrojov_sa_nezapise_nic(self):
        for s in self.src:
            os.remove(s.playlist)
        self.assertFalse(self.run_merge())
        self.assertFalse(os.path.exists(self.out))

    def test_neuplne_epg_sa_preskoci_ale_playlist_ostane(self):
        write(self.src[2].epg, MAGIO_EPG[:-30])               # chýba </tv>
        self.run_merge()
        self.assertEqual(len(self.entries()), 7)
        self.assertNotIn(b'AMC HD', read(self.epg))
        ET.fromstring(read(self.epg))                         # výstup je napriek tomu platný XML

    def test_epg_v_inom_kodovani_sa_prekoduje(self):
        latin = '<?xml version="1.0" encoding="ISO-8859-2"?>\n<tv><channel id="4621"><display-name>Šport' \
                '</display-name></channel></tv>\n'
        write(self.src[2].epg, latin.encode('iso-8859-2'))
        self.run_merge()
        root = ET.fromstring(read(self.epg))
        self.assertIn('Šport', [d.text for d in root.iter('display-name')])

    def test_prazdny_zdroj_alebo_zly_playlist_nezhodi(self):
        write(self.src[0].playlist, '')
        write(self.src[2].playlist, 'neplatný obsah bez kanálov')
        self.assertTrue(self.run_merge())
        self.assertEqual([e['name'] for e in self.entries()], ['HBO', 'ČT Sport', 'ČT Sport 2', 'Jednotka kópia'])

    def test_zdroje_sa_nikdy_nemenia(self):
        before = {s.playlist: read(s.playlist) for s in self.src}
        before.update({s.epg: read(s.epg) for s in self.src})
        self.run_merge()
        self.assertEqual(before, {p: read(p) for p in before})

    def test_ziadny_dockovy_subor_po_zapise(self):
        self.run_merge()
        self.assertFalse([n for n in os.listdir(self.out) if n.startswith('.bigtime-')])


if __name__ == '__main__':
    unittest.main()
