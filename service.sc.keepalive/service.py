# Udrží session service.sc.helper pri dlhom prehrávaní Stream Cinema.
# Helper zahodí session 1800 s po poslednom HTTP requeste na /stream/<id>
# (SessionManager.get() ju pri každom requeste obnoví) a nepretržité
# prehrávanie je jeden request. Keď Kodi po pauze alebo timeoute potrebuje
# reconnect neskôr ako po 30 min, dostane 404 a epizóda spadne. Preto počas
# prehrávania z helpera každých 10 min pošleme na tú istú URL request na 1 bajt.
import re
import time
import urllib.error
import urllib.request

import xbmc

TAG = '[service.sc.keepalive] '
HELPER_URL = re.compile(r'^http://127\.0\.0\.1:\d+/stream/[^/?#]+')
CHECK_EVERY = 30        # s, ako často sa pozrieť, čo hrá
KEEPALIVE_EVERY = 600   # s, musí byť bezpečne pod 1800 s expirácie helpera
TIMEOUT = 15


def log(msg, level=xbmc.LOGINFO):
    xbmc.log(TAG + msg, level)


def playing_helper_url(player):
    try:
        if not player.isPlaying():
            return None
        m = HELPER_URL.match(player.getPlayingFile())
    except RuntimeError:
        # medzi isPlaying() a getPlayingFile() prehrávanie skončilo
        return None
    return m.group(0) if m else None


def touch(url):
    """Vráti HTTP status, alebo None pri chybe spojenia."""
    req = urllib.request.Request(url, headers={'Range': 'bytes=0-0'})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            resp.read(1)
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        log('request zlyhal: {0}'.format(e), xbmc.LOGWARNING)
        return None


def main():
    monitor = xbmc.Monitor()
    player = xbmc.Player()
    current = None
    last = 0.0
    log('štart')
    while not monitor.waitForAbort(CHECK_EVERY):
        url = playing_helper_url(player)
        if url != current:
            # nové prehrávanie: jeho prvý request práve obnovil session
            current, last = url, time.time()
            if url:
                log('sledujem {0}'.format(url))
            continue
        if url and time.time() - last >= KEEPALIVE_EVERY:
            last = time.time()
            status = touch(url)
            if status in (200, 206):
                log('session obnovená ({0})'.format(status))
            else:
                log('session sa obnoviť nepodarilo ({0})'.format(status), xbmc.LOGWARNING)
    log('koniec')


main()
