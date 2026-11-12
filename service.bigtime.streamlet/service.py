# Bigtime Streamlet: pravidelne stiahne playlist streamletu, opraví tvg-id a
# postará sa o inštanciu IPTV Simple. Jediný sieťový prístup je sťahovanie
# playlistu a EPG (XMLTV).
import xbmc

import streamlet_core as core

START_DELAY = 20        # s po štarte Kodi, kým sa usadí sieť (zlyhanie siete sa skúsi znova o 10 min)
TICK = 300              # s, ako často sa pozrieť, či je čas na obnovu


def run(monitor):
    """Hlavná slučka; vráti sa, keď Kodi ohlási koniec."""
    if monitor.waitForAbort(START_DELAY):
        return
    while not monitor.abortRequested():
        try:
            core.cycle()
        except Exception as exc:
            core.log('kolo zlyhalo: %r' % exc, xbmc.LOGERROR)
        if monitor.waitForAbort(TICK):
            return


if __name__ == '__main__':
    core.log('služba beží')
    run(xbmc.Monitor())
