# Nastavenie IPTV Simple pre export O2 TV. Bez importov Kodi (testovateľné).
#
# Platí rovnako pre O2 TV aj Magio GO (každý má vlastnú značku, playlist a EPG).
#
# Pravidlo č. 1: funkčnú konfiguráciu nikdy nemeniť. Ak už nejaká inštancia
# číta playlist s `plugin://plugin.video.o2tv/`, nerobí sa nič. Nová inštancia
# vzniká len keď žiadna taká nie je a o2tv už playlist exportoval; cudzie
# inštancie sa nečítajú na zmenu ani nemažú, nová dostane prvé voľné číslo.
import os
import re
import tempfile
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

MARKER = b'plugin://plugin.video.o2tv/'
INSTANCE_NAME = 'O2 TV (Bigtime)'
DEFAULT_PLAYLIST = 'special://userdata/addon_data/plugin.video.o2tv/playlist.m3u'
DEFAULT_EPG = 'special://userdata/addon_data/plugin.video.o2tv/epg.xml'
# Magio GO: export z plugin.video.magiogo; obnova každých 15 min je nutná, lebo
# služba doplnku prepisuje epg.xml (zelená guľička archívu) a IPTV Simple ho
# načíta len pri obnove playlistu.
MAGIO_MARKER = b'plugin://plugin.video.magiogo/'
MAGIO_INSTANCE_NAME = 'Magio GO (Bigtime)'
MAGIO_PLAYLIST = 'special://userdata/addon_data/plugin.video.magiogo/playlist.m3u'
MAGIO_EPG = 'special://userdata/addon_data/plugin.video.magiogo/epg.xml'
MAGIO_EXTRA = (
    ('m3uRefreshMode', '1'),
    ('m3uRefreshIntervalMins', '15'),
    ('epgCache', 'true'),
)
# Spoločná inštancia: doplnok service.bigtime.setup spojí playlisty a EPG všetkých
# zdrojov (merge.py), aby Kodi nemalo každú skupinu (Šport...) po jednej za zdroj.
MERGED_MARKER = b'#BIGTIME-MERGED'
MERGED_NAME = 'Bigtime TV'
MERGED_PLAYLIST = 'special://userdata/addon_data/service.bigtime.setup/bigtime.m3u'
MERGED_EPG = 'special://userdata/addon_data/service.bigtime.setup/bigtime-epg.xml'
SOURCE_PLAYLISTS = (
    'special://userdata/addon_data/plugin.video.o2tv/playlist.m3u',
    'special://userdata/addon_data/plugin.video.magiogo/playlist.m3u',
    'special://userdata/addon_data/service.bigtime.streamlet/streamlet-cz.m3u',
)
SOURCE_MARKERS = (MARKER, MAGIO_MARKER)

SPOJENE = 'SPOJENE'         # nejaká inštancia číta spoločný playlist
SAMOSTATNE = 'SAMOSTATNE'   # inštancie čítajú zdroje samostatne (staré nastavenie)
ZIADNA = 'ZIADNA'           # žiadna inštancia nečíta ani spoločný playlist, ani zdroj
SETTINGS_DIR = 'special://userdata/addon_data/pvr.iptvsimple'

NASTAVENY = 'NASTAVENY'     # nejaká inštancia už číta playlist z o2tv
CAKA = 'CAKA'               # o2tv ešte neexportoval playlist (chýba prihlásenie)
TREBA = 'TREBA'             # export existuje, inštancia chýba

_INSTANCE_FILE = re.compile(r'^instance-settings-(\d+)\.xml$')


def _identity(path):
    return path


def list_instances(settings_dir):
    """Zoznam (číslo, cesta k súboru) zoradený podľa čísla."""
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
    """Hodnota nastavenia z instance-settings-N.xml, alebo None."""
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return None
    for el in root.iter('setting'):
        if el.get('id') == setting_id:
            return (el.text or '').strip()
    return None


def file_has_marker(path, marker=MARKER):
    """True, ak súbor existuje a obsahuje odkaz na plugin (predvolene o2tv).
    `marker` môže byť aj n-tica, stačí jeden."""
    markers = marker if isinstance(marker, (tuple, list)) else (marker,)
    try:
        keep = max(len(m) for m in markers) - 1
        tail = b''
        with open(path, 'rb') as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    return False
                if any(m in tail + chunk for m in markers):
                    return True
                tail = (tail + chunk)[-keep:]
    except OSError:
        return False


def is_configured(settings_dir, translate=_identity, marker=MARKER):
    """Číta niektorá inštancia IPTV Simple existujúci playlist s danou značkou?"""
    for _num, path in list_instances(settings_dir):
        m3u = read_setting(path, 'm3uPath')
        if not m3u:
            continue
        try:
            real = translate(m3u)
        except Exception:
            continue
        if real and file_has_marker(real, marker):
            return True
    return False


def _real(path, translate):
    try:
        return os.path.normpath(translate(path))
    except Exception:
        return None


def mode(settings_dir, translate=_identity):
    """SPOJENE: niektorá inštancia číta spoločný playlist. SAMOSTATNE: číta aspoň
    jeden zdroj (playlist O2 TV / Magio GO podľa značky, alebo súbor Streamletu).
    ZIADNA: nič z toho. Cudzie inštancie sa len čítajú."""
    merged = _real(MERGED_PLAYLIST, translate)
    sources = {_real(p, translate) for p in SOURCE_PLAYLISTS}
    standalone = False
    for _num, path in list_instances(settings_dir):
        m3u = read_setting(path, 'm3uPath')
        if not m3u:
            continue
        real = _real(m3u, translate)
        if real is None:
            continue
        if real == merged or file_has_marker(real, MERGED_MARKER):
            return SPOJENE
        if real in sources or file_has_marker(real, SOURCE_MARKERS):
            standalone = True
    return SAMOSTATNE if standalone else ZIADNA


def status(settings_dir, playlist, translate=_identity, marker=MARKER):
    if is_configured(settings_dir, translate, marker):
        return NASTAVENY
    if file_has_marker(playlist, marker):
        return TREBA
    return CAKA


def build_settings_xml(m3u_path, epg_path, name=INSTANCE_NAME, extra=()):
    """Minimálna inštancia; ostatné kľúče si Kodi doplní z defaultov."""
    values = (
        ('kodi_addon_instance_name', name),
        ('m3uPathType', '0'),
        ('m3uPath', m3u_path),
        ('epgPathType', '0'),
        ('epgPath', epg_path),
        ('epgTSOverride', 'true'),
        ('catchupEnabled', 'true'),
        ('catchupDays', '7'),
    ) + tuple(extra)
    rows = ''.join('    <setting id="%s">%s</setting>\n' % (k, escape(v)) for k, v in values)
    return '<settings version="2">\n' + rows + '</settings>\n'


def free_number(settings_dir):
    used = {n for n, _p in list_instances(settings_dir)}
    n = 1
    while n in used:
        n += 1
    return n


def create_instance(settings_dir, m3u_path, epg_path, name=INSTANCE_NAME, extra=()):
    """Zapíše novú inštanciu pod prvým voľným číslom (atómovo). Vráti jej cestu."""
    os.makedirs(settings_dir, exist_ok=True)
    num = free_number(settings_dir)
    target = os.path.join(settings_dir, 'instance-settings-%d.xml' % num)
    data = build_settings_xml(m3u_path, epg_path, name, extra).encode('utf-8')
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
