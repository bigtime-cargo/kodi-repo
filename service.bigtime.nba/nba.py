# NBA a americký šport z playlistu streamletu pre IPTV Simple. Bez importov Kodi
# (testovateľné).
#
# Zdroj: https://repo.streamlet.info/playlists/us.m3u (~3000 kanálov, projekt
# iptv-org). Doplnok z neho vyberie športové kanály (NBA TV, ESPN, Fox Sports, NBC
# Sports, TNT...), každý overí (manifest + začiatok prvého segmentu), nefunkčné
# vynechá, zvyšok dá do skupiny "NBA", očísluje od 201 a opraví tvg-id tak, aby
# sedelo s EPG z open-epg.com (HBO.cz@SD -> HBO.cz; FoxSports1.us -> FS1.us).
#
# Pravidlo č. 1: cudziu ani ručne vytvorenú inštanciu IPTV Simple nikdy nemeniť.
import html
import os
import re
import tempfile
import unicodedata
import urllib.parse
import urllib.request
import zlib
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from xml.sax.saxutils import escape

DEFAULT_PLAYLIST_URL = 'https://repo.streamlet.info/playlists/us.m3u'
DEFAULT_EPG_URL = 'https://www.open-epg.com/files/unitedstates1.xml'
INSTANCE_NAME = 'NBA (Bigtime)'
SETTINGS_DIR = 'special://userdata/addon_data/pvr.iptvsimple'
LOCAL_NAME = 'nba-us.m3u'
RAW_NAME = 'nba-raw.m3u'        # vybrané a opravené kanály, ešte neoverené
EPG_NAME = 'nba-epg.xml'        # lokálna kópia EPG (open-epg.com má limit 20 stiahnutí denne)
GROUP = 'NBA'                   # skupina (hlavička) všetkých kanálov v playliste
DEFAULT_CHNO_START = 201        # O2 TV má 1-99, Streamlet CZ od 101

# Čo je "NBA / americký šport". Vyberá sa podľa názvu kanála.
INCLUDE = re.compile(r'\bnba\b|\bespn|fox sports|\bfs[12]\b|\btnt\b|nbc sports|cbs sports', re.I)
EXCLUDE = re.compile(r'novelas|\bnews\b|espnews|golazo|deportes|\bseries\b', re.I)
# Skupina podľa názvu: poradie v skupine NBA (NBA TV prvá).
ORDER = (
    re.compile(r'\bnba\b', re.I),
    re.compile(r'\bespn', re.I),
    re.compile(r'fox sports|\bfs[12]\b', re.I),
    re.compile(r'nbc sports', re.I),
    re.compile(r'\btnt\b', re.I),
)
# tvg-id z playlistu -> id kanála v EPG z open-epg.com (porovnanie bez veľkosti písmen)
EPG_ALIASES = {
    'foxsports1.us': 'FS1.us',
    'foxsports2.us': 'FS2.us',
}

MIN_SOURCE_CHANNELS = 200       # menej = stiahlo sa niečo iné než americký playlist
MIN_CHANNELS = 3                # menej overených kanálov = podozrivé (napr. nejde sieť)
MAX_BYTES = 8 * 1024 * 1024
MIN_EPG_BYTES = 200 * 1024
MAX_EPG_BYTES = 300 * 1024 * 1024
EPG_CHUNK = 64 * 1024
CHECK_TIMEOUT = 12              # s na jednu požiadavku overenia
CHECK_WORKERS = 8
MAX_FAILS = 2                   # kanál sa vynechá až po toľkých po sebe idúcich zlyhaniach
UA = 'Mozilla/5.0 (compatible; service.bigtime.nba)'

NASTAVENY = 'NASTAVENY'         # inštancia číta náš lokálny súbor
TREBA = 'TREBA'                 # inštancia chýba

_INSTANCE_FILE = re.compile(r'^instance-settings-(\d+)\.xml$')
_TVG_ID = re.compile(r'(?<=\s)tvg-id="([^"]*)"')
_ATTR_GROUP = re.compile(r'(?<=\s)group-title="[^"]*"')
_ATTR_CHNO = re.compile(r'(?<=\s)tvg-chno="[^"]*"')
_BRACKETS = re.compile(r'[\(\[][^\)\]]*[\)\]]')


class PlaylistError(Exception):
    """Stiahnutý obsah nie je použiteľný playlist; starý súbor ostáva."""


class EpgError(Exception):
    """Stiahnutý obsah nie je použiteľné EPG (XMLTV); starý súbor ostáva."""


