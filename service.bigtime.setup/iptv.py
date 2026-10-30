# Nastavenie IPTV Simple pre export O2 TV. Bez importov Kodi (testovateľné).
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


def file_has_marker(path):
    """True, ak súbor existuje a obsahuje odkaz na plugin o2tv."""
    try:
        keep = len(MARKER) - 1
        tail = b''
        with open(path, 'rb') as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    return False
                if MARKER in tail + chunk:
                    return True
                tail = (tail + chunk)[-keep:]
    except OSError:
        return False


def is_configured(settings_dir, translate=_identity):
    """Číta niektorá inštancia IPTV Simple existujúci playlist z o2tv?"""
    for _num, path in list_instances(settings_dir):
        m3u = read_setting(path, 'm3uPath')
        if not m3u:
            continue
        try:
            real = translate(m3u)
        except Exception:
            continue
        if real and file_has_marker(real):
            return True
    return False


def status(settings_dir, playlist, translate=_identity):
    if is_configured(settings_dir, translate):
        return NASTAVENY
    if file_has_marker(playlist):
        return TREBA
    return CAKA


def build_settings_xml(m3u_path, epg_path, name=INSTANCE_NAME):
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
    )
    rows = ''.join('    <setting id="%s">%s</setting>\n' % (k, escape(v)) for k, v in values)
    return '<settings version="2">\n' + rows + '</settings>\n'


def free_number(settings_dir):
    used = {n for n, _p in list_instances(settings_dir)}
    n = 1
    while n in used:
        n += 1
    return n


def create_instance(settings_dir, m3u_path, epg_path, name=INSTANCE_NAME):
    """Zapíše novú inštanciu pod prvým voľným číslom (atómovo). Vráti jej cestu."""
    os.makedirs(settings_dir, exist_ok=True)
    num = free_number(settings_dir)
    target = os.path.join(settings_dir, 'instance-settings-%d.xml' % num)
    data = build_settings_xml(m3u_path, epg_path, name).encode('utf-8')
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
