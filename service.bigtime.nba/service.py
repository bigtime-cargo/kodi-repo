# Bigtime NBA: pravidelne stiahne americký playlist streamletu, vyberie NBA a
# športové kanály, overí ich a postará sa o inštanciu IPTV Simple. Sieťový prístup:
# playlist, overenie streamov (manifest + začiatok segmentu) a EPG (XMLTV).
import xbmc

import nba_core as core

START_DELAY = 30        # s po štarte Kodi, kým sa usadí sieť
TICK = 300              # s, ako často sa pozrieť, či je čas na obnovu


def run(monitor):
    """Hlavná slučka; vráti sa, keď Kodi ohlási koniec."""
    if monitor.waitForAbort(START_DELAY):
        return
    while not monitor.abortRequested():
        try:
            core.cycle(abort=monitor.abortRequested)
        except Exception as exc:
            core.log('kolo zlyhalo: %r' % exc, xbmc.LOGERROR)
        if monitor.waitForAbort(TICK):
            return


if __name__ == '__main__':
    core.log('služba beží')
    run(xbmc.Monitor())