# --- súbory -----------------------------------------------------------------

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
    fd, tmp = tempfile.mkstemp(prefix='.nba-', suffix='.tmp', dir=directory)
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


# --- playlist: čítanie a výber kanálov ------------------------------------------

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


def split_extinf(line):
    """Rozdelí riadok #EXTINF na (hlavička s atribútmi, názov). Názov je text za
    prvou čiarkou mimo úvodzoviek; keď úvodzovky nesedia, za poslednou čiarkou."""
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


def parse(text):
    """Zoznam záznamov (extinf, riadky_s_voľbami, adresa). Záznam = #EXTINF + riadky
    začínajúce # + adresa. Nedokončený záznam na konci sa zahodí."""
    lines = text.replace('\r\n', '\n').split('\n')
    items, i = [], 0
    while i < len(lines):
        if lines[i].startswith('#EXTINF'):
            extinf, opts, j = lines[i], [], i + 1
            while j < len(lines) and lines[j].startswith('#'):
                opts.append(lines[j])
                j += 1
            if j < len(lines) and lines[j].strip():
                items.append((extinf, opts, lines[j].strip()))
            i = j
        i += 1
    return items


def entry_name(extinf):
    return split_extinf(extinf)[1].strip()


def select(items):
    """Kanály, ktoré patria do NBA playlistu. Záznamy s vlastnými HTTP hlavičkami
    (#EXTVLCOPT, #KODIPROP) sa vynechajú: nedajú sa overiť a IPTV Simple ich berie
    inak."""
    out = []
    for extinf, opts, url in items:
        name = entry_name(extinf)
        if not INCLUDE.search(name) or EXCLUDE.search(name):
            continue
        if any(o.startswith(('#EXTVLCOPT', '#KODIPROP')) for o in opts):
            continue
        out.append((extinf, opts, url))
    return out


def _fold(text):
    text = unicodedata.normalize('NFKD', text)
    return ''.join(c for c in text if not unicodedata.combining(c)).lower()


def name_key(name):
    """Názov bez zátvoriek ((1080p), [Geo-blocked]), diakritiky a interpunkcie."""
    return ''.join(re.findall(r'[a-z0-9+]+', _fold(_BRACKETS.sub(' ', html.unescape(name)))))


def order_key(name):
    for n, rx in enumerate(ORDER):
        if rx.search(name):
            return (n, _fold(name))
    return (len(ORDER), _fold(name))


def fix_epg_id(extinf):
    """tvg-id bez prípony za '@' a so zámenou na id, ktoré pozná EPG (FoxSports1.us ->
    FS1.us)."""
    def sub(m):
        base = m.group(1).split('@', 1)[0]
        base = EPG_ALIASES.get(base.casefold(), base)
        return 'tvg-id="%s"' % base
    head, name = split_extinf(extinf)
    head = _TVG_ID.sub(sub, head)
    return head + (',' + name if ',' in extinf else '')


def prepare(items):
    """Zo vybraných záznamov odstráni duplicity názvu (ostane prvý) a opraví tvg-id.
    Poradie ostáva zo zdroja."""
    seen, out = set(), []
    for extinf, opts, url in items:
        key = name_key(entry_name(extinf))
        if key in seen:
            continue
        seen.add(key)
        out.append((fix_epg_id(extinf), opts, url))
    return out


def serialize(items):
    rows = ['#EXTM3U']
    for extinf, opts, url in items:
        rows.append(extinf)
        rows.extend(opts)
        rows.append(url)
    return '\n'.join(rows) + '\n'


def candidates(text):
    """Z celého amerického playlistu vyberie a opraví NBA kanály. Vyhodí
    PlaylistError, keď to nevyzerá ako americký playlist."""
    if not text.lstrip().startswith('#EXTM3U'):
        raise PlaylistError('chýba hlavička #EXTM3U')
    items = parse(text)
    if len(items) < MIN_SOURCE_CHANNELS:
        raise PlaylistError('iba %d kanálov (minimum %d)' % (len(items), MIN_SOURCE_CHANNELS))
    chosen = prepare(select(items))
    if not chosen:
        raise PlaylistError('v playliste nie je žiadny športový kanál')
    return chosen


# --- overenie streamov ---------------------------------------------------------

def _fetch(url, timeout, opener, byte_range=None, limit=300000):
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    if byte_range:
        req.add_header('Range', byte_range)
    with opener(req, timeout=timeout) as r:
        return r.read(limit)


