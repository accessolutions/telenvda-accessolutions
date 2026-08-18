# Rapport d’audit de compatibilité NVDA

**Extension auditée :** TeleNVDA Accessolutions  
**Version examinée :** 2026.08.18.1146  
**Version NVDA principalement analysée :** 2026.1  
**Type d’audit :** analyse statique et comparaison avec les API NVDA  
**Date :** 18 août 2026  

## 1. Verdict exécutif

L’extension TeleNVDA Accessolutions est **probablement fonctionnelle avec NVDA 2026.1 dans son chemin principal**, mais elle n’est pas exclusivement basée sur les API publiques et stables de NVDA.

Verdict précis :

- **NVDA 2026.1 : compatible en pratique, confiance moyenne à bonne.**
- **Aucune incompatibilité critique avec une API publique actuelle n’a été démontrée.**
- **Risque de maintenance élevé**, en raison de plusieurs accès privés ou internes.
- **Compatibilité annoncée avec NVDA 2019.3 non crédible pour l’état actuel du code.**
- Aucun test n’a été effectué avec NVDA réellement lancé, ni sur plusieurs versions.

Le manifeste annonce NVDA 2019.3 à 2026.1 dans [addon/manifest.ini](addon/manifest.ini#L9-L10), alors que le projet exige Python 3.13 dans [pyproject.toml](pyproject.toml#L12). Cette incohérence est renforcée par l’utilisation de syntaxe Python moderne dans du code importé au démarrage.

## 2. Incompatibilité historique démontrée

La compatibilité NVDA 2019.3–2022.x ne peut pas être retenue pour la version actuelle du code.

Le `GlobalPlugin` importe notamment les modules de transport et de mise à jour au chargement dans [addon/globalPlugins/remoteClient/__init__.py](addon/globalPlugins/remoteClient/__init__.py#L139-L142).

Or :

- [addon/globalPlugins/remoteClient/transport.py](addon/globalPlugins/remoteClient/transport.py#L28) utilise `list[str]`.
- [addon/globalPlugins/remoteClient/lib32/websocket/_url.py](addon/globalPlugins/remoteClient/lib32/websocket/_url.py#L102-L104) utilise `Optional[list[str]]` et l’opérateur walrus `:=`.
- Le walrus n’est pas compris par Python 3.7.
- Les annotations génériques natives comme `list[str]` ne sont pas compatibles avec les anciennes versions de Python utilisées par les anciennes versions de NVDA.
- Le module WebSocket concerné est importé par la chaîne de transport.

Cela peut donc empêcher le chargement du plugin avant même l’instanciation de `GlobalPlugin`.

### Classification historique

| Version NVDA | Évaluation statique |
|---|---|
| 2019.3–2022.4 | Incompatible ou très probablement non chargeable avec l’état actuel du code |
| 2023.x–2024.x | Compatibilité plausible, mais non certifiée |
| 2025.x | Compatibilité plausible et branches API prévues |
| 2026.1 | Compatibilité pratique probable |

La valeur `minimumNVDAVersion = 2019.3.0` devrait donc être revue ou le code devrait être rétrocompatible avec les versions Python correspondantes.

## 3. Analyse des API utilisées

### API publiques ou extension points modernes

| Fichier | Symbole ou mécanisme | Évaluation |
|---|---|---|
| [addon/globalPlugins/remoteClient/__init__.py](addon/globalPlugins/remoteClient/__init__.py#L225-L239) | `post_secureDesktopStateChange`, `inputCore.decide_handleRawKey` | API actuelles et adaptées à NVDA moderne |
| [addon/globalPlugins/remoteClient/nvda_patcher.py](addon/globalPlugins/remoteClient/nvda_patcher.py#L17-L32) | `braille.displayChanged`, `braille.displaySizeChanged` | Extension points modernes |
| [addon/globalPlugins/remoteClient/nvda_patcher.py](addon/globalPlugins/remoteClient/nvda_patcher.py#L95-L106) | `braille.pre_writeCells` | Extension point moderne |
| [addon/globalPlugins/remoteClient/nvda_patcher.py](addon/globalPlugins/remoteClient/nvda_patcher.py#L215-L232) | `inputCore.decide_executeGesture` | Extension point moderne |
| [addon/globalPlugins/remoteClient/local_machine.py](addon/globalPlugins/remoteClient/local_machine.py#L62-L69) | `braille.decide_enabled` | API moderne appropriée |
| [addon/globalPlugins/remoteClient/session.py](addon/globalPlugins/remoteClient/session.py#L122-L124) | Filtres braille selon la version | Bonne stratégie de transition |
| [addon/globalPlugins/remoteClient/__init__.py](addon/globalPlugins/remoteClient/__init__.py#L1757-L1759) | `security.post_sessionLockStateChanged` | API actuelle à partir de NVDA 2023 |

Ces usages sont cohérents avec l’évolution de NVDA et constituent la partie la plus robuste du plugin.

### API dépréciées

| Fichier | Symbole | Évaluation |
|---|---|---|
| [addon/globalPlugins/remoteClient/__init__.py](addon/globalPlugins/remoteClient/__init__.py#L1177-L1179) | `security.postSessionLockStateChanged` | Ancienne API conservée pour NVDA 2022.4 |
| [addon/globalPlugins/remoteClient/session.py](addon/globalPlugins/remoteClient/session.py#L122-L124) | `braille.filter_displaySize` | Ancienne API utilisée avant NVDA 2025 |
| [addon/globalPlugins/remoteClient/__init__.py](addon/globalPlugins/remoteClient/__init__.py#L1050) | `_popupSettingsDialog` | Fallback privé et déprécié |
| [addon/globalPlugins/remoteClient/__init__.py](addon/globalPlugins/remoteClient/__init__.py#L1050) | `popupSettingsDialog` | Chemin moderne privilégié |

La présence de fallbacks est utile, mais elle augmente la complexité et doit être testée sur chaque famille de versions.

## 4. API privées ou internes

### Risque élevé

Les éléments suivants sont privés ou internes, même s’ils existent encore dans NVDA 2026.1 :

- `speech._manager.speak`
- `speech._manager.cancel`
- `speech.speech._speechState.beenCanceled`
- `braille.handler._writeCells`
- `braille.handler._cursorBlinkTimer`
- `braille.handler._messageCallLater`
- `braille.handler.buffer`
- `inputCore.manager.executeGesture`

Ils sont utilisés notamment dans :

- [addon/globalPlugins/remoteClient/nvda_patcher.py](addon/globalPlugins/remoteClient/nvda_patcher.py#L70-L75)
- [addon/globalPlugins/remoteClient/nvda_patcher.py](addon/globalPlugins/remoteClient/nvda_patcher.py#L99-L104)
- [addon/globalPlugins/remoteClient/local_machine.py](addon/globalPlugins/remoteClient/local_machine.py#L38-L55)
- [addon/globalPlugins/remoteClient/local_machine.py](addon/globalPlugins/remoteClient/local_machine.py#L88-L116)
- [addon/globalPlugins/remoteClient/__init__.py](addon/globalPlugins/remoteClient/__init__.py#L1530-L1547)

Ces accès ne constituent pas une incompatibilité immédiate : le Remote Access natif de NVDA utilise lui aussi certains de ces objets internes. En revanche, ils peuvent casser lors d’un changement interne de NVDA, sans avertissement de compatibilité API.

### Risque moyen

Les éléments suivants existent actuellement mais restent moins stables :

- `scriptHandler._makeKbEmulateScript`
- `vision.handler.getActiveProviderInstances`
- `gui.settingsDialogs.SettingsPanel`
- `windowUtils.CustomWindow`

Ils sont utilisés dans :

- [addon/globalPlugins/remoteClient/input.py](addon/globalPlugins/remoteClient/input.py#L127-L147)
- [addon/globalPlugins/remoteClient/dialogs.py](addon/globalPlugins/remoteClient/dialogs.py#L7)
- [addon/globalPlugins/remoteClient/url_handler.py](addon/globalPlugins/remoteClient/url_handler.py#L41)

## 5. Bureau sécurisé et verrouillage de session

### Bureau sécurisé

Le traitement est globalement bien conçu :

1. Le plugin tente d’utiliser `post_secureDesktopStateChange`.
2. Il conserve un fallback par événement `gainFocus`.
3. Le test de `IAccessibleHandler.SecureDesktopNVDAObject` est protégé par `hasattr`.
4. Le serveur local temporaire est créé seulement lors de l’entrée dans le bureau sécurisé.

Le code concerné se trouve dans [addon/globalPlugins/remoteClient/__init__.py](addon/globalPlugins/remoteClient/__init__.py#L1550-L1611).

Nuance importante :

- Dans les anciennes versions de NVDA, `SecureDesktopNVDAObject` était fourni par `IAccessibleHandler`.
- Dans NVDA 2026.1, la gestion a évolué vers `winAPI.secureDesktop`.
- Le `hasattr` évite une incompatibilité fatale si l’ancien symbole n’existe plus.
- Le chemin moderne dépend toutefois du fonctionnement réel de `post_secureDesktopStateChange`.

Conclusion : **aucune incompatibilité certaine n’est démontrée**, mais l’entrée et la sortie d’UAC, de l’écran de connexion et du bureau sécurisé doivent impérativement être testées.

### Verrouillage de session

La transition est correcte :

- `postSessionLockStateChanged` pour NVDA 2022.4.
- `post_sessionLockStateChanged` pour NVDA 2023 et versions ultérieures.

Le code désactive aussi le contrôle distant lorsque la session est verrouillée. Ce comportement doit être validé avec des touches maintenues et des connexions maître/esclave actives.

## 6. Threads, GUI et callbacks

L’architecture réseau utilise de nombreux threads, mais la plupart des appels GUI sont correctement différés avec :

- `wx.CallAfter`
- `wx.CallLater`
- `queueHandler.queueFunction`

Les exemples sont répartis dans [addon/globalPlugins/remoteClient/callback_manager.py](addon/globalPlugins/remoteClient/callback_manager.py#L26-L31), [addon/globalPlugins/remoteClient/local_machine.py](addon/globalPlugins/remoteClient/local_machine.py#L88-L176) et [addon/globalPlugins/remoteClient/__init__.py](addon/globalPlugins/remoteClient/__init__.py#L1479-L1503).

Évaluation :

- **Bonne séparation générale entre réseau et GUI.**
- Les dialogues et messages utilisateur sont majoritairement renvoyés vers le thread principal.
- Les hooks clavier et souris utilisent leur propre boucle Windows.
- Le code démarre cependant des serveurs et modifie certains états NVDA directement depuis des callbacks qui devront être vérifiés en situation réelle.
- Les monkey-patches doivent être testés lors du rechargement et de la terminaison du plugin.

Aucune erreur évidente de thread n’a été démontrée statiquement, mais cette partie ne peut pas être certifiée sans exécution réelle de NVDA.

## 7. Erreur logique probable dans le support Vision

Une erreur indépendante des API NVDA a été identifiée dans [addon/globalPlugins/remoteClient/input.py](addon/globalPlugins/remoteClient/input.py#L147-L151).

Dans la boucle parcourant les fournisseurs Vision, le script est recherché sur `app` au lieu de `provider`.

Conséquence probable :

- Les scripts provenant d’un `VisionEnhancementProvider` peuvent ne pas être retrouvés.
- Le chemin fonctionne pour les Global Plugins, App Modules, Tree Interceptors et objets NVDA, mais pas nécessairement pour Vision.

Cette anomalie devrait être corrigée séparément et couverte par un test de geste Vision.

## 8. Matrice fonctionnelle à tester

| Fonctionnalité | Niveau de priorité |
|---|---:|
| Chargement du plugin au démarrage | Critique |
| Connexion maître/esclave | Critique |
| Contrôle clavier et libération des modificateurs | Critique |
| Parole distante : parler, annuler, pause, sourdine | Élevée |
| Braille distant et changement de taille | Élevée |
| Gestes braille et scripts d’application | Élevée |
| Scripts Vision | Élevée |
| Déconnexion pendant une touche maintenue | Élevée |
| Verrouillage et déverrouillage de session | Critique |
| UAC et bureau sécurisé | Critique |
| Écran de connexion Windows | Critique |
| Connexion locale du pont sécurisé | Élevée |
| Ouverture des paramètres depuis un callback réseau | Moyenne |
| Transfert de fichiers avec interface graphique | Moyenne |
| Partage d’écran et contrôle souris | Moyenne |
| Terminaison puis rechargement du plugin | Élevée |
| Exécution avec NVDA 2023, 2024, 2025 et 2026 | Critique |

## 9. Recommandations

### Priorité critique

1. Corriger l’incohérence entre [addon/manifest.ini](addon/manifest.ini#L9-L10) et [pyproject.toml](pyproject.toml#L12).
2. Décider officiellement si le support commence à NVDA 2023 ou à NVDA 2026.
3. Si le support 2019.3 est conservé, supprimer ou isoler toute syntaxe Python moderne des modules importés au démarrage.
4. Tester l’entrée et la sortie du bureau sécurisé avec NVDA 2026.1.
5. Ajouter une matrice de tests réellement exécutés sur chaque version supportée.

### Priorité élevée

1. Remplacer progressivement les monkey-patches privés par des extension points NVDA lorsqu’ils existent.
2. Isoler les accès à `speech._manager`, `speech.speech._speechState` et `braille.handler`.
3. Ajouter des vérifications explicites de présence des attributs privés avec journalisation claire.
4. Corriger la recherche des scripts Vision sur `provider`.
5. Vérifier que chaque callback enregistré est systématiquement désenregistré lors de la terminaison.

### Priorité moyenne

1. Remplacer le fallback `_popupSettingsDialog` par une compatibilité limitée aux versions qui le nécessitent.
2. Documenter les API privées volontairement utilisées et la version NVDA de référence.
3. Ajouter des tests de non-régression pour les changements de taille d’afficheur braille.
4. Vérifier les erreurs de signature des extension points modernes selon chaque version NVDA.

## Conclusion

Pour **NVDA 2026.1**, le `GlobalPlugin` de TeleNVDA est techniquement cohérent et devrait fonctionner dans son scénario principal. Les extension points modernes sont correctement utilisés et les principaux changements d’API entre NVDA 2022, 2023, 2025 et 2026 sont pris en compte.

Cependant, le plugin dépend encore fortement d’éléments privés de NVDA. Il faut donc le qualifier ainsi :

> Compatible en pratique avec NVDA 2026.1, mais non garanti par les seules API publiques et présentant un risque de maintenance élevé.

La compatibilité historique jusqu’à NVDA 2019.3 n’est pas confirmée et est probablement fausse pour l’état actuel du code, notamment à cause de la syntaxe Python moderne utilisée dans les modules importés.

Aucun fichier du code source n’a été modifié pendant cet audit.
