# Spoločná logika služby (service.py) aj skriptu (default.py): kontrola a nasadenie
# záplat a nastavenie IPTV Simple. Všetky funkcie sú určené na volanie z hlavného
# vlákna doplnku; výnimky sa zachytávajú po jednotlivých položkách, aby jedna chyba
# nezhodila ostatné ani službu. Doplnok nerobí žiadne sieťové požiadavky.
import json
import os
import tempfile

import xbmc
import xbmcgui
import xbmcvfs

import iptv
import patcher

ADDON_ID = 'service.bigtime.setup'
TAG = '[%s] ' % ADDON_ID
HEADING = 'Bigtime nastavenie'
PVR_IPTV = 'pvr.iptvsimple'

# Stavy navyše oproti patcher.* (NEPRITOMNY / OK / CHYBA / INE / NASADENE)
ODLOZENE = 'ODLOZENE'       # CHYBA, ale práve niečo prehráva
VYNIMKA = 'VYNIMKA'         # pri položke nastala chyba
NEDOSTUPNY = 'NEDOSTUPNY'   # IPTV Simple nie je nainštalovaný alebo je vypnutý

LABELS = {
    patcher.OK: 'OK',
    patcher.CHYBA: 'CHÝBA (dá sa nasadiť)',
    patcher.INE: 'INÉ (doplnok sa zmenil)',
    patcher.NEPRITOMNY: 'nie je nainštalovaný',
    patcher.NASADENE: 'nasadená',
    ODLOZENE: 'odložená (prehráva sa)',
    VYNIMKA: 'chyba (pozri kodi.log)',
}
IPTV_LABELS = {
    iptv.NASTAVENY: 'nastavený',
    iptv.CAKA: 'čaká na export z o2tv (treba sa prihlásiť)',
    iptv.TREBA: 'treba nastaviť',
    NEDOSTUPNY: 'IPTV Simple nie je nainštalovaný alebo je vypnutý',
}


def log(msg, level=xbmc.LOGINFO):
    xbmc.log(TAG + msg, level)


# --- cesty (samostatné funkcie, aby sa dali v testoch podstrčiť) -------------

def addons_dir():
    return xbmcvfs.translatePath('special://home/addons')


def data_dir():
    return xbmcvfs.translatePath('special://userdata/addon_data/%s' % ADDON_ID)


def iptv_dir():
    return xbmcvfs.translatePath(iptv.SETTINGS_DIR)


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
        directory = data_dir()
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix='.state-', suffix='.tmp', dir=directory)
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


def reset_pending():
    """Po štarte Kodi sa `reload_needed` zahodí: Kodi už načítal nové súbory.
    Stav `warned` (INE) ostáva."""
    state = load_state()
    had = [state.pop(k, None) for k in ('reload_needed', 'restart_pending', 'iptv_refresh_pending')]
    if any(v for v in had):
        save_state(state)


def _reload_list(state):
    v = state.get('reload_needed')
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


# --- pomocné ----------------------------------------------------------------

def is_playing():
    try:
        return bool(xbmc.Player().isPlaying())
    except Exception:
        return True     # nevieme zistiť = radšej nezasahovať


def toast(message, warning=False):
    try:
        icon = xbmcgui.NOTIFICATION_WARNING if warning else xbmcgui.NOTIFICATION_INFO
        xbmcgui.Dialog().notification(HEADING, message, icon, 6000)
    except Exception as exc:
        log('toast zlyhal: %r' % exc, xbmc.LOGWARNING)


def _label(entry):
    return '%s %s (%s)' % (entry['addon_id'], entry['version'], entry['rel_path'].rsplit('/', 1)[-1])


def addon_enabled(addon_id):
    return bool(xbmc.getCondVisibility('System.AddonIsEnabled(%s)' % addon_id))


# --- záplaty ----------------------------------------------------------------

