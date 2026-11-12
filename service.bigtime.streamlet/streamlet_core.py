# Logika s Kodi pre službu (service.py) aj skript (default.py). Výnimky sa
# zachytávajú tu, aby chyba siete nikdy nezhodila službu.
import json
import os
import tempfile
import time

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
RETRY_AFTER = 1800           # s po zlyhanom sťahovaní

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


def refresh_playlist(state):
    """Stiahne playlist. Vráti True pri úspechu. Stav sa mení v mieste."""
    now = time.time()
    state['last_try'] = now
    try:
        n, changed, written = streamlet.refresh(playlist_url(), local_path())
    except Exception as exc:
        state['last_fail'] = '%s' % exc
        log('sťahovanie playlistu zlyhalo: %r' % exc, xbmc.LOGWARNING)
        return False
    state.pop('last_fail', None)
    state['last_ok'] = now
    state['channels'] = n
    log('playlist: %d kanálov, opravených ID %d, %s'
        % (n, changed, 'zapísaný' if written else 'bez zmeny'))
    return True


def ensure_instance(state):
    """Vytvorí inštanciu IPTV Simple, ak chýba. Vráti stav (streamlet.*) alebo None."""
    if not iptv_available() or not os.path.isfile(local_path()):
        return None
    st = streamlet.status(iptv_dir(), local_path(), translate)
    if st == streamlet.TREBA:
        streamlet.create_instance(iptv_dir(), local_special(), epg_url())
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
        ensure_instance(state)
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
