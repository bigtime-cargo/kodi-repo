# Logika s Kodi pre službu (service.py) aj skript (default.py). Výnimky sa
# zachytávajú tu, aby chyba siete nikdy nezhodila službu.
import json
import os
import tempfile
import time
import urllib.error

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

import streamlet

ADDON_ID = 'service.bigtime.streamlet'
TAG = '[%s] ' % ADDON_ID
HEADING = 'Bigtime Streamlet'
PVR_IPTV = 'pvr.iptvsimple'

DEFAULT_HOURS = 12
RETRY_AFTER = 1800           # s po zlyhanom sťahovaní playlistu
EPG_RETRY_AFTER = 3 * 3600   # s po odmietnutí od servera (open-epg.com počíta aj neúspešné pokusy)
EPG_NET_RETRY_AFTER = 600    # s po chybe siete (server neodpovedal, limit sa nespotrebuje)
DEFAULT_EPG_HOURS = 12

STATUS_LABELS = {
    streamlet.NASTAVENY: 'nastavená',
    streamlet.RUCNA: 'ručná inštancia číta URL streamletu priamo (treba ju prepnúť na lokálny súbor)',
    streamlet.TREBA: 'treba vytvoriť',
}


def log(msg, level=xbmc.LOGINFO):
    xbmc.log(TAG + msg, level)


def _setting(key, default):
    try:
        value = xbmcaddon.Addon(ADDON_ID).getSetting(key).strip()
    except Exception:
        value = ''
    return value or default


def playlist_url():
    return _setting('playlist_url', streamlet.DEFAULT_PLAYLIST_URL)


def epg_url():
    return _setting('epg_url', streamlet.DEFAULT_EPG_URL)


def dedupe_enabled():
    return _setting('dedupe_o2', 'true').lower() != 'false'


def epg_local_enabled():
    return _setting('epg_local', 'true').lower() != 'false'


def epg_interval_seconds():
    try:
        hours = int(_setting('epg_hours', str(DEFAULT_EPG_HOURS)))
    except ValueError:
        hours = DEFAULT_EPG_HOURS
    return max(1, min(hours, 168)) * 3600


def interval_seconds():
    try:
        hours = int(_setting('refresh_hours', str(DEFAULT_HOURS)))
    except ValueError:
        hours = DEFAULT_HOURS
    return max(1, min(hours, 168)) * 3600


# --- cesty ------------------------------------------------------------------

def data_dir():
    return xbmcvfs.translatePath('special://userdata/addon_data/%s' % ADDON_ID)


def local_path():
    return os.path.join(data_dir(), streamlet.LOCAL_NAME)


def epg_path():
    return os.path.join(data_dir(), streamlet.EPG_NAME)


def epg_special():
    return 'special://userdata/addon_data/%s/%s' % (ADDON_ID, streamlet.EPG_NAME)


def raw_path():
    return os.path.join(data_dir(), streamlet.RAW_NAME)


def o2_playlist_path():
    return translate(streamlet.O2_PLAYLIST)


def local_special():
    return 'special://userdata/addon_data/%s/%s' % (ADDON_ID, streamlet.LOCAL_NAME)


def iptv_dir():
    return xbmcvfs.translatePath(streamlet.SETTINGS_DIR)


def translate(path):
    return xbmcvfs.translatePath(path) if path.startswith('special://') else path


# --- stav -------------------------------------------------------------------

def _state_path():
    return os.path.join(data_dir(), 'state.json')


def load_state():
    try:
        with open(_state_path(), 'rb') as f:
            data = json.loads(f.read().decode('utf-8'))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def save_state(state):
    try:
        os.makedirs(data_dir(), exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix='.state-', suffix='.tmp', dir=data_dir())
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(json.dumps(state, indent=1, sort_keys=True).encode('utf-8'))
            os.replace(tmp, _state_path())
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as exc:
        log('stav sa nepodarilo uložiť: %r' % exc, xbmc.LOGWARNING)


def toast(message, warning=False):
    try:
        icon = xbmcgui.NOTIFICATION_WARNING if warning else xbmcgui.NOTIFICATION_INFO
        xbmcgui.Dialog().notification(HEADING, message, icon, 6000)
    except Exception:
        pass


def iptv_available():
    return bool(xbmc.getCondVisibility('System.AddonIsEnabled(%s)' % PVR_IPTV))


