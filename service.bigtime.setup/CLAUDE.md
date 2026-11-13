# service.bigtime.setup

Jeden doplnok na každé zariadenie: (A) spojí playlisty a EPG z O2 TV, Magio GO a Streamletu do jedného zoznamu (`merge.py`) a nastaví pre neho jednu inštanciu IPTV Simple (`iptv.py`, `iptv_cycle()` v `bigtime_core.py`), (B) stráži a nasadzuje naše záplaty cudzích doplnkov (YouTube, SC helper). Žiadne HTTP požiadavky, len stdlib + Kodi API.

## Súbory
- `service.py` – slučka služby (120 s po štarte, IPTV každých 5 min, záplaty každých 30 min); `default.py` – dialóg v Programových doplnkoch.
- `bigtime_core.py` – logika s Kodi (JSON-RPC toggle, toasty, state.json v `addon_data/service.bigtime.setup/`).
- `patcher.py`, `iptv.py`, `merge.py` – bez Kodi, testovateľné.
- `patches/` – generované `tools/make_patches.py` (manifest.json + .patch); zabaľujú sa len rozdiely, nie celé cudzie súbory.
- `tools/` sa nezabalí do zipu (build.sh). Build berie len súbory sledované gitom: nové súbory treba `git add`.

## Nová záplata
1. Pridať adresár `<addon_id>-<verzia>/` do `~/kontrola-TCL/nastroje/zaplaty/` (opravený súbor + `<súbor>.orig`).
2. Doplniť dvojicu (addon, súbor) do `TARGETS` v `tools/make_patches.py` (cesta v doplnku, popis).
3. `python3 tools/make_patches.py`, potom testy, zvýšiť verziu v `addon.xml`, build.
Pri novej verzii cudzieho doplnku sa starý záznam nezmaže sám: súbor bude `INE` (toast raz), treba záplatu prerobiť.

