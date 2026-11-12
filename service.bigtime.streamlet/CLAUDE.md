# service.bigtime.streamlet

Služba: každých `refresh_hours` (12) stiahne `https://repo.streamlet.info/playlists/cz.m3u`, od `tvg-id` odstrihne príponu od `@` (`HBO.cz@SD` → `HBO.cz`) a uloží ju do `addon_data/service.bigtime.streamlet/streamlet-cz.m3u`. Inštancia IPTV Simple číta ten lokálny súbor a od 1.2.0 aj lokálnu kópiu EPG `streamlet-epg.xml` (zdroj `https://www.open-epg.com/files/czech.xml`).

**Prečo:** XMLTV z open-epg.com má `channel id="HBO.cz"`, playlist `HBO.cz@SD`; IPTV Simple porovnáva ID presne, takže bez opravy nemal EPG žiadny kanál (zistené 12. 11. 2026 na telke, log `EPG channels not found`). Po oprave sa zhoduje 67 z 93 riadkov; ~15 kanálov nemá EPG v žiadnom verejnom súbore (pozri `~/kontrola-TCL/diagnostika/2026-11-12-iptv-streamlet/`).

## Súbory
- `streamlet.py` – bez Kodi: sťahovanie, oprava, validácia, atómový zápis, inštancie IPTV Simple.
- `streamlet_core.py` – Kodi časť (nastavenia, state.json, toast, jedno kolo `cycle`); `service.py` slučka (90 s po štarte, kontrola každých 300 s); `default.py` dialóg „Obnoviť teraz“.
- `tools/test_streamlet.py` – offline testy so stubmi Kodi: `python3 -m unittest tools.test_streamlet -v`. `tools/` sa nezabalí.

## Pravidlá
- **Filter duplicít s O2 TV** (nastavenie `dedupe_o2`, predvolene zapnuté): z lokálneho playlistu sa vynechá kanál streamletu, ktorého názov už má O2 TV (zoznam z `addon_data/plugin.video.o2tv/playlist.m3u`, atribút `tvg-name`). O2 má archív (catchup) a streamlet je len živý. Názvy sa porovnávajú bez diakritiky, veľkosti písmen, interpunkcie a značiek v zátvorkách, poradie slov nevadí (`TV Barrandov` = `Barrandov TV`); `Prima Cool` a `Prima COOL SK` sú rôzne kanály a ostanú oba. Playlist s opravenými ID bez filtra sa ukladá ako `streamlet-raw.m3u`; keď sa zmení zoznam O2 (alebo nastavenie), lokálny playlist sa z neho prefiltruje bez sťahovania (`filter_sig` v state.json). Export O2 s menej ako 5 kanálmi sa ignoruje. Na telke vynecháva 10 kanálov (ČT 1, ČT 2, ČT24, Kino Barrandov, Barrandov Krimi, TV Barrandov, CNN Prima News, KVIFF TV, Prima love SK, Slager Original). Zmena sa v IPTV Simple prejaví po hodine (obnova playlistu) alebo reštarte Kodi.
- Sieťový prístup: sťahovanie playlistu (https/http, 30 s, max 5 MB) a od 1.2.0 aj EPG (XMLTV, gzip sa rozbalí, po častiach do dočasného súboru, max 300 MB po rozbalení).
- **EPG sťahuje doplnok sám** (nastavenia `epg_local`, `epg_hours` = 12): open-epg.com dovolí stiahnuť súbor 20-krát za deň na IP (13. 11. 2026 mobil dostal stránku „You reached the download limit“ → IPTV Simple `Invalid EPG XML: no <tv> tag found`) a IPTV Simple ho sťahuje pri každom štarte Kodi. Overenie: začiatok obsahuje `<tv`, koniec `</tv>`, ≥ 200 kB; inak sa zahodí a starý súbor ostane. Po zlyhaní nový pokus najskôr o 3 h (open-epg počíta aj neúspešné pokusy). Lokálna kópia sa bez limitu číta pri každom štarte.
- **Jediná výnimka z „inštancie sa nemenia“:** naša inštancia (číta `streamlet-cz.m3u`) s `epgPathType` 1 a `epgUrl` rovným nastavenému `epg_url` sa prepne na `epgPathType` 0 + `epgPath` = lokálna kópia, a to až keď je kópia na disku platná. Prejaví sa po reštarte Kodi (toast len raz). Nová inštancia vzniká rovno s lokálnym EPG, ak kópia už existuje.
- Zlý obsah (nie `#EXTM3U`, < 20 kanálov, < 50 % predošlého počtu) sa zahodí, starý súbor ostane. Po zlyhaní nový pokus najskôr o 30 min.
- Cudzie ani ručné inštancie sa nemenia. Nová vzniká len keď žiadna nečíta náš súbor ani `repo.streamlet.info` priamo (ručná inštancia sa len ohlási do logu). Ručná inštancia na telke bola 12. 11. 2026 zmazaná a doplnok vytvoril vlastnú.
- Žiadne `Addons.SetAddonEnabled` (SIGSEGV pri natívnych doplnkoch, pozri service.bigtime.setup). Novú inštanciu Kodi načíta až po reštarte.
- `epgUrl` v nastaveniach platí len pri vytvorení inštancie.
- Build berie len súbory sledované gitom: nové súbory treba `git add`. V `build.sh` doplniť do `SOURCES`.