def _uris(txt):
    return [l.strip() for l in txt.splitlines() if l.strip() and not l.startswith('#')]


def check_stream(url, timeout=CHECK_TIMEOUT, opener=urllib.request.urlopen):
    """Odpovedá stream? Stiahne manifest HLS, zostúpi k zoznamu segmentov a načíta
    začiatok posledného segmentu. Vráti (True/False, dôvod)."""
    try:
        txt = _fetch(url, timeout, opener).decode('utf-8', 'ignore')
        if '#EXTM3U' not in txt:
            return False, 'nie je HLS'
        lines = _uris(txt)
        for _ in range(3):                      # master -> media playlist
            if '#EXT-X-STREAM-INF' not in txt:
                break
            if not lines:
                return False, 'prázdny playlist'
            url = urllib.parse.urljoin(url, lines[0])
            txt = _fetch(url, timeout, opener).decode('utf-8', 'ignore')
            lines = _uris(txt)
        if '#EXT-X-STREAM-INF' in txt or not lines:
            return False, 'bez segmentov'
        data = _fetch(urllib.parse.urljoin(url, lines[-1]), timeout, opener, 'bytes=0-1999', 4000)
        if len(data) < 100 or data[:1] == b'<':
            return False, 'zlý segment'
        return True, 'ok'
    except Exception as exc:
        return False, '%s' % (exc.__class__.__name__,)


def check_all(items, workers=CHECK_WORKERS, timeout=CHECK_TIMEOUT, opener=urllib.request.urlopen,
              abort=lambda: False):
    """Overí všetky adresy naraz. Vráti {adresa: (ok, dôvod)}; po `abort()` sa
    nedokončené preskočia (chýbajú vo výsledku)."""
    def one(item):
        if abort():
            return item[2], None
        return item[2], check_stream(item[2], timeout, opener)

    with ThreadPoolExecutor(max(1, workers)) as ex:
        results = list(ex.map(one, items))
    return {url: res for url, res in results if res is not None}


def keep_working(items, results, fails=None, known=None, max_fails=MAX_FAILS):
    """Vyberie kanály, ktoré prešli overením. `known` = adresy, ktoré už raz prešli
    a sú v playliste (None = všetky). Kanál z `known`, ktorý neprešiel, ostane, kým
    nezlyhá `max_fails`-krát po sebe (krátky výpadok ho nevyhodí). Kanál, ktorý ešte
    nikdy neprešiel (nový alebo už raz zlyhal), sa pri zlyhaní vynechá hneď, inak by
    sa nefunkčné kanály dostali do playlistu hneď pri prvom spustení. Vráti (kanály,
    nové počítadlo zlyhaní {adresa: n}, názvy vynechaných). Kanál bez výsledku
    (overenie sa prerušilo) ostáva a počítadlo sa nemení."""
    fails = fails or {}
    kept, new_fails, dropped = [], {}, []
    for item in items:
        url = item[2]
        res = results.get(url)
        was_ok = known is None or url in known
        if res is None:
            if fails.get(url):
                new_fails[url] = fails[url]
            kept.append(item)
        elif res[0]:
            kept.append(item)
        else:
            n = int(fails.get(url, 0)) + 1
            new_fails[url] = n
            if was_ok and n < max_fails:
                kept.append(item)
            else:
                dropped.append(entry_name(item[0]))
    return kept, new_fails, dropped


# --- výsledný lokálny playlist -------------------------------------------------

def _put_attr(head, regex, name, value):
    attr = '%s="%s"' % (name, value.replace('"', "'"))
    parts = regex.split(head)
    if len(parts) == 1:
        return head.rstrip() + ' ' + attr
    return parts[0] + attr + ''.join(parts[1:])


def with_attrs(extinf, group, chno):
    """Riadok #EXTINF s `group-title` a `tvg-chno` (existujúce sa nahradia)."""
    head, name = split_extinf(extinf)
    head = _put_attr(head, _ATTR_GROUP, 'group-title', group)
    head = _put_attr(head, _ATTR_CHNO, 'tvg-chno', str(chno))
    return head + (',' + name if ',' in extinf else '')