# --- jedno kolo -------------------------------------------------------------

def is_due(state, now=None):
    """Treba playlist obnoviť? Hneď, keď súbor chýba; inak po intervale od
    posledného úspechu; po zlyhaní najskôr po RETRY_AFTER."""
    now = time.time() if now is None else now
    if state.get('last_fail') and now - float(state.get('last_try', 0) or 0) < RETRY_AFTER:
        return False
    if not os.path.isfile(local_path()):
        return True
    return now - float(state.get('last_ok', 0) or 0) >= interval_seconds()


def is_epg_due(state, now=None):
    """Treba EPG stiahnuť? Len keď je zapnuté lokálne EPG; po zlyhaní (aj pri
    limite open-epg.com) najskôr po EPG_RETRY_AFTER; inak keď súbor chýba alebo
    po intervale od posledného úspechu."""
    if not epg_local_enabled():
        return False
    now = time.time() if now is None else now
    wait = float(state.get('epg_wait', EPG_RETRY_AFTER) or EPG_RETRY_AFTER)
    if state.get('epg_fail') and now - float(state.get('epg_try', 0) or 0) < wait:
        return False
    if not streamlet.epg_valid(epg_path()):
        return True
    return now - float(state.get('epg_ok', 0) or 0) >= epg_interval_seconds()


def refresh_epg(state):
    """Stiahne EPG do addon_data. Vráti True pri úspechu."""
    now = time.time()
    state['epg_try'] = now
    try:
        size = streamlet.download_epg(epg_url(), epg_path())
    except Exception as exc:
        state['epg_fail'] = '%s' % exc
        # server odpovedal (stránka s limitom, HTTP chyba, zlý obsah) -> dlhá pauza;
        # sieť sa nepodarilo ani otvoriť -> krátka, nič sa nespotrebovalo
        answered = isinstance(exc, (streamlet.EpgError, urllib.error.HTTPError))
        state['epg_wait'] = EPG_RETRY_AFTER if answered else EPG_NET_RETRY_AFTER
        log('sťahovanie EPG zlyhalo: %s' % exc, xbmc.LOGWARNING)
        return False
    state.pop('epg_fail', None)
    state.pop('epg_wait', None)
    state['epg_ok'] = now
    state['epg_size'] = size
    log('EPG: %.1f MB' % (size / 1048576.0))
    return True


def migrate_epg(state):
    """Keď je lokálne EPG pripravené, naša inštancia sa prepne z URL na súbor.
    Prejaví sa po reštarte Kodi (upozorní sa len raz)."""
    if not epg_local_enabled() or not streamlet.epg_valid(epg_path()):
        return
    if not iptv_available() or not os.path.isfile(local_path()):
        return
    changed = streamlet.migrate_epg(
        iptv_dir(), local_path(), epg_url(), epg_special(), translate)
    if changed:
        log('inštancia %s číta EPG z lokálneho súboru' % os.path.basename(changed))
        if not state.get('epg_migrated'):
            toast('EPG sa bude čítať z lokálneho súboru, reštartuj Kodi')
        state['epg_migrated'] = time.time()


def current_o2_keys():
    """Kľúče názvov kanálov O2 TV, alebo None (filter vypnutý / export O2 chýba)."""
    if not dedupe_enabled():
        return None
    return streamlet.o2_keys(streamlet.read_text(o2_playlist_path()))


def _remember_filter(state, keys, removed):
    state['filter_sig'] = streamlet.keys_signature(keys)
    state['removed'] = removed


def refresh_playlist(state):
    """Stiahne playlist. Vráti True pri úspechu. Stav sa mení v mieste."""
    now = time.time()
    state['last_try'] = now
    keys = current_o2_keys()
    try:
        n, changed, written, removed = streamlet.refresh(
            playlist_url(), local_path(), raw_path(), keys)
    except Exception as exc:
        state['last_fail'] = '%s' % exc
        log('sťahovanie playlistu zlyhalo: %r' % exc, xbmc.LOGWARNING)
        return False
    state.pop('last_fail', None)
    state['last_ok'] = now
    state['channels'] = n
    _remember_filter(state, keys, removed)
    log('playlist: %d kanálov, opravených ID %d, vynechaných duplicít %d, %s'
        % (n, changed, len(removed), 'zapísaný' if written else 'bez zmeny'))
    return True


