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
# Spoločná inštancia: obnova každých 5 min (do 1.3.1 15), pozri retime_merged.
OLD_REFRESH_MINS = '15'
MERGED_REFRESH_MINS = '5'
MERGED_EXTRA = (
    ('m3uRefreshMode', '1'),
    ('m3uRefreshIntervalMins', MERGED_REFRESH_MINS),
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
# Migrácia starých samostatných inštancií: prázdny playlist pre inštancie, ktoré sa vyradia
EMPTY_PLAYLIST = 'special://userdata/addon_data/service.bigtime.setup/prazdny.m3u'
ROLE_O2, ROLE_MAGIO, ROLE_STREAMLET = 'o2tv', 'magiogo', 'streamlet'
ROLE_ORDER = (ROLE_O2, ROLE_MAGIO, ROLE_STREAMLET)      # ktorá inštancia sa stane spoločnou

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


def _values(m3u_path, epg_path, name=INSTANCE_NAME, extra=()):
    return (
        ('kodi_addon_instance_name', name),
        ('m3uPathType', '0'),
        ('m3uPath', m3u_path),
        ('epgPathType', '0'),
        ('epgPath', epg_path),
        ('epgTSOverride', 'true'),
        ('catchupEnabled', 'true'),
        ('catchupDays', '7'),
    ) + tuple(extra)


def build_settings_xml(m3u_path, epg_path, name=INSTANCE_NAME, extra=()):
    """Minimálna inštancia; ostatné kľúče si Kodi doplní z defaultov."""
    rows = ''.join('    <setting id="%s">%s</setting>\n' % (k, escape(v))
                   for k, v in _values(m3u_path, epg_path, name, extra))
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


# --- migrácia starých samostatných inštancií -----------------------------------
# Skúška na PC (Kodi 21.3): súbory instance-settings-N.xml prepísané za behu Kodi ostanú a po
# reštarte Kodi samo zmaže kanály inštancií, ktoré čítajú prázdny playlist, takže netreba
# resetovať PVR databázu. Inštancia O2 TV (jej kanály majú v databáze rovnaké riadky) sa
# prepíše na spoločný zoznam a obľúbené O2 kanály sa zachovajú.

def instance_roles(settings_dir, translate=_identity):
    """[(číslo, cesta, rola)] inštancií, ktoré čítajú niektorý náš zdroj (O2 TV, Magio GO,
    Streamlet) samostatne. Inštancie so spoločným playlistom a cudzie sa neberú."""
    by_path = {_real(p, translate): r for p, r in zip(SOURCE_PLAYLISTS, ROLE_ORDER)}
    found = []
    for num, path in list_instances(settings_dir):
        m3u = read_setting(path, 'm3uPath')
        if not m3u:
            continue
        real = _real(m3u, translate)
        if real is None or file_has_marker(real, MERGED_MARKER):
            continue
        role = by_path.get(real)
        if role is None:
            if file_has_marker(real, MARKER):
                role = ROLE_O2
            elif file_has_marker(real, MAGIO_MARKER):
                role = ROLE_MAGIO
        if role:
            found.append((num, path, role))
    return found


def plan_migration(settings_dir, translate=_identity):
    """Plán prepísania, alebo None. `primary` (číslo, cesta, rola) sa stane spoločnou
    inštanciou (O2 TV, potom Magio GO, potom Streamlet; pri rovnakej role nižšie číslo),
    `others` sa vyprázdnia, `roles` sú zdroje, ktoré musia mať export."""
    found = instance_roles(settings_dir, translate)
    if not found:
        return None
    primary = min(found, key=lambda r: (ROLE_ORDER.index(r[2]), r[0]))
    return {'primary': primary, 'others': [r for r in found if r[0] != primary[0]],
            'roles': sorted({r[2] for r in found}, key=ROLE_ORDER.index)}


def render_instance(path, values):
    """Text inštancie `path` s upravenými nastaveniami `values` ((id, hodnota)...). Ostatné
    nastavenia ostanú; pri zmenenom sa zahodí `default="true"`. Čítanie, nič nezapisuje."""
    root = ET.parse(path).getroot()
    present = {el.get('id'): el for el in root.iter('setting')}
    for key, value in values:
        el = present.get(key)
        if el is None:
            el = ET.SubElement(root, 'setting', {'id': key})
            present[key] = el
        el.text = value
        el.attrib.pop('default', None)
    return ET.tostring(root, encoding='unicode') + '\n'


def write_atomic(path, text):
    """Zapíše súbor atómovo (dočasný súbor v tom istom adresári + fsync + replace)."""
    directory = os.path.dirname(path) or '.'
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.bigtime-', suffix='.tmp', dir=directory)
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


def merged_values(m3u_path, epg_path):
    """Nastavenia spoločnej inštancie (rovnaké ako pri novo vytvorenej)."""
    return _values(m3u_path, epg_path, MERGED_NAME, MERGED_EXTRA)


def retime_merged(settings_dir, translate=_identity):
    """Spoločným inštanciám s našou starou obnovou 15 min (do 1.3.1) skráti obnovu na
    MERGED_REFRESH_MINS, aby sa zelené guľôčky archívu po štarte objavili skôr (IPTV Simple číta
    EPG pri štarte skôr, než ho služby Magio GO a setup zapíšu nanovo, a potom až pri obnove).
    Iná hodnota (nastavená ručne) ostane. Vráti zoznam zmenených súborov."""
    merged = _real(MERGED_PLAYLIST, translate)
    changed = []
    for _num, path in list_instances(settings_dir):
        m3u = read_setting(path, 'm3uPath')
        real = _real(m3u, translate) if m3u else None
        if real is None or not (real == merged or file_has_marker(real, MERGED_MARKER)):
            continue
        if read_setting(path, 'm3uRefreshIntervalMins') != OLD_REFRESH_MINS:
            continue
        write_atomic(path, render_instance(path, (('m3uRefreshIntervalMins', MERGED_REFRESH_MINS),)))
        changed.append(path)
    return changed


def emptied_values(empty_path, epg_path):
    """Inštancia, ktorá nemá poskytovať žiadne kanály (názov ostáva, mení sa len zdroj)."""
    return (('m3uPathType', '0'), ('m3uPath', empty_path), ('epgPathType', '0'), ('epgPath', epg_path))
