# Playlist streamletu a inštancia IPTV Simple. Bez importov Kodi (testovateľné).
#
# Playlist má tvg-id="HBO.cz@SD" (alebo @Czechia, @HD, @UHD), EPG z open-epg.com
# má channel id="HBO.cz". IPTV Simple porovnáva ID presne, takže bez opravy sa
# nespáruje nič. Doplnok preto playlist stiahne, od '@' odstrihne príponu a uloží
# ho lokálne; inštancia IPTV Simple číta ten lokálny súbor.
#
# Pravidlo č. 1: cudziu ani ručne vytvorenú inštanciu nikdy nemeniť. Nová vzniká
# len keď žiadna nečíta lokálny súbor ani priamo URL streamletu.
import hashlib
import html
import os
import re
import tempfile
import unicodedata
import urllib.request
import zlib
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

import kanaly_skupiny

DEFAULT_PLAYLIST_URL = 'https://repo.streamlet.info/playlists/cz.m3u'
DEFAULT_EPG_URL = 'https://www.open-epg.com/files/czech.xml'
STREAMLET_HOST = 'repo.streamlet.info'
INSTANCE_NAME = 'Streamlet CZ (Bigtime)'
SETTINGS_DIR = 'special://userdata/addon_data/pvr.iptvsimple'
LOCAL_NAME = 'streamlet-cz.m3u'
EPG_NAME = 'streamlet-epg.xml'      # lokálna kópia EPG (open-epg.com má limit 20 stiahnutí denne)
RAW_NAME = 'streamlet-raw.m3u'      # opravené ID, ale bez filtra duplicít
O2_PLAYLIST = 'special://userdata/addon_data/plugin.video.o2tv/playlist.m3u'
MIN_O2_CHANNELS = 5          # kratší export O2 je podozrivý, filter sa nepoužije
MIN_KEY = 3                  # kratší kľúč názvu by párovať príliš veľa

MIN_CHANNELS = 20            # menej = stiahlo sa niečo iné než playlist
MAX_BYTES = 5 * 1024 * 1024
KEEP_RATIO = 0.5             # nový playlist s menej ako polovicou kanálov sa zahodí
MIN_EPG_BYTES = 200 * 1024   # menší súbor nie je celé EPG (napr. stránka s chybou)
MAX_EPG_BYTES = 300 * 1024 * 1024    # po rozbalení
EPG_CHUNK = 64 * 1024
DEFAULT_CHNO_START = 101     # číslovanie streamletu; O2 TV má 1-99
MIN_EPG_IDS = 10             # menej ID v lokálnom EPG = EPG je nepoužiteľné, nič sa neskrýva
MAX_HIDE_RATIO = 0.7         # filter, ktorý by vyradil viac kanálov, sa nepoužije
UA = 'Mozilla/5.0 (compatible; service.bigtime.streamlet)'

NASTAVENY = 'NASTAVENY'      # inštancia číta náš lokálny súbor
RUCNA = 'RUCNA'              # ručná inštancia číta URL streamletu priamo
TREBA = 'TREBA'              # inštancia chýba

_INSTANCE_FILE = re.compile(r'^instance-settings-(\d+)\.xml$')
_TVG_ID = re.compile(r'(tvg-id=")([^"]*)"')


class PlaylistError(Exception):
    """Stiahnutý obsah nie je použiteľný playlist; starý súbor ostáva."""


class EpgError(Exception):
    """Stiahnutý obsah nie je použiteľné EPG (XMLTV); starý súbor ostáva."""


# --- playlist ---------------------------------------------------------------

def count_channels(text):
    return text.count('#EXTINF')


def fix_playlist(text):
    """Vráti (text s opravenými tvg-id, počet zmenených ID)."""
    changed = 0

    def sub(m):
        nonlocal changed
        new = m.group(2).split('@', 1)[0]
        if new != m.group(2):
            changed += 1
        return m.group(1) + new + '"'

    return _TVG_ID.sub(sub, text), changed


def download(url, timeout=30, opener=urllib.request.urlopen):
    """Stiahne text; chybu siete nechá prejsť ako výnimku (volajúci ju zachytí)."""
    if not url.lower().startswith(('https://', 'http://')):
        raise PlaylistError('nepodporovaná URL')
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with opener(req, timeout=timeout) as r:
        data = r.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise PlaylistError('odpoveď je väčšia než %d bajtov' % MAX_BYTES)
    try:
        return data.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise PlaylistError('odpoveď nie je UTF-8')


