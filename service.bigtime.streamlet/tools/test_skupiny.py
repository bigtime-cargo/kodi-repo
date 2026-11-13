# Offline testy zaradenia kanálov do tém: python3 -m unittest tools.test_skupiny -v
# POZOR: kanaly_skupiny.py má kópiu v plugin.video.o2tv; testy v oboch kontrolujú tie isté kanály.
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import kanaly_skupiny as ks  # noqa: E402

OCAKAVANE = {
    'Všeobecné': ['Jednotka', 'ČT1', 'Markíza', 'Prima Cool'],
    'Správy': ['ČT24', 'CNN Prima News', 'Televize Seznam', 'Current Time TV'],
    'Filmy a seriály': ['HBO', 'Prima Krimi', 'Warner TV', 'Film+'],
    'Dokumenty a životný štýl': ['Discovery Channel', 'TLC', 'Prima Zoom'],
    'Šport': ['ČT Sport', 'Sport1', 'Eurosport 4K Czechia'],
    'Deti': ['Disney Junior', 'Minimax', 'ČT:D', 'ČT:D/ČT art (1080p)'],
    'Hudba': ['Óčko Gold', 'Music Box Hits', 'Slager Muzika'],
    'Regionálne': ['JČ1', 'Plzeň TV', 'TV Brno 1'],
    'Ostatné': ['TV Noe', 'Up Network', 'Mňau TV'],
}


class Tema(unittest.TestCase):
    def test_ocakavane_temy(self):
        for tema, nazvy in OCAKAVANE.items():
            for nazov in nazvy:
                with self.subTest(nazov=nazov):
                    self.assertEqual(ks.tema(nazov), tema)

    @unittest.expectedFailure
    def test_samostatny_ct_art_je_v_detoch(self):
        # Pravidlá nemajú vzor pre samotné „ČT art“ (v praxi je kanál „ČT:D/ČT art“, ten
        # sedí cez „ČT:D“). Keď sa pravidlo doplní (v oboch kópiách), odstráň tento dekorátor.
        self.assertEqual(ks.tema('ČT art'), 'Deti')

    def test_vsetky_temy_su_pokryte(self):
        self.assertEqual(set(OCAKAVANE), set(ks.TEMY))

    def test_zatvorky_a_diakritika_nevadia(self):
        self.assertEqual(ks.tema('ČT24 (1080p)'), 'Správy')
        self.assertEqual(ks.tema('Jednotka [Geo-blocked]'), 'Všeobecné')
        self.assertEqual(ks.tema('MARKÍZA'), 'Všeobecné')

    def test_prazdny_nazov(self):
        self.assertEqual(ks.tema(''), 'Ostatné')
        self.assertEqual(ks.tema(None), 'Ostatné')


class GroupTitle(unittest.TestCase):
    def test_bez_archivu(self):
        self.assertEqual(ks.group_title('Jednotka'), 'Všeobecné')

    def test_s_archivom(self):
        self.assertEqual(ks.group_title('Jednotka', archive=True), 'O2 TV (archív);Všeobecné')
        self.assertEqual(ks.group_title('HBO', archive=True), ks.ARCHIV + ';Filmy a seriály')


class OrderKey(unittest.TestCase):
    def test_podla_temy_potom_nazvu(self):
        nazvy = ['Sport1', 'HBO', 'Jednotka', 'ČT24', 'Barrandov Krimi', 'Markíza']
        self.assertEqual(sorted(nazvy, key=ks.order_key),
                         ['Jednotka', 'Markíza', 'ČT24', 'Barrandov Krimi', 'HBO', 'Sport1'])

    def test_normalize(self):
        self.assertEqual(ks.normalize('  Óčko  Gold (HD) '), 'ocko gold')
        self.assertEqual(ks.normalize(None), '')


if __name__ == '__main__':
    unittest.main()