def apply_filter(state):
    """Zoznam O2 TV (alebo nastavenie) sa zmenil: lokálny playlist sa znova
    vyrobí z uloženého bez sťahovania."""
    keys = current_o2_keys()
    if streamlet.keys_signature(keys) == state.get('filter_sig', ''):
        return
    res = streamlet.refilter(raw_path(), local_path(), keys)
    if res is None:
        return          # uložený playlist ešte nie je, filter sa použije pri sťahovaní
    written, removed = res
    _remember_filter(state, keys, removed)
    log('filter duplicít s O2 TV: vynechaných %d, %s'
        % (len(removed), 'playlist prepísaný' if written else 'bez zmeny'))


def ensure_instance(state):
    """Vytvorí inštanciu IPTV Simple, ak chýba. Vráti stav (streamlet.*) alebo None."""
    if not iptv_available() or not os.path.isfile(local_path()):
        return None
    st = streamlet.status(iptv_dir(), local_path(), translate)
    if st == streamlet.TREBA:
        use_file = epg_local_enabled() and streamlet.epg_valid(epg_path())
        streamlet.create_instance(iptv_dir(), local_special(), epg_url(),
                                  epg_path=epg_special() if use_file else None)
        state['instance_created'] = time.time()
        log('vytvorená inštancia IPTV Simple')
        toast('Streamlet pridaný do IPTV Simple, reštartuj Kodi')
    elif st == streamlet.RUCNA and not state.get('warned_manual'):
        state['warned_manual'] = True
        log('ručná inštancia číta URL streamletu priamo, nemením ju', xbmc.LOGWARNING)
    return st


def cycle(force=False):
    """Jedno kolo služby. Vráti stav."""
    state = load_state()
    try:
        if force or is_due(state):
            refresh_playlist(state)
        else:
            apply_filter(state)
        if force and epg_local_enabled():
            state.pop('epg_fail', None)
            refresh_epg(state)
        elif is_epg_due(state):
            refresh_epg(state)
        ensure_instance(state)
        migrate_epg(state)
    except Exception as exc:
        log('kolo zlyhalo: %r' % exc, xbmc.LOGERROR)
    save_state(state)
    return state


def status_lines():
    state = load_state()
    lines = []
    ok = state.get('last_ok')
    if ok:
        lines.append('Playlist: %s, %s kanálov' % (
            time.strftime('%d. %m. %Y %H:%M', time.localtime(float(ok))), state.get('channels', '?')))
    else:
        lines.append('Playlist: ešte nebol stiahnutý')
    if ok:
        removed = state.get('removed') or []
        if not dedupe_enabled():
            lines.append('Filter duplicít s O2 TV: vypnutý')
        elif removed:
            lines.append('Vynechané (má ich O2 TV): %s' % ', '.join(removed))
        elif state.get('filter_sig'):
            lines.append('Filter duplicít s O2 TV: žiadne duplicity')
        else:
            lines.append('Filter duplicít s O2 TV: čaká na export O2 TV')
    if not epg_local_enabled():
        lines.append('EPG: lokálna kópia vypnutá (číta ho IPTV Simple z URL)')
    elif streamlet.epg_valid(epg_path()):
        when = state.get('epg_ok')
        lines.append('EPG: %s, %.1f MB' % (
            time.strftime('%d. %m. %Y %H:%M', time.localtime(float(when))) if when else '?',
            float(state.get('epg_size', 0) or 0) / 1048576.0))
    else:
        lines.append('EPG: ešte nebolo stiahnuté')
    if state.get('epg_fail'):
        lines.append('Chyba EPG: %s' % state['epg_fail'])
    if state.get('last_fail'):
        lines.append('Posledná chyba: %s' % state['last_fail'])
    if not iptv_available():
        lines.append('IPTV Simple: nie je nainštalovaný alebo je vypnutý')
    else:
        st = streamlet.status(iptv_dir(), local_path(), translate)
        lines.append('Inštancia IPTV Simple: %s' % STATUS_LABELS.get(st, st))
    return lines


def run_dialog():
    dialog = xbmcgui.Dialog()
    lines = status_lines()
    if dialog.yesno(HEADING, '\n'.join(lines), nolabel='Zavrieť', yeslabel='Obnoviť teraz'):
        cycle(force=True)
        dialog.ok(HEADING, '\n'.join(status_lines()))