def validate(text, old_count=0):
    """Vráti počet kanálov alebo vyhodí PlaylistError."""
    if not text.lstrip().startswith('#EXTM3U'):
        raise PlaylistError('chýba hlavička #EXTM3U')
    n = count_channels(text)
    if n < MIN_CHANNELS:
        raise PlaylistError('iba %d kanálov (minimum %d)' % (n, MIN_CHANNELS))
    if old_count and n < old_count * KEEP_RATIO:
        raise PlaylistError('kanálov %d, predtým %d - zahadzujem' % (n, old_count))
    return n


def read_text(path):
    try:
        with open(path, 'rb') as f:
            return f.read().decode('utf-8')
    except (OSError, UnicodeDecodeError):
        return None


def write_atomic(path, text):
    """Zapíše súbor atómovo. Vráti True, ak sa obsah zmenil."""
    if read_text(path) == text:
        return False
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.stream-', suffix='.tmp', dir=directory)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(text.encode('utf-8'))
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return True


# --- EPG (XMLTV) ------------------------------------------------------------
#
# open-epg.com dovolí stiahnuť každý súbor 20-krát za deň (na IP) a potom namiesto
# XML vráti stránku "You reached the download limit". IPTV Simple sťahuje EPG pri
# každom štarte Kodi, takže pri niekoľkých zariadeniach sa limit rýchlo minie a
# EPG zmizne. Doplnok preto EPG sťahuje sám (raz za pár hodín), overí ho a
# inštancia IPTV Simple číta lokálny súbor.

def _page_message(head):
    """Čitateľný začiatok odpovede (pre hlášku o chybe)."""
    text = re.sub(r'<[^>]+>', ' ', head.decode('utf-8', 'replace'))
    return ' '.join(text.split())[:120]