def render(items, chno_start=DEFAULT_CHNO_START):
    """Lokálny playlist: všetky kanály v skupine NBA, číslované od `chno_start` podľa
    order_key (NBA TV, ESPN, Fox Sports, NBC Sports, TNT, názov)."""
    ranked = sorted(range(len(items)), key=lambda i: (order_key(entry_name(items[i][0])), i))
    out = list(items)
    for n, i in enumerate(ranked):
        extinf, opts, url = items[i]
        out[i] = (with_attrs(extinf, GROUP, chno_start + n), opts, url)
    return serialize(out)


# --- EPG (XMLTV) ---------------------------------------------------------------

def _page_message(head):
    text = re.sub(r'<[^>]+>', ' ', head.decode('utf-8', 'replace'))
    return ' '.join(text.split())[:120]


def download_epg(url, dest, timeout=90, opener=urllib.request.urlopen):
    """Stiahne XMLTV (aj gzip) po častiach do dočasného súboru, overí ho a atómovo
    ním nahradí `dest`. Vráti veľkosť v bajtoch. Pri chybe vyhodí výnimku a starý
    súbor ostane nedotknutý. (open-epg.com dovolí 20 stiahnutí denne na IP a potom
    vráti stránku s hláškou namiesto XML.)"""
    if not url.lower().startswith(('https://', 'http://')):
        raise EpgError('nepodporovaná URL')
    directory = os.path.dirname(dest)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.epg-', suffix='.tmp', dir=directory)
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept-Encoding': 'gzip'})
    try:
        size, head, tail = 0, b'', b''
        decoder, first = None, True
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


_EPG_CHANNEL_TAG = re.compile(rb'<channel\b[^>]*>')
_EPG_ID_ATTR = re.compile(rb'\sid\s*=\s*(?:"([^"]*)"|\'([^\']*)\')')


def epg_channel_ids(path, chunk=EPG_CHUNK):
    """Množina ID kanálov (`<channel id="...">`, casefold) z lokálneho XMLTV, čítaná
    po častiach. None, keď sa nedá prečítať."""
    ids, carry = set(), b''
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
                carry = (rest[i:] if len(rest) - i <= 8192 else b'') if i >= 0 else rest[-8:]
    except OSError:
        return None
    return ids


def epg_coverage(text, ids):
    """(počet kanálov s EPG, počet všetkých) pre lokálny playlist; None, keď ID chýbajú."""
    if not ids:
        return None
    items = parse(text)
    have = 0
    for extinf, _o, _u in items:
        m = _TVG_ID.search(split_extinf(extinf)[0])
        if m and m.group(1).casefold() in ids:
            have += 1
    return have, len(items)


# --- inštancie IPTV Simple -----------------------------------------------------

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
    """NASTAVENY: nejaká inštancia číta náš súbor. Inak TREBA."""
    for _num, path in list_instances(settings_dir):
        m3u = read_setting(path, 'm3uPath')
        if m3u:
            try:
                if _same_file(translate(m3u), local_path):
                    return NASTAVENY
            except Exception:
                pass
    return TREBA


def build_settings_xml(m3u_path, epg_url, name=INSTANCE_NAME, epg_path=None):
    """Minimálna inštancia; ostatné kľúče si Kodi doplní z defaultov. Playlist sa
    z lokálneho súboru znova načíta každú hodinu. S `epg_path` číta EPG z lokálneho
    súboru, inak z `epg_url`."""
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


def _write_new(directory, target, data):
    fd, tmp = tempfile.mkstemp(prefix='.bigtime-', suffix='.tmp', dir=directory)
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


def create_instance(settings_dir, m3u_path, epg_url, name=INSTANCE_NAME, epg_path=None):
    """Zapíše novú inštanciu pod prvým voľným číslom (atómovo). Vráti jej cestu."""
    os.makedirs(settings_dir, exist_ok=True)
    target = os.path.join(settings_dir, 'instance-settings-%d.xml' % free_number(settings_dir))
    _write_new(settings_dir, target, build_settings_xml(m3u_path, epg_url, name, epg_path).encode('utf-8'))
    return target


def _set(root, setting_id, value):
    for el in root.iter('setting'):
        if el.get('id') == setting_id:
            el.text = value
            el.attrib.pop('default', None)
            return
    el = ET.SubElement(root, 'setting', {'id': setting_id})
    el.text = value
    el.tail = '\n'


def migrate_epg(settings_dir, m3u_local, epg_url, epg_special, translate=_identity):
    """Naša inštancia (číta náš playlist), ktorá berie EPG priamo z `epg_url`, sa
    prepne na lokálny súbor `epg_special`. Iné inštancie sa nemenia. Vráti cestu
    zmenenej inštancie alebo None."""
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