def patch_cycle():
    """Prejde manifest, nasadí chýbajúce záplaty (len keď nič neprehráva).
    Vráti zoznam (entry, stav)."""
    results = []
    try:
        entries = patcher.load_manifest()
    except Exception as exc:
        log('manifest sa nepodarilo načítať: %r' % exc, xbmc.LOGERROR)
        return results

    state = load_state()
    state_dirty = False
    warned = state.get('warned')
    if not isinstance(warned, list):
        warned = []
    deployed = []
    toasts = []
    ad = addons_dir()

    for entry in entries:
        try:
            st, digest = patcher.inspect(ad, entry)
            if st == patcher.CHYBA:
                if is_playing():
                    log('%s: záplata odložená, prehráva sa' % _label(entry))
                    results.append((entry, ODLOZENE))
                    continue
                st = patcher.apply(ad, entry)
                if st == patcher.NASADENE:
                    log('%s: záplata nasadená' % _label(entry))
                    if entry['addon_id'] not in deployed:
                        deployed.append(entry['addon_id'])
            elif st == patcher.INE:
                key = '%s|%s' % (entry['addon_id'], digest)
                if key not in warned:
                    msg = 'Záplata pre %s %s nesedí (nová verzia?)' % (entry['addon_id'], entry['version'])
                    log(msg + ' [' + entry['rel_path'] + ']', xbmc.LOGWARNING)
                    warned.append(key)
                    state_dirty = True
                    if msg not in toasts:
                        toasts.append(msg)
            results.append((entry, st))
        except Exception as exc:
            log('%s: chyba: %r' % (_label(entry), exc), xbmc.LOGERROR)
            results.append((entry, VYNIMKA))

    for msg in toasts:
        toast(msg, warning=True)

    # Doplnky sa nikdy nevypínajú ani nezapínajú (znovunačítanie natívneho modulu
    # zhodí Kodi, pozri CLAUDE.md): nový kód sa použije po reštarte Kodi.
    if deployed:
        state['reload_needed'] = sorted(set(_reload_list(state)) | set(deployed))
        state_dirty = True
        toast('Záplaty pre %s nasadené — reštartuj Kodi, aby sa použili.' % ', '.join(deployed),
              warning=True)
    if state_dirty:
        state['warned'] = warned
        save_state(state)
    return results


# --- IPTV Simple ------------------------------------------------------------

def iptv_available():
    return bool(xbmc.getCondVisibility('System.HasAddon(%s)' % PVR_IPTV)) and addon_enabled(PVR_IPTV)


def iptv_status():
    return iptv.status(iptv_dir(), translate(iptv.DEFAULT_PLAYLIST), translate)


def iptv_cycle():
    """Nastaví IPTV Simple, ak ešte nič nečíta playlist z o2tv. Vráti stav iptv.* / NEDOSTUPNY."""
    st = iptv_status()
    if st != iptv.TREBA:
        return st
    if not iptv_available():
        log('IPTV Simple nie je nainštalovaný alebo je vypnutý, nič nerobím')
        return NEDOSTUPNY

    path = iptv.create_instance(iptv_dir(), translate(iptv.DEFAULT_PLAYLIST), translate(iptv.DEFAULT_EPG))
    log('vytvorená inštancia IPTV Simple: %s' % os.path.basename(path))
    state = load_state()
    state['reload_needed'] = sorted(set(_reload_list(state)) | {PVR_IPTV})
    save_state(state)
    toast('IPTV Simple nastavený pre O2 TV — reštartuj Kodi.', warning=True)
    return iptv.NASTAVENY


def cycle(patches=True):
    """Jedno kolo služby; každá časť sa chráni samostatne."""
    if patches:
        try:
            patch_cycle()
        except Exception as exc:
            log('kolo záplat zlyhalo: %r' % exc, xbmc.LOGERROR)
    try:
        iptv_cycle()
    except Exception as exc:
        log('kolo IPTV zlyhalo: %r' % exc, xbmc.LOGERROR)


# --- dialóg skriptu ---------------------------------------------------------

def status_lines():
    """Textový prehľad (len čítanie, nič nemení)."""
    lines = []
    try:
        entries = patcher.load_manifest()
    except Exception as exc:
        entries = []
        lines.append('Manifest záplat sa nepodarilo načítať')
        log('manifest: %r' % exc, xbmc.LOGERROR)
    ad = addons_dir()
    reload_needed = _reload_list(load_state())
    for entry in entries:
        try:
            st = patcher.check(ad, entry)
        except Exception as exc:
            log('%s: chyba: %r' % (_label(entry), exc), xbmc.LOGERROR)
            st = VYNIMKA
        label = LABELS.get(st, st)
        if st == patcher.OK and entry['addon_id'] in reload_needed:
            label = 'OK (čaká na reštart Kodi)'
        lines.append('%s: %s' % (_label(entry), label))
    try:
        st = iptv_status()
        if st == iptv.TREBA and not iptv_available():
            st = NEDOSTUPNY
    except Exception as exc:
        log('IPTV: %r' % exc, xbmc.LOGERROR)
        st = VYNIMKA
    label = IPTV_LABELS.get(st, LABELS.get(st, st))
    if st == iptv.NASTAVENY and PVR_IPTV in reload_needed:
        label += ' (čaká na reštart Kodi)'
    lines.append('IPTV Simple (O2 TV): %s' % label)
    return lines


def run_dialog():
    dialog = xbmcgui.Dialog()
    again = dialog.yesno(HEADING, '\n'.join(status_lines()),
                         nolabel='Zavrieť', yeslabel='Skontrolovať a opraviť teraz')
    if not again:
        return
    if is_playing():
        dialog.ok(HEADING, 'Práve niečo prehráva, do doplnkov nezasahujem. '
                           'Skús to znova po skončení prehrávania.')
        return
    cycle()
    dialog.ok(HEADING, '\n'.join(status_lines()))
