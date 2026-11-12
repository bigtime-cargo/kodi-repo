# Playlist streamletu a inštancia IPTV Simple. Bez importov Kodi (testovateľné).
#
# Playlist má tvg-id="HBO.cz@SD" (alebo @Czechia, @HD, @UHD), EPG z open-epg.com
# má channel id="HBO.cz". IPTV Simple porovnáva ID presne, takže bez opravy sa
# nespáruje nič. Doplnok preto playlist stiahne, od '@' odstrihne príponu a uloží
# ho lokálne; inštancia IPTV Simple číta ten lokálny súbor.
#
# Pravidlo č. 1: cudziu ani ručne vytvorenú inštanciu nikdy nemeniť. Nová vzniká
# len keď žiadna nečíta lokálny súbor ani priamo URL streamletu.
import os
import re
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

DEFAULT_PLAYLIST_URL = 'https://repo.streamlet.info/playlists/cz.m3u'
DEFAULT_EPG_URL = 'https://www.open-epg.com/files/czech.xml'
STREAMLET_HOST = 'repo.streamlet.info'
INSTANCE_NAME = 'Streamlet CZ (Bigtime)'
SETTINGS_DIR = 'special://userdata/addon_data/pvr.iptvsimple'
LOCAL_NAME = 'streamlet-cz.m3u'

MIN_CHANNELS = 20            # menej = stiahlo sa niečo iné než playlist
MAX_BYTES = 5 * 1024 * 1024
KEEP_RATIO = 0.5             # nový playlist s menej ako polovicou kanálov sa zahodí
UA = 'Mozilla/5.0 (compatible; service.bigtime.streamlet)'

NASTAVENY = 'NASTAVENY'      # inštancia číta náš lokálny súbor
RUCNA = 'RUCNA'              # ručná inštancia číta URL streamletu priamo
TREBA = 'TREBA'              # inštancia chýba

_INSTANCE_FILE = re.compile(r'^instance-settings-(\d+)\.xml$')
_TVG_ID = re.compile(r'(tvg-id=")([^"]*)"')


class PlaylistError(Exception):
    """Stiahnutý obsah nie je použiteľný playlist; starý súbor ostáva."""


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


def refresh(url, local_path, timeout=30, opener=urllib.request.urlopen):
    """Stiahne, opraví a zapíše playlist. Vráti (kanály, zmenených ID, zmenil sa súbor).
    Pri akejkoľvek chybe vyhodí výnimku a starý súbor ostane nedotknutý."""
    text = download(url, timeout, opener)
    old = read_text(local_path)
    old_count = count_channels(old) if old else 0
    n = validate(text, old_count)
    fixed, changed = fix_playlist(text)
    return n, changed, write_atomic(local_path, fixed)


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


def build_settings_xml(m3u_path, epg_url, name=INSTANCE_NAME):
    """Minimálna inštancia; ostatné kľúče si Kodi doplní z defaultov.
    Playlist sa z lokálneho súboru znova načíta každú hodinu."""
    values = (
        ('kodi_addon_instance_name', name),
        ('m3uPathType', '0'),
        ('m3uPath', m3u_path),
        ('m3uRefreshMode', '1'),
        ('m3uRefreshIntervalMins', '60'),
        ('epgPathType', '1'),
        ('epgUrl', epg_url),
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


def create_instance(settings_dir, m3u_path, epg_url, name=INSTANCE_NAME):
    """Zapíše novú inštanciu pod prvým voľným číslom (atómovo). Vráti jej cestu."""
    os.makedirs(settings_dir, exist_ok=True)
    target = os.path.join(settings_dir, 'instance-settings-%d.xml' % free_number(settings_dir))
    data = build_settings_xml(m3u_path, epg_url, name).encode('utf-8')
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
