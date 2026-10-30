# service.bigtime.setup

Jeden doplnok na každé zariadenie: (A) nastaví IPTV Simple pre export O2 TV, (B) stráži a nasadzuje naše záplaty cudzích doplnkov (YouTube, SC helper). Žiadne HTTP požiadavky, len stdlib + Kodi API.

## Súbory
- `service.py` – slučka služby (120 s po štarte, IPTV každých 5 min, záplaty každých 30 min); `default.py` – dialóg v Programových doplnkoch.
- `bigtime_core.py` – logika s Kodi (JSON-RPC toggle, toasty, state.json v `addon_data/service.bigtime.setup/`).
- `patcher.py`, `iptv.py` – bez Kodi, testovateľné.
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
- IPTV: ak niektorá inštancia číta playlist s `plugin://plugin.video.o2tv/`, nemení sa nič; nová inštancia až keď o2tv playlist exportoval; cudzie inštancie sa nikdy nemenia.

## Prečo sa doplnky nereštartujú
Pôvodná verzia po nasadení záplat prepínala doplnky cez JSON-RPC `Addons.SetAddonEnabled` false/true. Živý test na Linux PC (Kodi 21.3, Python 3.14, 29. 10. 2026): reštart plugin.video.youtube prešiel, ale pri `service.sc.helper` Kodi spadol na SIGSEGV v dynamickom linkeri (dlsym `PyInit__core`) vo vlákne skriptu helpera. Helper nesie natívny modul `resources/lib/_sc.so` a jeho opätovné načítanie v tom istom procese Kodi neprežije; to isté hrozí pre natívne doplnky ako pvr.iptvsimple.
Pravidlo: žiadne prepínanie doplnkov, žiadne `executeJSONRPC` s `SetAddonEnabled` (test `ZakazPrepinania` to stráži). Nový kód sa použije po reštarte Kodi; doplnok len ukáže toast a stav „čaká na reštart Kodi“ v dialógu skriptu.

## Overené
`python3 -m unittest tools.test_bigtime -v` (offline testy so stubmi Kodi): aplikátor vs. naše záplaty bajt po bajte, odmietnutie zmeneného originálu, stavy, atómovosť, `..`/symlink, IPTV scenáre, logika služby. Nasadenie súborov overené živým testom na PC (hashe sedeli). Neoverené: načítanie novej inštancie IPTV Simple po reštarte Kodi.
