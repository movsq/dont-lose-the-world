# dont-lose-the-world

Sync savů Toca Boca World (a později dalších her) mezi tabletem a PC (BlueStacks) přes adb.
Nic se nikdy nepřepíše bez zálohy.

## Použití

Poprvé na novém PC **`setup.bat`** (viz níž). Pak dvojklik na **`sync.bat`**, nebo:

```
python tsync.py doctor      # zkontroluje, že je vše připravené (nic nemění)
python tsync.py devices     # připojená zařízení
python tsync.py sync        # zálohuje obě zařízení a navrhne směr
python tsync.py status      # jen ukáže, co by se dělo
python tsync.py backup      # jen zálohuje připojená zařízení
python tsync.py log         # historie záloh
python tsync.py restore tablet 1a2b3c4d   # vrátí zálohu na zařízení
python tsync.py push tablet pc            # ruční směr
python tsync.py export 1a2b3c4d slozka    # vybalí zálohu do složky
python tsync.py verify      # kontrola integrity úložiště
```

## Jak to funguje

1. **Záloha vždycky první.** Každé připojené zařízení se nejdřív stáhne do `store/toca.git`
   a záloha se zpětně přečte a porovná (sha256) se soubory na zařízení.
2. **Žádný milion savů.** Úložiště je git: soubory, které se nezměnily, se neukládají znovu,
   a když se nezměnilo nic, nový snapshot se vůbec nevytvoří.
3. **Návrh směru.** Porovnává se se stavem po posledním syncu:
   - změnil se jen tablet → navrhne tablet → PC (a obráceně),
   - změnilo se obojí → konflikt, ukáže které lokace a rozhodneš ty,
   - zdroj vypadá jako resetovaná/čerstvá hra (chybí lokace, výrazně menší) → musíš napsat `ANO`.
4. **Bezpečný zápis.** Soubory se nahrají do `.tsync/` na zařízení a ověří. Teprve pak se save
   přepíše (přímo na zařízení, během zlomku sekundy) a znovu se ověří. Když cokoli nesedí,
   save se automaticky vrátí ze zálohy.
5. **Hra musí být zavřená.** Když běží, tsync nabídne, že ji uloží (Home) a zavře.

6. **Kontrola verze hry.** Save z novější verze hry se do starší nenahraje bez potvrzení.

Synchronizuje se **všechno ve `files/` kromě výjimek** v `apps/toca.json`. Save totiž není jen
`active_state` (lokace): patří k němu i `playerprefs` (čítače ID, odemčené scény), `home_designer`,
dárky, vlastní oblečky, sběratelské předměty atd. Když nová verze hry přidá další složku, přenese se taky.
- `exclude` – cache, které si hra stáhne sama (`il2cpp`, `contenttestfolder`, `DefaultSaveFiles`…)
- `backup_only` – věci vázané na zařízení (`accounts`, `transaction-log.txt`, `playerprefs/unity.*`…):
  zálohují se, ale nikdy nekopírují.

Zařízení s `"write_protected": true` v `devices.json` se jen zálohuje, nikdy se na něj nezapisuje.
Zařízení s `"ignore": true` se úplně přeskakuje (třeba telefon připojený na nabíjení).

**Zařízení se poznávají podle Android ID**, ne podle adb jména (každý BlueStacks je `emulator-5554`).
Nové zařízení nabídne `sync` pojmenovat. První sync nové dvojice nic nenavrhuje, směr vybíráš ty.

## Přesun na jiné PC

1. Nainstaluj Tocu v BlueStacks z Play Store **s účtem, na kterém jsou nákupy**, a jednou ji spusť.
   Nákupy se ověřují proti účtu, ze kterého byla hra nainstalovaná.
2. Zkopíruj celou složku `dont-lose-the-world` (i se `store/`). Pak už používej jen tuhle kopii.
3. Připoj tablet (potvrď na něm „Povolit ladění USB“) a spusť **`setup.bat`**.
   Zkontroluje Python, Git, adb, nastavení BlueStacks, zařízení, hru a zálohy.
   Co chybí, nabídne doinstalovat (Python a Git přes winget, adb stáhne od Googlu).
   Když je vše v pořádku, nic nemění, takže ho jde pustit kdykoli znovu.
   Po instalaci Pythonu nebo Gitu zavři okno a spusť ho ještě jednou.
4. Spusť `sync.bat`: pojmenuj nový BlueStacks a při prvním syncu vyber **Tablet → nový PC**.

Kontrola bez `setup.bat`: `python tsync.py doctor`.

## Soubory

- `tsync.py` – celý nástroj (Python 3, bez knihoven navíc; potřebuje git a adb)
- `setup.bat` / `setup.ps1` – kontrola a doinstalování Pythonu, Gitu a adb
- `apps/*.json` – profily aplikací (balíček, co se synchronizuje, co se jen zálohuje).
  Po změně výjimek v profilu ukáže první další sync konflikt (mění se otisk savu) – vyber směr ručně.

Jen lokálně, nejsou v gitu (osobní data):
- `store/` – **zálohy, nemazat.** Obsahují i tokeny nákupů. Dá se celé zkopírovat jinam.
- `devices.json` – pojmenovaná zařízení a jejich ID; vytvoří se samo při prvním připojení
- `platform-tools/` – adb od Googlu; stáhne ho `setup.bat`

## Přidání další hry

Nový soubor `apps/<jméno>.json` podle `apps/toca.json` a pak `python tsync.py --app <jméno> sync`.