## Poistky
- `check`: NEPRITOMNY / OK / CHYBA / INE; zapisuje sa len pri CHYBA (sha == orig_sha256), inak sa nič nemení.
- Prísny unified diff bez fuzz; výsledok musí mať `patched_sha256`, inak sa nezapíše nič. Atómový zápis, záloha `<súbor>.orig-bigtime`, zmazanie `__pycache__/<modul>.*.pyc`.
- `rel_path` bez `..`/absolútnych ciest, symlinky v ceste k cieľu sa odmietnu.
- Súbory sa nasadzujú len keď nič neprehráva (odložené sa dorobí v ďalšom kole). Doplnky sa nikdy nevypínajú ani nezapínajú; po nasadení príde jeden toast „reštartuj Kodi“ a doplnok sa zapíše do `reload_needed` v state.json (po štarte Kodi sa vyčistí).
- IPTV (1.2.0, spoločný zoznam): Kodi nespája rovnako pomenované skupiny z rôznych inštancií (Šport bol trikrát), preto `merge.py` spojí `plugin.video.o2tv/playlist.m3u`, `service.bigtime.streamlet/streamlet-cz.m3u` a `plugin.video.magiogo/playlist.m3u` (+ ich EPG) do `addon_data/service.bigtime.setup/bigtime.m3u` a `bigtime-epg.xml`; hlavička playlistu má `#BIGTIME-MERGED` (podľa nej Streamlet vie, že vlastnú inštanciu netreba). Spája sa len pri zmene zdroja (veľkosť+čas), každých ~5 min v slučke služby. Zdroje sa nemenia; riadky kanálov (skupiny, catchup, #EXTVLCOPT) sa kopírujú tak, ako ich doplnky vyrobili, mení sa len `tvg-chno` pri kolízii (prednosť O2 TV, Streamlet, Magio GO; kolízia dostane prvé voľné číslo od 1000). Kanál s `tvg-id` z predchádzajúceho zdroja sa vynechá (v rámci jedného zdroja sa duplicity nechávajú). EPG Streamletu (~17 MB) sa orezáva na kanály z playlistu (podľa id bez ohľadu na veľkosť písmen aj podľa display-name) a drží sa v cache `epg-streamlet.xml`.
- Režimy (`iptv.mode`): SPOJENE = inštancia číta spoločný playlist → súbory sa udržiavajú; SAMOSTATNE = niektorá inštancia číta zdroj samostatne (playlist so značkou o2tv/magiogo, alebo súbor Streamletu) → NEROBÍ SA NIČ (nič sa nespája, nič sa nevytvára; v dialógu hláška, že treba staré inštancie zmazať); ZIADNA = nič nečíta → keď niektorý zdroj už exportoval, spojí sa a vznikne inštancia „Bigtime TV“ (obnova 15 min — bez nej sa epg.xml Magio GO s guličkou archívu nenačíta). Cudzie inštancie (NBA...) sa nikdy nemenia ani nepočítajú. Závislosť na plugin.video.o2tv je `optional`.
- Migrácia existujúceho zariadenia: Kodi ukončiť, zálohovať a zmazať staré `instance-settings-N.xml` (O2 TV, Streamlet, Magio GO), spustiť Kodi; služba do ~2 min vytvorí „Bigtime TV“, potom reštart Kodi. Kanály sa v Kodi viažu na inštanciu, takže obľúbené a vlastné poradie sa stratia. Po zmazaní inštancií treba resetovať aj PVR databázy (`Database/TV*.db`, `Epg*.db`, Kodi zastavené), inak ostanú osirelé skupiny (Šport 3×). Estuary ukladá koláže log skupín (dlaždice „Skupiny TV programov“) do `Textures13.db` pod kľúčom `image://pvr@pvr://channels/tv/<skupina>@<id>//?ts=…`; názvy skupín sa po migrácii opakujú, preto ostanú staré logá. Pri zastavenom Kodi: `DELETE FROM texture WHERE url LIKE 'image://pvr@%'` a zmazať súbory `Thumbnails/<cachedurl>` (kanálové loga `image://pvrchannel_tv@…` ostávajú). Pozor: `adb shell` v cykle `while read` číta stdin — dať `</dev/null`.

## Prečo sa doplnky nereštartujú
Pôvodná verzia po nasadení záplat prepínala doplnky cez JSON-RPC `Addons.SetAddonEnabled` false/true. Živý test na Linux PC (Kodi 21.3, Python 3.14, 29. 10. 2026): reštart plugin.video.youtube prešiel, ale pri `service.sc.helper` Kodi spadol na SIGSEGV v dynamickom linkeri (dlsym `PyInit__core`) vo vlákne skriptu helpera. Helper nesie natívny modul `resources/lib/_sc.so` a jeho opätovné načítanie v tom istom procese Kodi neprežije; to isté hrozí pre natívne doplnky ako pvr.iptvsimple.
Pravidlo: žiadne prepínanie doplnkov, žiadne `executeJSONRPC` s `SetAddonEnabled` (test `ZakazPrepinania` to stráži). Nový kód sa použije po reštarte Kodi; doplnok len ukáže toast a stav „čaká na reštart Kodi“ v dialógu skriptu.

## Overené
`python3 -m unittest tools.test_bigtime -v` (offline testy so stubmi Kodi): aplikátor vs. naše záplaty bajt po bajte, odmietnutie zmeneného originálu, stavy, atómovosť, `..`/symlink, IPTV scenáre, logika služby. Nasadenie súborov overené živým testom na PC (hashe sedeli). Testy: `python3 -m unittest tools.test_bigtime tools.test_merge -v`. Zlučovač overený na reálnych súboroch z PC (156 kanálov = 34 + 83 + 39, unikátne čísla, 10 MB EPG, 0,3 s). Načítanie spoločnej inštancie v Kodi: pozri históriu migrácie PC/telka nižšie, ak je doplnená.
