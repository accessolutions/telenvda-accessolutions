# Instructions du projet TeleNVDA (Client)

## Règle absolue : pas d'exécutable `.exe` dans le module

- **Ne jamais inclure de fichier `.exe` dans l'add-on TeleNVDA.**
- Toute fonctionnalité doit être implémentée en **Python pur**, en s'appuyant sur :
  - les API déjà fournies par NVDA (`gui`, `windowUtils`, `winUser`, `addonHandler`, `inputCore`, etc.) ;
  - `ctypes` pour les appels Win32 ;
  - les exécutables déjà présents dans NVDA ou dans Windows (par exemple `nvda.exe`, `nvda_slave.exe`, `powershell.exe`).
- Si un `.exe` est **réellement indispensable** et qu'aucune solution Python n'existe :
  - il doit s'agir d'un exécutable **officiel, signé numériquement par l'éditeur du programme** ;
  - la signature doit être vérifiable (éditeur, horodatage) ;
  - l'origine et la raison de l'inclusion doivent être documentées dans le dépôt ;
  - un exécutable recompilé localement, non signé, ou d'origine inconnue est **interdit**.
- En cas de doute : **on se passe du `.exe`**. L'absence d'exécutable prime sur la fonctionnalité.

Cette règle ne concerne **que les `.exe`**. Les bibliothèques natives (`.dll`) restent autorisées
lorsqu'elles sont nécessaires (par exemple la DLL Opus pour l'audio).

### Pourquoi

- Les exécutables non signés déclenchent des alertes antivirus et SmartScreen chez les utilisateurs.
- Ils compliquent l'audit du code et la revue par la communauté NVDA.
- Ils cassent la portabilité (NVDA portable, scratchpad) et alourdissent le paquet `.nvda-addon`.

### Cas connu : `url_handler.exe` (retiré)

`addon/globalPlugins/remoteClient/url_handler.exe` et sa source `url_handler.cpp` ont été supprimés
du dépôt : ils faisaient doublon avec le gestionnaire de protocole d'URL fourni par NVDA.
Les protocoles `nvdaremote:` et `telenvda:` sont désormais enregistrés vers
`nvda_slave.exe handleRemoteURL`, l'exécutable signé de NVDA, et l'add-on détourne en Python le
pointeur `_nvdaControllerInternal_handleRemoteURL` de `nvdaHelperLocal.dll` pour recevoir l'URL
(voir `url_handler.py`). Ce binaire ne doit pas être réintroduit.

## Divers

- Toute exclusion de fichier à la compilation se déclare dans `excludedFiles` de `buildVars.py` ;
  ce n'est pas une justification pour garder un `.exe` dans le dépôt.
