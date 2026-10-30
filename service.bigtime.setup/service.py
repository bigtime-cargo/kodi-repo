# Bigtime nastavenie: po štarte Kodi (a potom pravidelne) stráži záplaty cudzích
# doplnkov a nastaví IPTV Simple pre export O2 TV. Žiadna sieť, žiadne nastavenia.
import xbmc

import bigtime_core as core

START_DELAY = 120       # s po štarte Kodi, kým sa doplnky usadia
TICK = 300              # s, ako často sa pozrieť na IPTV (lacná kontrola súborov)
PATCH_EVERY = 1800      # s, ako často prejsť manifest záplat


def run(monitor):
    """Hlavná slučka; vráti sa, keď Kodi ohlási koniec."""
    try:
        core.reset_pending()
    except Exception as exc:
        core.log('reset stavu zlyhal: %r' % exc, xbmc.LOGWARNING)
    if monitor.waitForAbort(START_DELAY):
        return
    since_patches = PATCH_EVERY     # prvé kolo hneď prejde aj záplaty
    while not monitor.abortRequested():
        due = since_patches >= PATCH_EVERY
        try:
            core.cycle(patches=due)
        except Exception as exc:
            core.log('kolo zlyhalo: %r' % exc, xbmc.LOGERROR)
        if due:
            since_patches = 0
        if monitor.waitForAbort(TICK):
            return
        since_patches += TICK


if __name__ == '__main__':
    core.log('služba beží')
    run(xbmc.Monitor())