def download_epg(url, dest, timeout=90, opener=urllib.request.urlopen):
    """Stiahne XMLTV (aj gzip) po častiach do dočasného súboru, overí ho a atómovo
    ním nahradí `dest`. Vráti veľkosť v bajtoch. Pri chybe vyhodí výnimku a starý
    súbor ostane nedotknutý."""
    if not url.lower().startswith(('https://', 'http://')):
        raise EpgError('nepodporovaná URL')
    directory = os.path.dirname(dest)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.epg-', suffix='.tmp', dir=directory)
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept-Encoding': 'gzip'})
    try:
        size, head, tail = 0, b'', b''
        decoder = None
        first = True
        with os.fdopen(fd, 'wb') as out, opener(req, timeout=timeout) as r:
            while True:
                chunk = r.read(EPG_CHUNK)
                if not chunk:
                    break
                if first:
                    first = False
                    if chunk[:2] == b'\x1f\x8b':
                        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
                if decoder is not None:
                    try:
                        chunk = decoder.decompress(chunk)
                    except zlib.error as exc:
                        raise EpgError('chybný gzip: %s' % exc)
                if not chunk:
                    continue
                if len(head) < 4096:
                    head += chunk[:4096 - len(head)]
                tail = (tail + chunk)[-1024:]
                size += len(chunk)
                if size > MAX_EPG_BYTES:
                    raise EpgError('EPG je väčšie než %d MB' % (MAX_EPG_BYTES // 1048576))
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        if b'<tv' not in head:
            raise EpgError('nie je XMLTV: %s' % (_page_message(head) or 'prázdna odpoveď'))
        if b'</tv>' not in tail:
            raise EpgError('XMLTV je neúplné (chýba </tv>)')
        if size < MIN_EPG_BYTES:
            raise EpgError('EPG má iba %d bajtov' % size)
        os.chmod(tmp, 0o644)
        os.replace(tmp, dest)
        return size
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def epg_valid(path):
    """Je na disku použiteľná kópia EPG? (rýchla kontrola veľkosti a konca)"""
    try:
        size = os.path.getsize(path)
        if size < MIN_EPG_BYTES:
            return False
        with open(path, 'rb') as f:
            f.seek(max(0, size - 1024))
            return b'</tv>' in f.read()
    except OSError:
        return False


# --- filter duplicít s O2 TV --------------------------------------------------
#
# Kanál zo streamletu, ktorý už má O2 TV, sa z lokálneho playlistu vynechá: O2 TV
# má archív (catchup) a EPG zo svojho zdroja, kanál zo streamletu je len živý.
# Porovnávajú sa názvy bez diakritiky, veľkosti písmen, interpunkcie a značiek
# v zátvorkách ("(1080p)", "[Geo-blocked]"); poradie slov nevadí ("TV Barrandov" =
# "Barrandov TV"). "Prima Cool" (CZ) a "Prima COOL SK" sa nezhodujú, sú to dva kanály.

_TAG = re.compile(r'[\(\[][^)\]]*[\)\]]')


def _fold(text):
    text = unicodedata.normalize('NFKD', text)
    return ''.join(c for c in text if not unicodedata.combining(c)).lower()


def name_keys(name):
    """Množina kľúčov názvu kanála (prázdna, ak je názov príliš krátky)."""
    tokens = re.findall(r'[a-z0-9+]+', _fold(_TAG.sub(' ', html.unescape(name))))
    keys = {''.join(tokens), ''.join(sorted(tokens))}
    return {k for k in keys if len(k) >= MIN_KEY}


def _entry_name(extinf):
    tvg = re.search(r'tvg-name="([^"]*)"', extinf)
    if tvg and tvg.group(1).strip():
        return tvg.group(1)
    return extinf.rsplit(',', 1)[1].strip() if ',' in extinf else ''


def o2_keys(text):
    """Kľúče názvov kanálov z playlistu O2 TV; None, ak vyzerá nepoužiteľne."""
    if not text or '#EXTM3U' not in text:
        return None
    names = [_entry_name(m) for m in re.findall(r'#EXTINF:[^\n]*', text)]
    names = [n for n in names if n]
    if len(names) < MIN_O2_CHANNELS:
        return None
    keys = set()
    for n in names:
        keys |= name_keys(n)
    return keys


def keys_signature(keys):
    """Odtlačok množiny kľúčov (na zistenie, že sa O2 zoznam zmenil)."""
    if not keys:
        return ''
    return hashlib.sha1('\n'.join(sorted(keys)).encode('utf-8')).hexdigest()[:16]


def _drop_entries(text, drop):
    """Vráti (text bez záznamov, pre ktoré `drop(riadok #EXTINF)` platí, zoznam ich
    názvov). Záznam = riadok #EXTINF + riadky za ním až po adresu (vrátane)."""
    out, removed, entry = [], [], []
    for line in text.split('\n'):
        if not entry:
            if line.startswith('#EXTINF'):
                entry = [line]
            else:
                out.append(line)
            continue
        entry.append(line)
        if line.strip() and not line.startswith('#'):        # adresa ukončuje záznam
            if drop(entry[0]):
                removed.append(_entry_name(entry[0]))
            else:
                out.extend(entry)
            entry = []
    out.extend(entry)           # nedokončený záznam na konci sa nechá tak
    return '\n'.join(out), removed


def filter_duplicates(text, keys):
    """Vráti (playlist bez kanálov, ktoré už má O2 TV, zoznam vynechaných názvov)."""
    if not keys:
        return text, []
    return _drop_entries(text, lambda extinf: bool(name_keys(_entry_name(extinf)) & keys))


# --- skupiny a číslovanie kanálov -----------------------------------------------
#
# Každý #EXTINF dostane `group-title` (téma podľa názvu) a `tvg-chno` (číslo od
# `chno_start` podľa témy a názvu). Číslo držíme v atribúte, poradie riadkov v súbore
# ostáva. IPTV Simple berie číslo z `tvg-chno`, keď inštancia nemá zapnuté
# `numberByOrder` (predvolene vypnuté).

_ATTR_GROUP = re.compile(r'(?<=\s)group-title="[^"]*"')
_ATTR_CHNO = re.compile(r'(?<=\s)tvg-chno="[^"]*"')
_ATTR_TVG_ID = re.compile(r'(?:^|\s)tvg-id="([^"]*)"')


def split_extinf(line):
    """Rozdelí riadok #EXTINF na (hlavička s atribútmi, názov). Názov je text za
    prvou čiarkou mimo úvodzoviek (čiarka v hodnote atribútu ho nerozdelí); keď
    úvodzovky nesedia, za poslednou čiarkou. Bez čiarky je názov prázdny."""
    in_quote = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_quote = not in_quote
        elif ch == ',' and not in_quote:
            return line[:i], line[i + 1:]
    if ',' in line:
        head, name = line.rsplit(',', 1)
        return head, name
    return line, ''


def _put_attr(head, regex, name, value):
    """Nahradí atribút `name` v hlavičke (ostatné jeho výskyty zmaže), inak ho pridá."""
    attr = '%s="%s"' % (name, value.replace('"', "'"))
    parts = regex.split(head)
    if len(parts) == 1:
        return head.rstrip() + ' ' + attr
    return parts[0] + attr + ''.join(p for p in parts[1:])


def with_attrs(extinf, group, chno):
    """Riadok #EXTINF s `group-title` a `tvg-chno` (existujúce sa nahradia)."""
    cr = '\r' if extinf.endswith('\r') else ''
    line = extinf[:-1] if cr else extinf
    head, name = split_extinf(line)
    head = _put_attr(head, _ATTR_GROUP, 'group-title', group)
    head = _put_attr(head, _ATTR_CHNO, 'tvg-chno', str(chno))
    return head + (',' + name if ',' in line else '') + cr


def add_groups(text, chno_start=DEFAULT_CHNO_START):
    """Pridá ku každému #EXTINF `group-title` a `tvg-chno`. Čísla idú od `chno_start`
    podľa kanaly_skupiny.order_key (téma, názov; pri zhode pôvodné poradie). Vráti
    (text, počet kanálov). Riadky iné než #EXTINF sa nemenia."""
    lines = text.split('\n')
    idx = [i for i, l in enumerate(lines) if l.startswith('#EXTINF')]
    names = {i: split_extinf(lines[i].rstrip('\r'))[1].strip() for i in idx}
    ranked = sorted(idx, key=lambda i: (kanaly_skupiny.order_key(names[i]), i))
    for n, i in enumerate(ranked):
        lines[i] = with_attrs(lines[i], kanaly_skupiny.group_title(names[i]), chno_start + n)
    return '\n'.join(lines), len(idx)


# --- skrytie kanálov bez EPG ---------------------------------------------------

_EPG_CHANNEL_TAG = re.compile(rb'<channel\b[^>]*>')
_EPG_ID_ATTR = re.compile(rb'\sid\s*=\s*(?:"([^"]*)"|\'([^\']*)\')')


def epg_channel_ids(path, chunk=EPG_CHUNK):
    """Množina ID kanálov (`<channel id="...">`, bez ohľadu na veľkosť písmen) z lokálneho
    XMLTV. Súbor sa číta po častiach s prekryvom, nikdy celý. None, keď sa nedá prečítať."""
    ids = set()
    carry = b''
    try:
        with open(path, 'rb') as f:
            while True:
                data = f.read(chunk)
                if not data:
                    break
                buf = carry + data
                end = 0
                for m in _EPG_CHANNEL_TAG.finditer(buf):
                    end = m.end()
                    a = _EPG_ID_ATTR.search(m.group(0))
                    if a:
                        raw = a.group(1) if a.group(1) is not None else a.group(2)
                        ids.add(html.unescape(raw.decode('utf-8', 'replace')).strip().casefold())
                rest = buf[end:]
                i = rest.rfind(b'<channel')
                if i >= 0:
                    carry = rest[i:] if len(rest) - i <= 8192 else b''
                else:
                    carry = rest[-8:]       # možný začiatok `<channel` na hranici
    except OSError:
        return None
    return ids


def epg_signature(path):
    """Veľkosť a čas zmeny lokálneho EPG (na zistenie, že sa obnovilo)."""
    try:
        st = os.stat(path)
        return '%d-%d' % (st.st_size, int(st.st_mtime))
    except OSError:
        return 'none'


def _entry_tvg_id(extinf):
    m = _ATTR_TVG_ID.search(split_extinf(extinf.rstrip('\r'))[0])
    return m.group(1).split('@', 1)[0].strip().casefold() if m else ''


def hide_no_epg(text, ids):
    """Vynechá kanály, ktorých `tvg-id` nie je v `ids` (kanál bez tvg-id tiež).
    Vráti (text, názvy vynechaných, dôvod prečo sa nič nevynechalo alebo None).
    Poistky: bez použiteľného EPG (None / menej než MIN_EPG_IDS ID), pri vyradení
    viac než MAX_HIDE_RATIO kanálov alebo ak by ostalo menej než MIN_CHANNELS kanálov
    sa nevynechá nič."""
    if ids is None:
        return text, [], 'lokálne EPG sa nedá prečítať'
    if len(ids) < MIN_EPG_IDS:
        return text, [], 'v lokálnom EPG je iba %d ID kanálov' % len(ids)
    total = count_channels(text)
    if not total:
        return text, [], None
    out, removed = _drop_entries(text, lambda extinf: _entry_tvg_id(extinf) not in ids)
    if len(removed) > total * MAX_HIDE_RATIO:
        return text, [], 'EPG by vyradilo %d z %d kanálov' % (len(removed), total)
    if total - len(removed) < MIN_CHANNELS:
        return text, [], 'po vyradení by ostalo iba %d kanálov' % (total - len(removed))
    return out, removed, None


# --- výsledný lokálny playlist -------------------------------------------------

class Options:
    """Nastavenia výstupu (skupiny, číslovanie, skrytie bez EPG). `report` vyplní
    render(): `no_epg` (vynechané názvy) a `note` (dôvod, prečo sa niečo preskočilo)."""

    def __init__(self, groups=False, chno_start=DEFAULT_CHNO_START, hide_no_epg=False,
                 epg_path=None):
        self.groups = bool(groups)
        self.chno_start = int(chno_start)
        self.hide_no_epg = bool(hide_no_epg) and bool(epg_path)
        self.epg_path = epg_path
        self.report = {'no_epg': [], 'note': ''}

    def signature(self):
        """Odtlačok nastavení, ktoré menia výstup; prázdny, keď sú obe funkcie vypnuté."""
        parts = []
        if self.groups:
            parts.append('g%d' % self.chno_start)
        if self.hide_no_epg:
            parts.append('h' + epg_signature(self.epg_path))
        return ','.join(parts)


def filter_signature(keys, opts=None):
    """Odtlačok všetkého, čo mení lokálny playlist oproti uloženému (zoznam O2 TV,
    skupiny, číslovanie, skrytie bez EPG a stav EPG). Bez opts a pri vypnutých
    funkciách sa rovná keys_signature (staré state.json ostáva platné)."""
    sig = keys_signature(keys)
    extra = opts.signature() if opts else ''
    return sig + '|' + extra if extra else sig


def render(fixed, o2=None, opts=None):
    """Z playlistu s opravenými ID vyrobí lokálny: filter duplicít s O2 TV, potom
    voliteľne skrytie bez EPG a skupiny s číslovaním. Vráti (text, vynechané
    duplicity); podrobnosti dopĺňa do `opts.report`."""
    shown, removed = filter_duplicates(fixed, o2)
    if opts is None:
        return shown, removed
    opts.report = {'no_epg': [], 'note': ''}
    notes = []
    if opts.hide_no_epg:
        shown, no_epg, note = hide_no_epg(shown, epg_channel_ids(opts.epg_path))
        opts.report['no_epg'] = no_epg
        if note:
            notes.append('skrytie bez EPG preskočené: ' + note)
    if opts.groups:
        decorated, _n = add_groups(shown, opts.chno_start)
        if count_channels(decorated) == count_channels(shown):
            shown = decorated
        else:
            notes.append('skupiny preskočené: zmenil sa počet kanálov')
    opts.report['note'] = '; '.join(notes)
    return shown, removed


def refresh(url, local_path, raw_path=None, o2=None, timeout=30, opener=urllib.request.urlopen,
            opts=None):
    """Stiahne, opraví a zapíše playlist. Vráti (kanály, zmenených ID, zmenil sa
    súbor, názvy vynechaných duplicít). Pri akejkoľvek chybe vyhodí výnimku a starý
    súbor ostane nedotknutý. `raw_path` dostane playlist bez filtra (na lokálne
    prefiltrovanie, keď sa zmení zoznam O2 TV), `o2` sú kľúče z o2_keys(), `opts`
    je Options (skupiny, číslovanie, skrytie bez EPG)."""
    text = download(url, timeout, opener)
    old = read_text(raw_path or local_path)
    old_count = count_channels(old) if old else 0
    n = validate(text, old_count)
    fixed, changed = fix_playlist(text)
    if raw_path:
        write_atomic(raw_path, fixed)
    shown, removed = render(fixed, o2, opts)
    return n, changed, write_atomic(local_path, shown), removed


def refilter(raw_path, local_path, o2=None, opts=None):
    """Znova vyrobí lokálny playlist z uloženého (bez siete). Vráti (zmenil sa
    súbor, názvy vynechaných duplicít) alebo None, keď uložený playlist chýba."""
    raw = read_text(raw_path)
    if not raw:
        return None
    shown, removed = render(raw, o2, opts)
    return write_atomic(local_path, shown), removed


# --- inštancie IPTV Simple --------------------------------------------------

def _identity(path):
    return path


def list_instances(settings_dir):
    try:
        names = os.listdir(settings_dir)
    except OSError:
        return []
    found = []
    for n in names:
        m = _INSTANCE_FILE.match(n)
        if m:
            found.append((int(m.group(1)), os.path.join(settings_dir, n)))
    return sorted(found)


def read_setting(path, setting_id):
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return None
    for el in root.iter('setting'):
        if el.get('id') == setting_id:
            return (el.text or '').strip()
    return None


def _same_file(a, b):
    return os.path.normpath(a) == os.path.normpath(b)


def status(settings_dir, local_path, translate=_identity):
    """NASTAVENY: nejaká inštancia číta náš súbor. RUCNA: nejaká číta URL
    streamletu priamo (nemení sa, len sa ohlási). Inak TREBA."""
    manual = False
    for _num, path in list_instances(settings_dir):
        m3u = read_setting(path, 'm3uPath')
        if m3u:
            try:
                if _same_file(translate(m3u), local_path):
                    return NASTAVENY
            except Exception:
                pass
        url = read_setting(path, 'm3uUrl') or ''
        if STREAMLET_HOST in url:
            manual = True
    return RUCNA if manual else TREBA


def build_settings_xml(m3u_path, epg_url, name=INSTANCE_NAME, epg_path=None):
    """Minimálna inštancia; ostatné kľúče si Kodi doplní z defaultov.
    Playlist sa z lokálneho súboru znova načíta každú hodinu. S `epg_path` číta
    EPG z lokálneho súboru, inak z `epg_url`."""
    if epg_path:
        epg = (('epgPathType', '0'), ('epgPath', epg_path))
    else:
        epg = (('epgPathType', '1'), ('epgUrl', epg_url))
    values = (
        ('kodi_addon_instance_name', name),
        ('m3uPathType', '0'),
        ('m3uPath', m3u_path),
        ('m3uRefreshMode', '1'),
        ('m3uRefreshIntervalMins', '60'),
    ) + epg + (
        ('epgCache', 'true'),
        ('epgTSOverride', 'true'),
    )
    rows = ''.join('    <setting id="%s">%s</setting>\n' % (k, escape(v)) for k, v in values)
    return '<settings version="2">\n' + rows + '</settings>\n'


def free_number(settings_dir):
    used = {n for n, _p in list_instances(settings_dir)}
    n = 1
    while n in used:
        n += 1
    return n


def create_instance(settings_dir, m3u_path, epg_url, name=INSTANCE_NAME, epg_path=None):
    """Zapíše novú inštanciu pod prvým voľným číslom (atómovo). Vráti jej cestu."""
    os.makedirs(settings_dir, exist_ok=True)
    target = os.path.join(settings_dir, 'instance-settings-%d.xml' % free_number(settings_dir))
    data = build_settings_xml(m3u_path, epg_url, name, epg_path).encode('utf-8')
    fd, tmp = tempfile.mkstemp(prefix='.bigtime-', suffix='.tmp', dir=settings_dir)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        if os.path.lexists(target):
            raise FileExistsError(target)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


def _set(root, setting_id, value):
    for el in root.iter('setting'):
        if el.get('id') == setting_id:
            el.text = value
            el.attrib.pop('default', None)      # hodnota už nie je predvolená
            return
    el = ET.SubElement(root, 'setting', {'id': setting_id})
    el.text = value
    el.tail = '\n'


def migrate_epg(settings_dir, m3u_local, epg_url, epg_special, translate=_identity):
    """Naša inštancia (číta náš playlist), ktorá berie EPG priamo z `epg_url`, sa
    prepne na lokálny súbor `epg_special`. Inštancie, ktoré EPG berú odinakiaľ, sa
    nemenia. Vráti cestu zmenenej inštancie alebo None."""
    for _num, path in list_instances(settings_dir):
        m3u = read_setting(path, 'm3uPath')
        try:
            ours = bool(m3u) and _same_file(translate(m3u), m3u_local)
        except Exception:
            ours = False
        if not ours:
            continue
        kind = read_setting(path, 'epgPathType')
        if kind not in (None, '', '1') or read_setting(path, 'epgUrl') != epg_url:
            continue
        tree = ET.parse(path)
        root = tree.getroot()
        _set(root, 'epgPathType', '0')
        _set(root, 'epgPath', epg_special)
        directory = os.path.dirname(path)
        fd, tmp = tempfile.mkstemp(prefix='.bigtime-', suffix='.tmp', dir=directory)
        try:
            with os.fdopen(fd, 'wb') as f:
                tree.write(f, encoding='utf-8', xml_declaration=True)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o644)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return path
    return None
