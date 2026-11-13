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

import nba

ADDON_ID = 'service.bigtime.nba'
TAG = '[%s] ' % ADDON_ID
HEADING = 'Bigtime NBA'
PVR_IPTV = 'pvr.iptvsimple'

DEFAULT_HOURS = 6
RETRY_AFTER = 1800           # s po zlyhanom sťahovaní playlistu
EPG_RETRY_AFTER = 3 * 3600   # s po odmietnutí od servera (open-epg.com počíta aj neúspešné pokusy)
EPG_NET_RETRY_AFTER = 600    # s po chybe siete
DEFAULT_EPG_HOURS = 12

STATUS_LABELS = {
    nba.NASTAVENY: 'nastavená',
    nba.TREBA: 'treba vytvoriť',
}


def log(msg, level=xbmc.LOGINFO):
    xbmc.log(TAG + msg, level)


def _setting(key, default):
    try:
        value = xbmcaddon.Addon(ADDON_ID).getSetting(key).strip()
    except Exception:
        value = ''
    return value or default


def _hours(key, default):
    try:
        hours = int(_setting(key, str(default)))
    except ValueError:
        hours = default
    return max(1, min(hours, 168)) * 3600


def playlist_url():
    return _setting('playlist_url', nba.DEFAULT_PLAYLIST_URL)


def epg_url():
    return _setting('epg_url', nba.DEFAULT_EPG_URL)


def epg_local_enabled():
    return _setting('epg_local', 'true').lower() != 'false'


def check_enabled():
    return _setting('check_streams', 'true').lower() != 'false'


def chno_start():
    try:
        n = int(_setting('chno_start', str(nba.DEFAULT_CHNO_START)))
    except ValueError:
        n = nba.DEFAULT_CHNO_START
    return max(1, min(n, 9000))


def interval_seconds():
    return _hours('refresh_hours', DEFAULT_HOURS)


def epg_interval_seconds():
    return _hours('epg_hours', DEFAULT_EPG_HOURS)


# --- cesty ------------------------------------------------------------------

def data_dir():
    return xbmcvfs.translatePath('special://userdata/addon_data/%s' % ADDON_ID)


def local_path():
    return os.path.join(data_dir(), nba.LOCAL_NAME)


def raw_path():
    return os.path.join(data_dir(), nba.RAW_NAME)


def epg_path():
    return os.path.join(data_dir(), nba.EPG_NAME)


def local_special():
    return 'special://userdata/addon_data/%s/%s' % (ADDON_ID, nba.LOCAL_NAME)


def epg_special():
    return 'special://userdata/addon_data/%s/%s' % (ADDON_ID, nba.EPG_NAME)


def iptv_dir():
    return xbmcvfs.translatePath(nba.SETTINGS_DIR)


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
    """Treba EPG stiahnuť? Len keď je zapnuté lokálne EPG; po zlyhaní najskôr po
    EPG_RETRY_AFTER; inak keď súbor chýba alebo po intervale od posledného úspechu."""
    if not epg_local_enabled():
        return False
    now = time.time() if now is None else now
    wait = float(state.get('epg_wait', EPG_RETRY_AFTER) or EPG_RETRY_AFTER)
    if state.get('epg_fail') and now - float(state.get('epg_try', 0) or 0) < wait:
        return False
    if not nba.epg_valid(epg_path()):
        return True
    return now - float(state.get('epg_ok', 0) or 0) >= epg_interval_seconds()


def refresh_epg(state):
    """Stiahne EPG do addon_data. Vráti True pri úspechu."""
    now = time.time()
    state['epg_try'] = now
    try:
        size = nba.download_epg(epg_url(), epg_path())
    except Exception as exc:
        state['epg_fail'] = '%s' % exc
        answered = isinstance(exc, (nba.EpgError, urllib.error.HTTPError))
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
    if not epg_local_enabled() or not nba.epg_valid(epg_path()):
        return
    if not iptv_available() or not os.path.isfile(local_path()):
        return
    changed = nba.migrate_epg(iptv_dir(), local_path(), epg_url(), epg_special(), translate)
    if changed:
        log('inštancia %s číta EPG z lokálneho súboru' % os.path.basename(changed))
        if not state.get('epg_migrated'):
            toast('EPG sa bude čítať z lokálneho súboru, reštartuj Kodi')
        state['epg_migrated'] = time.time()


