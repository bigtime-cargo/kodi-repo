# service.bigtime.nba

Služba: každých `refresh_hours` (6) stiahne `https://repo.streamlet.info/playlists/us.m3u` (~3000 kanálov, projekt iptv-org), vyberie NBA a športové kanály, **každý stream overí** a zapíše `addon_data/service.bigtime.nba/nba-us.m3u`. Všetky kanály majú `group-title="NBA"` a `tvg-chno` od 201 (O2 TV 1-99, Streamlet CZ od 101). Inštancia IPTV Simple „NBA (Bigtime)“ číta ten lokálny súbor a lokálnu kópiu EPG `nba-epg.xml` (zdroj `https://www.open-epg.com/files/unitedstates1.xml`).

## Súbory
- `nba.py` – bez Kodi: výber kanálov, oprava `tvg-id`, overenie streamov, skupina + číslovanie, EPG, inštancie IPTV Simple.
- `nba_core.py` – Kodi časť (nastavenia, state.json, toast, jedno kolo `cycle`); `service.py` slučka (30 s po štarte, kontrola každých 300 s); `default.py` dialóg „Obnoviť teraz“.
- `tools/test_nba.py` – offline testy so stubmi Kodi: `python3 -W ignore -m unittest tools.test_nba -v` (z adresára doplnku). `tools/` sa nezabalí.
- Štruktúra a pravidlá vychádzajú z `service.bigtime.streamlet` (kód EPG a inštancií je jeho kópia; oprava v jednom = skontrolovať aj v druhom).

## Výber kanálov
- Podľa názvu: `INCLUDE` (NBA, ESPN*, Fox Sports / FS1 / FS2, TNT, NBC Sports, CBS Sports), `EXCLUDE` (Novelas, News, ESPNews, Golazo, Deportes, Series). Pozor: `\bespn` bez koncovej hranice, aby sa chytilo aj `ESPNU` a `ESPN8`.
- Záznamy s vlastnými hlavičkami (`#EXTVLCOPT`, `#KODIPROP`) sa vynechajú (nedajú sa férovo overiť).
- Duplicity názvu (bez zátvoriek `(1080p)`, `[Geo-blocked]`) – ostane prvý záznam.
- `tvg-id` sa čistí od `@…` (`NBATV.us@SD` → `NBATV.us`) a mapuje podľa `EPG_ALIASES` (`FoxSports1.us` → `FS1.us`, lebo open-epg.com ho volá `FS1.us`). Zhoda s EPG je len pre americké ID: ESPN/ESPN2/ESPNU/NBATV/FS1 (13. 11. 2026: 5 z 22 kanálov); Latin America a NBC Sports NOW EPG nemajú. Na ich doplnenie treba ďalší zdroj EPG a mapovanie v `EPG_ALIASES`.

## Overenie streamov
- `check_stream`: manifest HLS (aj master → media playlist) a prvých 2000 B posledného segmentu (musí to byť binárny obsah, nie HTML). Vlastný `User-Agent` doplnku, **žiadne predstieranie prehliadača ani VLC** (pozri nižšie).
- `check_all` – 8 vlákien, 12 s na požiadavku, preruší sa pri `abort` (Kodi sa zatvára).
- **Hysteréza (1.0.1):** len kanál, ktorý už raz prešiel a je v playliste (`known_urls`: adresy z lokálneho playlistu bez tých, čo majú v `state.json: fails` počet > 0), dostane jedno zlyhanie zadarmo; vypadne až pri `MAX_FAILS` (2) po sebe. Kanál, ktorý ešte nikdy neprešiel, sa pri zlyhaní vynechá hneď. (V 1.0.0 dostal toleranciu aj nový kanál, takže prvý playlist na telke obsahoval 9 mŕtvych z 31 kandidátov.) Po úspechu sa počítadlo zmaže, vyradený kanál sa vráti, keď zase funguje. Prerušené overenie (Kodi sa zatvára) playlist nemení. `ALGO` v `nba_core.py` zvýšiť pri zmene výberu/overenia: playlist sa potom obnoví hneď, nie až po `refresh_hours`.
- **Poistky, pri ktorých ostane starý playlist:** zdroj nie je `#EXTM3U` alebo má < 200 kanálov, nenašiel sa žiaden športový kanál, overenie prešlo pri menej než 3 kanáloch (nejde sieť), po overení ostali < 3 kanály. Po zlyhaní nový pokus najskôr o 30 min.
- Overenie beží zo zariadenia, na ktorom je Kodi, takže geoblokovanie sa posudzuje z jeho IP (z Slovenska 13. 11. 2026: 31 kandidátov, 22 funguje; `[Geo-blocked]` kanály vypadnú samy).

## EPG
- Rovnako ako v streamlete: `epg_local` (zapnuté), `epg_hours` (12), po odmietnutí od servera pauza 3 h (open-epg.com má limit 20 stiahnutí denne na IP), po chybe siete 10 min. Naša inštancia sa prepne z URL na lokálny súbor (`migrate_epg`), prejaví sa po reštarte Kodi.
- Dialóg skriptu ukazuje „program má X z Y kanálov“ (`epg_coverage`).

## Pravidlá
- Cudzie ani ručné inštancie IPTV Simple sa nemenia. Nová vzniká len keď žiadna nečíta náš súbor. Žiadne `Addons.SetAddonEnabled` (SIGSEGV, pozri service.bigtime.setup).
- Čítanie `tvg-chno`: IPTV Simple ho berie, kým inštancia nemá zapnuté `numberByOrder`. Zmena `chno_start` prečísluje lokálny playlist bez sťahovania (`state.json: chno`).
- Streamy v americkom playliste sú neoficiálne prevysielania (IP adresy, súkromné servery) a krehké; môžu zmiznúť kedykoľvek. Právne čisté zdroje (NBA League Pass) tento doplnok nerieši.
- Build berie len súbory sledované gitom: nové súbory treba `git add`. V `build.sh` je v `SOURCES`.
