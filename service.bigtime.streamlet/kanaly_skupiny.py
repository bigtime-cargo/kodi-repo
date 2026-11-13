# Zaradenie TV kanála do témy podľa názvu. Bez importov Kodi (testovateľné).
#
# POZOR: rovnaký súbor je v doplnku plugin.video.o2tv (resources/lib/kanaly_skupiny.py).
# Pri zmene pravidiel zmeň oba (testy v oboch kontrolujú tie isté kanály).
import re
import unicodedata

# Poradie tém = poradie skupín aj číslovania kanálov (tvg-chno).
TEMY = [
    'Všeobecné',
    'Správy',
    'Filmy a seriály',
    'Dokumenty a životný štýl',
    'Šport',
    'Deti',
    'Hudba',
    'Regionálne',
    'Ostatné',
]
ARCHIV = 'O2 TV (archív)'      # skupina všetkých kanálov z O2 TV (majú archív)

_BRACKETS = re.compile(r'[\(\[][^\)\]]*[\)\]]')
_NONWORD = re.compile(r'[^0-9a-zЀ-ӿ]+')


def normalize(name):
    """Názov bez zátvoriek ((1080p), [Geo-blocked]), diakritiky a interpunkcie, malými písmenami."""
    s = _BRACKETS.sub(' ', name or '')
    s = unicodedata.normalize('NFKD', s.lower())
    # kombinačné znaky zahodiť (č → c); azbuka (й) sa tým nerozloží na inú písmenu
    s = ''.join(ch for ch in s if not unicodedata.combining(ch))
    return ' '.join(_NONWORD.sub(' ', s).split())


# Prvé zhodné pravidlo vyhráva (preto Správy pred Všeobecné, Deti pred Hudba atď.).
_RULES = [
    ('Správy', r'\bct ?24\b|\bcnn\b|\bta3\b|\b24\b|\bnews\b|\bdvtv\b|current time|'
               r'настоящее время|\bseznam\b|\bspravy\b|\bzpravy\b'),
    ('Deti', r'jojko|minimax|disney|\bnick\b|nickelodeon|nicktoons|nick jr|\bct ?d\b|'
             r'cartoon|boomerang|baby'),
    ('Hudba', r'ocko|music|muzika|slager|elektrika|retro ?music|\bhits?\b'),
    ('Šport', r'sport|golf|ballcasterz'),
    ('Regionálne', r'\bjc1\b|jihoceska|plzen|\bpolar\b|prerov|\bbrno\b|vychodoceska|\butv\b'),
    ('Dokumenty a životný štýl',
     r'animal|discovery|food network|hgtv|history|investigation|national geographic|nat geo|'
     r'spektrum|\btlc\b|travel|explore|viasat|prima zoom|paprika|doktor'),
    ('Filmy a seriály',
     r'\bhbo\b|hbo \d|cinemax|film|kino|warner|krimi|prima max|prima star|prima love|'
     r'markiza klasik|kviff'),
    ('Všeobecné',
     r'(jednotka|dvojka|ct ?[12]|markiza|tv joj|joj|dajto|doma|joj plus|tv2|1 1|'
     r'tv barrandov|barrandov tv|prima( cool| show)?( sk)?)'),
]
_COMPILED = [(tema, re.compile(rx)) for tema, rx in _RULES]
# Všeobecné sa musí zhodovať s celým názvom, inak by `joj` zobral aj „JOJ Krimi“.
_COMPILED[-1] = ('Všeobecné', re.compile(r'^' + _RULES[-1][1] + r'$'))


def tema(name):
    """Téma kanála podľa názvu (jedna z TEMY)."""
    n = normalize(name)
    for t, rx in _COMPILED:
        if rx.search(n):
            return t
    return 'Ostatné'


def group_title(name, archive=False):
    """Hodnota `group-title` pre M3U: téma, pre O2 TV navyše skupina s archívom.
    IPTV Simple berie viac skupín oddelených bodkočiarkou."""
    t = tema(name)
    return ';'.join([ARCHIV, t]) if archive else t


def order_key(name):
    """Kľúč poradia: najprv podľa témy, potom podľa názvu."""
    return (TEMY.index(tema(name)), normalize(name))