def refresh_playlist(state, abort=lambda: False, opener=None, check_opener=None):
    """Stiahne americký playlist, vyberie NBA kanály, overí ich a zapíše lokálny
    playlist. Vráti True pri úspechu. Stav sa mení v mieste; pri chybe ostáva starý
    súbor."""
    now = time.time()
    state['last_try'] = now
    try:
        kwargs = {'opener': opener} if opener else {}
        chosen = nba.candidates(nba.download(playlist_url(), **kwargs))
        nba.write_atomic(raw_path(), nba.serialize(chosen))
        dropped = []
        if check_enabled():
            ck = {'opener': check_opener} if check_opener else {}
            results = nba.check_all(chosen, abort=abort, **ck)
            ok = sum(1 for r in results.values() if r[0])
            if results and len(results) == len(chosen) and ok < nba.MIN_CHANNELS:
                raise nba.PlaylistError(
                    'overenie prešlo iba %d z %d kanálov (nejde sieť?), nechávam starý playlist'
                    % (ok, len(chosen)))
            kept, fails, dropped = nba.keep_working(chosen, results, state.get('fails'))
            state['fails'] = fails
        else:
            kept = chosen
            state['fails'] = {}
        if len(kept) < nba.MIN_CHANNELS:
            raise nba.PlaylistError('po overení ostalo %d kanálov' % len(kept))
        written = nba.write_atomic(local_path(), nba.render(kept, chno_start()))
    except Exception as exc:
        state['last_fail'] = '%s' % exc
        log('obnova playlistu zlyhala: %r' % exc, xbmc.LOGWARNING)
        return False
    state.pop('last_fail', None)
    state['last_ok'] = now
    state['candidates'] = len(chosen)
    state['channels'] = len(kept)
    state['dropped'] = dropped
    state['chno'] = chno_start()
    log('playlist: kandidátov %d, v playliste %d, vynechaných %d, %s'
        % (len(chosen), len(kept), len(dropped), 'zapísaný' if written else 'bez zmeny'))
    return True


def apply_numbering(state):
    """Zmenilo sa prvé číslo kanála: lokálny playlist sa znova očísluje z uloženého
    bez sťahovania a overovania."""
    if state.get('chno') == chno_start() or not os.path.isfile(local_path()):
        state['chno'] = chno_start()
        return
    text = nba.read_text(local_path())
    if text:
        items = nba.parse(text)
        nba.write_atomic(local_path(), nba.render(items, chno_start()))
        log('kanály znova očíslované od %d' % chno_start())
    state['chno'] = chno_start()


def ensure_instance(state):
    """Vytvorí inštanciu IPTV Simple, ak chýba. Vráti stav (nba.*) alebo None."""
    if not iptv_available() or not os.path.isfile(local_path()):
        return None
    st = nba.status(iptv_dir(), local_path(), translate)
    if st == nba.TREBA:
        use_file = epg_local_enabled() and nba.epg_valid(epg_path())
        nba.create_instance(iptv_dir(), local_special(), epg_url(),
                            epg_path=epg_special() if use_file else None)
        state['instance_created'] = time.time()
        log('vytvorená inštancia IPTV Simple')
        toast('NBA pridané do IPTV Simple, reštartuj Kodi')
    return st


def cycle(force=False, abort=lambda: False):
    """Jedno kolo služby. Vráti stav."""
    state = load_state()
    try:
        if force or is_due(state):
            refresh_playlist(state, abort)
        else:
            apply_numbering(state)
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


def _epg_line(state):
    if not epg_local_enabled():
        return 'EPG: lokálna kópia vypnutá (číta ho IPTV Simple z URL)'
    if not nba.epg_valid(epg_path()):
        return 'EPG: ešte nebolo stiahnuté'
    when = state.get('epg_ok')
    line = 'EPG: %s, %.1f MB' % (
        time.strftime('%d. %m. %Y %H:%M', time.localtime(float(when))) if when else '?',
        float(state.get('epg_size', 0) or 0) / 1048576.0)
    text = nba.read_text(local_path())
    cover = nba.epg_coverage(text, nba.epg_channel_ids(epg_path())) if text else None
    if cover:
        line += ', program má %d z %d kanálov' % cover
    return line


def status_lines():
    state = load_state()
    lines = []
    ok = state.get('last_ok')
    if ok:
        lines.append('Playlist: %s, %s kanálov v skupine %s' % (
            time.strftime('%d. %m. %Y %H:%M', time.localtime(float(ok))),
            state.get('channels', '?'), nba.GROUP))
        dropped = state.get('dropped') or []
        if dropped:
            lines.append('Vynechané (nefungujú): %s' % ', '.join(dropped))
        if not check_enabled():
            lines.append('Overovanie streamov: vypnuté')
    else:
        lines.append('Playlist: ešte nebol vytvorený')
    lines.append(_epg_line(state))
    if state.get('epg_fail'):
        lines.append('Chyba EPG: %s' % state['epg_fail'])
    if state.get('last_fail'):
        lines.append('Posledná chyba: %s' % state['last_fail'])
    if not iptv_available():
        lines.append('IPTV Simple: nie je nainštalovaný alebo je vypnutý')
    else:
        st = nba.status(iptv_dir(), local_path(), translate)
        lines.append('Inštancia IPTV Simple: %s' % STATUS_LABELS.get(st, st))
    return lines


def run_dialog():
    dialog = xbmcgui.Dialog()
    lines = status_lines()
    if dialog.yesno(HEADING, '\n'.join(lines), nolabel='Zavrieť', yeslabel='Obnoviť teraz'):
        cycle(force=True)
        dialog.ok(HEADING, '\n'.join(status_lines()))
