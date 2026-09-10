# Plan d'amélioration du son distant avec Opus

Date de préparation : 10/09/2026

## Mode d'emploi de ce document

Ce document est volontairement prescriptif. Il s'adresse à un développeur qui
connaît Python mais pas nécessairement WASAPI, Opus, le protocole TeleNVDA ou les
particularités des scripts NVDA. Lorsqu'un choix technique est déjà tranché ici,
il ne faut pas le remplacer pendant l'implémentation sans mesure ou test qui
démontre la nécessité de le faire.

La règle principale est : **une seule couche modifiée à la fois, puis ses tests**.
Il ne faut jamais modifier simultanément la capture, le codec, le protocole et la
lecture avant de lancer les vérifications. Chaque phase ci-dessous doit pouvoir
être vérifiée séparément avant de poursuivre.

## 1. Décision proposée

Utiliser **Opus** pour le son distant, au moyen de la bibliothèque officielle
`libopus` chargée directement depuis Python avec `ctypes`.

La solution retenue respecte les contraintes suivantes :

- aucun navigateur, Edge ou moteur WebRTC ;
- aucun programme auxiliaire ni fichier `.exe` ajouté pour l'audio ;
- deux DLL `libopus` embarquées, une x86 et une x64, sans processus séparé ;
- aucune modification obligatoire du relais TeleNVDA ;
- transport dans les messages chiffrés TCP/TLS ou WebSocket existants ;
- activation du son distant uniquement lorsque les deux extrémités ont validé
  l'implémentation Opus.

Le codec seul ne suffira pas. La mauvaise qualité actuelle vient principalement
de transformations effectuées avant l'envoi :

1. la stéréo 48 kHz est réduite à du mono 16 kHz par simple décimation, sans
   filtre anti-repliement ;
2. le PCM 16 bits est ensuite réduit avec un codage très destructeur.

Le nouveau chemin doit donc conserver le PCM 48 kHz stéréo depuis WASAPI jusqu'à
l'encodeur Opus. **La conversion obligatoire en 16 kHz mono est supprimée.**
Remplacer seulement `compress()` et `expand()` conserverait une grande partie de
la dégradation actuelle.

Il ne faut pas confondre fréquence et profondeur : le nouveau flux reste en PCM
signé **16 bits**, format directement accepté par `opus_encode()` et
`nvwave.WavePlayer`, mais il est en **48 000 Hz et deux canaux**. Le défaut retiré
est donc « 16 kHz mono », pas « PCM 16 bits ».

### Choix figés pour la première version

| Sujet | Choix à appliquer | Ne pas faire |
|---|---|---|
| Format Opus | PCM 16 bits, 48 kHz, stéréo | convertir en 16 kHz mono avant Opus |
| Taille de trame | 20 ms, soit 960 échantillons par canal | réutiliser les messages audio de 200 ms |
| Profil | `OPUS_APPLICATION_AUDIO`, signal `OPUS_AUTO` | détecter soi-même voix ou musique |
| Débit initial | VBR, 96 kbit/s | commencer directement à 510 kbit/s |
| Transport | messages TeleNVDA existants | ajouter WebRTC, UDP, Edge ou un serveur audio |
| Binaire | DLL officielle x86 et x64 via `ctypes` | lancer un encodeur `.exe` |
| Compatibilité | négociation Opus entre les deux extrémités | démarrer sans paramètres confirmés |
| Interface | conserver les commandes et dialogues actuels | redessiner l'interface pendant le changement de codec |
| Pertes | numéro de séquence et PLC | activer FEC/DRED avant d'en mesurer le besoin |

## 2. Objectif de qualité

Le profil par défaut doit convenir automatiquement à un mélange de voix, de sons
système, de vidéos et de musique :

- entrée et sortie : PCM signé 16 bits, 48 kHz, stéréo entrelacée ;
- trames Opus : 20 ms ;
- application Opus : `OPUS_APPLICATION_AUDIO` ;
- type de signal : `OPUS_AUTO` ;
- VBR activé ;
- débit nominal proposé : 96 kbit/s stéréo ;
- adaptation prudente entre 48 et 128 kbit/s selon l'encombrement de la file
  d'envoi, avec hystérésis pour éviter les changements permanents ;
- complexité initiale : 6, à confirmer par mesure CPU dans NVDA x86 et x64 ;
- DTX désactivé au départ afin de ne pas couper les sons faibles ou très courts ;
- suppression conservée uniquement pour les blocs PCM réellement silencieux.

Opus sait lui-même adapter sa bande passante et son usage de la stéréo au débit
disponible. Il ne faut donc pas écrire un détecteur maison « voix ou musique ».
Le mode automatique désigne ici l'adaptation native d'Opus et l'ajustement du
débit selon la congestion, pas une classification fragile des applications.

## 3. Architecture cible

```text
Poste contrôlé
  WASAPI loopback par application, 48 kHz stéréo
        -> mélange PCM 48 kHz stéréo
        -> encodeur libopus, trames de 20 ms
        -> 2 trames groupées par message, soit un envoi toutes les 40 ms
        -> Base64 et message remote_audio_opus_data
        -> chiffrement AES-GCM existant
        -> TCP/TLS ou WebSocket
        -> relais TeleNVDA, sans décodage

Poste contrôleur
  message remote_audio_opus_data
        -> remise en ordre et contrôle de séquence
        -> décodeur libopus
        -> petit tampon adaptatif
        -> nvwave.WavePlayer en 48 kHz stéréo
```

Le regroupement de deux paquets de 20 ms réduit le coût JSON et AES-GCM sans
imposer les blocs de 200 ms actuels. Le message doit contenir une liste structurée
de paquets Base64 plutôt qu'un assemblage binaire difficile à valider.

## 4. Intégration native sans exécutable

Créer un module `audio_opus.py` qui charge une DLL par chemin absolu avec
`ctypes.CDLL`. L'architecture doit être déterminée avec la taille réelle d'un
pointeur du processus, et non seulement avec le numéro de version de NVDA.

Disposition proposée :

```text
addon/globalPlugins/remoteClient/native/x86/opus.dll
addon/globalPlugins/remoteClient/native/x64/opus.dll
addon/globalPlugins/remoteClient/native/opus-LICENSE.txt
```

Le wrapper doit rester petit et n'exposer que les opérations nécessaires :

- création, configuration et destruction d'un encodeur ;
- encodage de PCM 16 bits ;
- création, réinitialisation et destruction d'un décodeur ;
- décodage en PCM 16 bits ;
- réglage dynamique du débit ;
- réglage du gain de décodage pour appliquer le volume distant ;
- traduction explicite des codes d'erreur Opus en exceptions Python.

La DLL doit provenir du code source officiel de `libopus`, actuellement en
version stable 1.6.1, avec version et SHA-256 épinglés. La CI doit construire les
variantes Windows x86 et x64 de façon reproductible avant `scons`. Aucun outil de
compilation ni exécutable de démonstration ne doit être placé dans l'add-on.

La licence BSD à trois clauses de `libopus` autorise la redistribution binaire,
mais son texte et ses mentions doivent accompagner les DLL.

## 5. Évolution du protocole

Ajouter une capacité distincte, par exemple `remote_audio_opus_v1`. Elle ne doit
être annoncée que si la DLL correspondant au processus se charge et si un test
d'initialisation encodeur-décodeur réussit.

La négociation proposée est la suivante :

1. les deux clients annoncent `remote_audio_opus_v1` ;
2. le contrôleur demande une session Opus seulement si le poste contrôlé annonce
  cette capacité ;
3. le poste contrôlé refuse la demande si Opus ne peut pas être initialisé ;
4. la réponse confirme toujours le codec, la fréquence, le nombre de canaux et
  la durée des trames réellement acceptés.

Utiliser le type de données `remote_audio_opus_data` pour les paquets audio.
Exemple de charge utile :

```json
{
  "type": "remote_audio_opus_data",
  "target": 12,
  "stream": 1,
  "sequence": 2048,
  "frame_ms": 20,
  "packets": ["...base64...", "...base64..."]
}
```

Contraintes de validation à imposer avant tout décodage :

- identifiant de flux attendu ;
- séquence entière dans une plage valide ;
- une ou deux trames par message dans la première version ;
- taille Base64 et taille décodée strictement bornées ;
- durée, fréquence et canaux égaux aux paramètres négociés ;
- rejet sans exception propagée de tout paquet Opus invalide.

Le numéro de séquence permet de détecter un bloc abandonné localement lorsque la
file d'envoi est saturée. Le décodeur peut alors produire une trame de dissimulation
de perte Opus, plutôt que de créer un trou brutal.

## 6. Capture et mélange

Le chemin WASAPI existant est conservé : il sait déjà capturer séparément les
arbres de processus sous Windows 10 version 2004 et versions ultérieures.

Modifier le mélangeur pour produire une seule sortie `PCM48_STEREO`, qui conserve
les trames capturées jusqu'à l'encodeur Opus.

Procéder dans cet ordre :

1. ajouter une constante de format décrivant 48 kHz, 2 canaux et 16 bits ;
2. faire produire au mélangeur une sortie stéréo native ;
3. prouver par test que les échantillons gauche et droit restent distincts ;
4. connecter cette sortie au nouvel encodeur Opus ;
5. supprimer la réduction 16 kHz mono du chemin audio distant.

Le mélange doit additionner les canaux gauche et droit séparément dans des
accumulateurs plus larges, puis borner le résultat en 16 bits. Une amélioration
ultérieure par limiteur doux ne doit être envisagée qu'après des mesures montrant
un écrêtage audible ; elle ne doit pas retarder la première version Opus.

L'exclusion de NVDA et des applications refusées reste inchangée. Sur les anciens
Windows qui imposent la capture de toute la carte son, Opus améliore le codage mais
ne peut pas empêcher la voix de NVDA d'être incluse.

## 7. Encodage et régulation du débit

Créer une abstraction de codec explicite plutôt que des conditions dispersées :

```text
RemoteAudioEncoder
  - OpusEncoder, 48 kHz stéréo

RemoteAudioDecoder
  - OpusDecoder
```

Le débit Opus doit être piloté par niveaux stables :

| État du transport | Débit cible initial | Action |
|---|---:|---|
| stable | 96 kbit/s | profil automatique normal |
| durablement vide et liaison rapide | 128 kbit/s | meilleure musique/stéréo |
| congestion modérée | 64 kbit/s | préserver la continuité |
| congestion forte | 48 kbit/s | réduire avant d'abandonner des trames |

Les seuils exacts doivent être déterminés par mesure. Un changement ne doit être
appliqué qu'après plusieurs observations consécutives et un délai minimal entre
deux changements. Si la file continue de croître au niveau minimal, les trames les
plus anciennes sont abandonnées comme aujourd'hui.

L'usage de FEC, DRED ou redondance n'est pas recommandé dans la première version.
Le transport est fiable et ordonné par TCP ; ces mécanismes augmenteraient le débit
sans corriger le blocage en tête de ligne. La dissimulation de perte du décodeur
suffit pour les blocs volontairement abandonnés par TeleNVDA.

## 8. Lecture et latence

Adapter `audio_playback.Player` pour ouvrir `nvwave.WavePlayer` en 48 kHz stéréo
lorsque le codec négocié est Opus.

Remplacer le simple comptage de messages par un tampon fondé sur la durée :

- préchargement initial visé : 80 à 120 ms ;
- cible adaptative maximale en fonctionnement normal : 200 ms ;
- plafond absolu proposé : 500 ms ;
- suppression des données les plus anciennes au-delà du plafond ;
- remise à zéro du décodeur et du tampon à chaque nouvel identifiant de flux.

Le volume peut être appliqué par le réglage de gain du décodeur Opus, ce qui évite
une boucle Python sur chaque échantillon. À 0 %, le lecteur doit produire du silence
ou ne pas alimenter la sortie, sans détruire la synchronisation du décodeur.

Objectif initial mesurable : moins de 300 ms entre la capture et la lecture sur un
réseau local stable, au lieu du regroupement et du préchargement beaucoup plus longs
du chemin actuel.

## 9. Fichiers à créer ou modifier

### Nouveaux fichiers

- `addon/globalPlugins/remoteClient/audio_opus.py` : wrapper `ctypes` et classes
  encodeur/décodeur ;
- `addon/globalPlugins/remoteClient/native/x86/opus.dll` ;
- `addon/globalPlugins/remoteClient/native/x64/opus.dll` ;
- `addon/globalPlugins/remoteClient/native/opus-LICENSE.txt` ;
- tests unitaires du wrapper, du protocole et du tampon ;
- script de construction reproductible de `libopus` pour le développement local.

Créer également un dossier `tests` à la racine. Aucun test automatisé audio
n'existe actuellement dans le dépôt ; il faut donc installer une infrastructure
minimale avant de modifier le comportement.

### Fichiers existants

- `audio_capture.py` : sortie 48 kHz stéréo sans réduction intermédiaire ;
- `audio_share.py` : négociation, choix du codec, paquets Opus et adaptation du
  débit ;
- `audio_playback.py` : format négocié, tampon temporel et gestion des pertes ;
- `capabilities.py` : capacité `remote_audio_opus_v1` conditionnelle ;
- `configuration.py` et `dialogs.py` : ne pas les modifier pour le codec dans la
  première version. Le profil automatique est le comportement par défaut. Ajouter
  une option avancée seulement si les essais réels démontrent qu'elle est utile ;
- `protocol.md` : messages, bornes et règles de négociation Opus ;
- `readme.md` et documentation française : qualité et consommation réseau ;
- `buildVars.py` : exclusions éventuelles et mentions de licence ;
- `.github/workflows/build_addon.yml` : construction vérifiée des deux DLL avant
  l'empaquetage.

Le constructeur actuel ajoute récursivement tous les fichiers du dossier `addon`.
Les DLL placées sous `native` seront donc incluses sans modifier le format du paquet.

## 10. Phases d'implémentation

Chaque phase doit être livrée dans un commit distinct. Le développeur doit lancer
les tests indiqués avant de commencer la suivante. Si une phase échoue, corriger
seulement les fichiers de cette phase ; ne pas avancer en espérant que la suite
résoudra le problème.

### Phase 0 - Mesure de référence

- enregistrer plusieurs extraits PCM avant conversion : voix, musique, vidéo,
  notification brève et silence ;
- mesurer le débit, la latence, la taille de file et le temps CPU actuels ;
- conserver les mêmes extraits pour comparer le chemin actuel et Opus ;
- reproduire précisément le défaut signalé avant toute modification.

Livrable : tableau de référence et fichiers audio de test non distribués dans
l'add-on.

### Phase 1 - Preuve de faisabilité `libopus`

- produire les DLL x86 et x64 depuis une version et un SHA-256 épinglés ;
- écrire le wrapper minimal `ctypes` ;
- effectuer un aller-retour PCM -> Opus -> PCM sans NVDA ;
- vérifier création/destruction répétée, erreurs, stéréo et trames de 20 ms ;
- mesurer le temps CPU sur les deux architectures.

Critère de passage : aucun plantage, durée décodée exacte et coût CPU compatible
avec NVDA pendant une synthèse vocale active.

### Phase 2 - Chemin local 48 kHz stéréo

- faire sortir le mélangeur en PCM 48 kHz stéréo ;
- vérifier explicitement qu'aucun appel à `downmix()` n'existe sur le chemin Opus ;
- brancher Opus localement sans réseau ;
- lire le résultat avec `nvwave.WavePlayer` ;
- comparer à l'écoute avec le chemin actuel sur les mêmes extraits.

Critère de passage : amélioration nette sur musique et sons aigus, sans régression
sur la compréhension de la parole ni interruption de NVDA.

### Phase 3 - Protocole négocié

- ajouter la capacité conditionnelle et le nouveau type de données ;
- ajouter la négociation Opus et les erreurs explicites d'indisponibilité ;
- ajouter identifiant de flux, séquence et limites de taille ;
- tester les capacités présentes, absentes et invalides entre contrôleur et contrôlé.

Critère de passage : une session démarre uniquement après confirmation des
paramètres Opus par les deux extrémités.

Avant de passer à la phase 4, effectuer aussi un double appui sur
`NVDA+Contrôle+Maj+K` depuis les deux postes. Le dialogue des sources doit encore
s'ouvrir sans démarrer ni arrêter accidentellement le son.

### Phase 4 - Tampon et adaptation automatique

- introduire le tampon temporel borné ;
- simuler retard, rafales, congestion et blocs abandonnés ;
- activer les paliers de débit avec hystérésis ;
- journaliser codec, débit, profondeur du tampon, sous-alimentations et abandons,
  sans journaliser les données audio.

Critère de passage : pas de croissance infinie de mémoire, pas de son ancien joué
après une congestion et reprise rapide après une coupure courte.

### Phase 5 - Validation réelle et déploiement

- tester NVDA x86 et x64 sur les versions Windows réellement supportées ;
- tester TLS brut et WebSocket sur 443 ;
- tester une liaison locale, une bonne liaison Internet et une liaison limitée ;
- vérifier Firefox, Chrome, lecteurs multimédias, sons Windows et synthèses tierces ;
- faire une comparaison d'écoute à l'aveugle entre le chemin actuel et Opus ;
- publier d'abord sur un canal de test avec journaux détaillés.

## 22. État de réalisation de la phase 3

La phase 3 est implémentée dans les fichiers suivants :

- `audio_protocol.py` centralise la sélection d'Opus, la confirmation des paramètres,
  les bornes des paquets et le suivi des séquences ;
- `capabilities.py` annonce `remote_audio_opus_v1` uniquement lorsque libopus est
  réellement initialisable ;
- `audio_share.py` exige la capacité des deux extrémités, refuse les requêtes
  invalides ou indisponibles, confirme le format et associe chaque session à un
  identifiant de flux ;
- `tests/test_audio_protocol.py` couvre les capacités présentes et absentes, le
  codec inconnu, les paramètres invalides, les limites de paquets et les séquences.

Le type `remote_audio_opus_data` est enregistré et validé, mais l'émission et le
décodage des trames Opus restent volontairement pour l'étape suivante de branchement
des données. Le chemin audio historique est conservé pendant cette étape.

Avant la phase 4, il faut valider manuellement le double appui sur
`NVDA+Contrôle+Maj+K` depuis les deux postes et vérifier que le dialogue des sources
s'ouvre sans démarrer ni arrêter accidentellement le son. La phase suivante pourra
alors brancher les paquets validés sur le tampon temporel sans modifier la
négociation.

## 11. Matrice de tests minimale

| Domaine | Vérification |
|---|---|
| DLL | bon fichier x86/x64, chargement par chemin absolu, licence présente |
| Codec | mono/stéréo, silence, maximum, trame invalide, création/destruction |
| Qualité | voix, musique, transitoires, fréquences aiguës, faible volume |
| Réseau | débit stable, rafales, file saturée, reconnexion, arrêt en cours de trame |
| Protocole | négociation Opus, refus propre, message malformé |
| Lecture | périphérique NVDA choisi, volume 0 à 100 %, changement de périphérique |
| Capture | plusieurs applications, navigateur multiprocessus, exclusion immédiate |
| Robustesse | DLL absente ou corrompue, encodeur en erreur, décodeur en erreur |
| Accessibilité | annonces non répétitives, dialogue de consentement et options utilisables |
| Raccourci | appui simple différé, double appui, contrôle local/distant, déconnexion |
| Sources | sélection, annulation, ajout manuel, persistance correcte selon le rôle |

## 12. Critères d'acceptation

La migration est considérée réussie lorsque :

- la préférence d'écoute à l'aveugle va clairement à Opus sur la musique, les
  vidéos et les sons système ;
- la voix reste au moins aussi intelligible que dans le chemin actuel ;
- le débit moyen, Base64 et chiffrement compris, reste inférieur ou proche du
  débit actuel d'environ 170 kbit/s ;
- la latence locale mesurée reste sous 300 ms dans le profil automatique ;
- NVDA ne présente ni blocage de parole ni hausse CPU gênante ;
- une DLL manquante désactive proprement le son distant et explique la cause ;
- aucun navigateur ni fichier `.exe` audio n'est nécessaire ou livré.

## 13. Risques et décisions à ne pas différer

- **Compatibilité x86/x64** : tester les deux DLL dès la preuve de faisabilité,
  avant de modifier le protocole.
- **Blocage TCP** : Opus réduit le débit mais ne supprime pas le blocage en tête de
  ligne. Un transport UDP/WebRTC serait un autre projet et contredirait la contrainte
  actuelle ; il n'est pas nécessaire pour cette amélioration.
- **Charge CPU** : commencer à une complexité modérée et mesurer pendant que NVDA
  parle, plutôt que choisir la complexité maximale par principe.
- **Qualité du mélange** : préserver la stéréo avant de juger Opus. Une comparaison
  faite après la décimation mono 16 kHz donnerait une conclusion trompeuse.
- **Sécurité native** : construire depuis la source officielle, épingler le hash,
  charger par chemin absolu et borner toutes les données reçues avant l'appel C.
- **Licence** : distribuer les mentions BSD de la version exacte compilée.

## 14. Solution écartée

Une implémentation Opus en Python pur n'est pas retenue : elle serait complexe,
difficile à auditer et trop coûteuse dans le processus NVDA. Les interfaces Opus
de navigateur ou WebRTC sont également écartées, tout comme un encodeur lancé en
sous-processus. Une DLL officielle appelée dans le processus NVDA est le compromis
le plus simple, performant et maintenable sous les contraintes choisies.

## 15. Références techniques

- Documentation officielle de l'encodeur :
  <https://opus-codec.org/docs/opus_api-1.5/group__opus__encoder.html>
- Documentation officielle du décodeur :
  <https://opus-codec.org/docs/opus_api-1.5/group__opus__decoder.html>
- Présentation et caractéristiques d'Opus : <https://opus-codec.org/>
- Licence et conditions de redistribution : <https://opus-codec.org/license/>

## 16. Comportements existants à préserver absolument

L'amélioration du codec ne doit changer aucun des comportements suivants.

### Appui simple sur `NVDA+Contrôle+Maj+K`

- Le raccourci réel déclaré est `kb:control+shift+NVDA+k`.
- Si la touche NVDA est Insertion, l'utilisateur tape
  `Insert+Contrôle+Maj+K`.
- Le premier appui ne démarre ou n'arrête pas immédiatement le son : il crée un
  `wx.CallLater` et attend de savoir si un deuxième appui arrive.
- Le délai vient de `keyboard.multiPressTimeout`, exprimé en millisecondes, avec
  100 ms de marge.
- À expiration, `_toggle_remote_audio()` choisit la session master connectée ou,
  à défaut, la session slave connectée.
- Un contrôleur peut demander ou arrêter le son. Un poste contrôlé peut arrêter
  une session qu'il a acceptée mais ne peut pas en démarrer une.

### Double appui rapide

- Le deuxième appui est reconnu parce que `remote_audio_timer` existe encore.
- Il annule ce minuteur avant d'ouvrir la liste des applications.
- Il ne doit jamais appeler `_toggle_remote_audio()`.
- Ne pas utiliser `getLastScriptRepeatCount()` : lorsque le contrôle clavier est
  distant, les scripts présents dans `guestScripts` sont appelés directement et
  le compteur standard de NVDA n'est pas incrémenté.
- `script_toggle_remote_audio` doit rester dans `guestScripts`, sinon le
  raccourci partirait vers le poste contrôlé au lieu d'être traité localement.

### Dialogue des sources pendant le contrôle distant

- Si `sending_keys` est vrai, `_open_audio_sources()` rend d'abord le contrôle au
  poste local avec libération des touches maintenues.
- Le dialogue est ensuite ouvert localement.
- Sa fermeture doit rendre le contrôle distant seulement si le master est encore
  connecté et si un poste contrôlé est encore disponible.
- Cette restauration doit rester dans un bloc `finally` afin de fonctionner après
  validation, annulation ou exception.

### Choix et mémorisation des applications

- Une case cochée signifie que l'application est entendue.
- Une case décochée signifie qu'elle est exclue.
- Les applications actuellement entendues et les exclusions enregistrées sont
  réunies dans la liste.
- Une exclusion enregistrée reste visible même si l'application ne tourne plus.
- Le bouton d'ajout accepte un nom d'exécutable, le normalise en minuscules et
  évite les doublons.
- Sur le poste qui écoute, les exclusions validées sont enregistrées dans la
  configuration et envoyées à la session active.
- Sur le poste qui partage son son, le choix ne vaut que pour la session active et
  ne doit jamais écraser sa configuration personnelle.
- Le bouton Annuler, la touche Échap et la croix ne doivent modifier ni la
  configuration ni la session.
- Une modification pendant la session doit envoyer `remote_audio_exclude` et
  arrêter immédiatement les captures devenues indésirables.

### Consentement et confidentialité

- Le poste contrôlé doit toujours confirmer avant de partager son son.
- Ce consentement ne vaut que pour la session en cours.
- NVDA doit rester exclu de la capture par processus.
- Sur un ancien Windows qui impose la capture globale, le message doit continuer
  à avertir que toute la carte son, y compris la voix de NVDA, sera partagée.

## 17. Ordre exact des modifications pour un développeur débutant

### Étape A - Installer les tests avant de toucher au son

1. Ajouter `pytest` comme dépendance de développement, pas comme dépendance
   embarquée dans l'add-on.
2. Créer `tests/conftest.py` avec les faux modules minimaux nécessaires pour
   importer le code hors de NVDA : `addonHandler`, `gui`, `ui`, `wx`, `nvwave`,
   `logHandler` et `buildVersion`.
3. Créer un faux transport avec une `queue`, une méthode `send()` qui mémorise les
   messages et un gestionnaire de callbacks minimal.
4. Écrire d'abord les tests du comportement actuel décrits à la section 19.
5. Lancer les tests et vérifier qu'ils passent avant toute modification audio.

Cette étape produit le filet de sécurité. Un test qui échoue avant les changements
ne doit pas être présenté comme une régression Opus.

### Étape B - Ajouter la DLL et son wrapper isolé

1. Construire `opus.dll` depuis la source officielle épinglée.
2. Copier uniquement les deux DLL finales et la licence sous `native`.
3. Dans `audio_opus.py`, définir les `argtypes` et `restype` de chaque fonction C
   immédiatement après le chargement.
4. Stocker les pointeurs encodeur/décodeur dans des objets propriétaires.
5. Fournir une méthode `close()` idempotente et un dernier recours de nettoyage ;
   ne jamais libérer deux fois le même pointeur.
6. Refuser toute donnée PCM dont la taille ne correspond pas exactement à une
   trame de 20 ms, soit 960 échantillons x 2 canaux x 2 octets = 3 840 octets.
7. Ne pas importer `wx`, `gui` ou le transport dans ce module.

Arrêt obligatoire : exécuter uniquement les tests du wrapper. Ne pas encore
modifier `audio_share.py`.

### Étape C - Faire évoluer le mélangeur sans changer le réseau

1. Ajouter les constantes du format stéréo 48 kHz.
2. Extraire la somme stéréo dans une fonction pure testable.
3. Supprimer `downmix()` du chemin audio distant.
4. Ajouter un mode de sortie stéréo explicite à la construction du mélangeur.
5. Faire fonctionner un essai local capture -> mélange stéréo -> Opus -> décodage
   -> fichier WAV de diagnostic ou lecteur local.

Arrêt obligatoire : écouter le résultat local et exécuter les tests du mélangeur.
À ce stade, aucun message réseau ne doit avoir changé.

### Étape D - Ajouter la négociation sans envoyer encore de son Opus

1. Déclarer `FEATURE_REMOTE_AUDIO_OPUS = "remote_audio_opus_v1"`.
2. Ajouter cette capacité uniquement lorsque `audio_opus.is_available()` réussit.
3. Ajouter les champs `codecs` à la requête et `codec` à la réponse.
4. Refuser une requête sans Opus, un codec non proposé ou un codec non disponible.
5. Journaliser le codec choisi ou la cause du refus, sans donnée audio.

Arrêt obligatoire : tester les capacités présentes, absentes et invalides avec un
faux transport. Aucun flux ne doit démarrer sans négociation Opus réussie.

### Étape E - Brancher les données Opus

1. Enregistrer un callback distinct pour `msg_remote_audio_opus_data`.
2. Créer l'encodeur une seule fois après acceptation de la session.
3. Accumuler exactement deux trames Opus de 20 ms par message.
4. Incrémenter la séquence pour chaque trame, y compris lorsqu'une trame est
   abandonnée pour congestion.
5. Valider complètement le message avant d'appeler la DLL côté réception.
6. Créer le décodeur avant de confirmer localement le démarrage de la lecture.
7. Fermer encodeur, décodeur, captures et lecteur sur arrêt, refus, déconnexion ou
   exception.
8. Faire évoluer `_on_mixed_block()` pour produire les trames Opus validées.

Arrêt obligatoire : tests du protocole et essai entre deux clients compatibles
Opus dans les deux sens.

### Étape F - Introduire le tampon adaptatif

Cette étape vient après la qualité, car changer simultanément codec et tampon rend
les défauts difficiles à attribuer.

1. Représenter la profondeur en millisecondes, pas en nombre de messages.
2. Commencer la lecture entre 80 et 120 ms.
3. Sur trou de séquence, demander au décodeur une trame PLC de 20 ms.
4. Ne jamais fabriquer plus de quelques trames PLC consécutives ; après une longue
   coupure, vider le tampon et attendre un nouveau préchargement.
5. Au-delà de 500 ms, supprimer les données anciennes et resynchroniser.
6. Mesurer les sous-alimentations avant d'ajuster automatiquement la cible.

### Étape G - Ajouter l'adaptation de débit en dernier

1. Commencer avec un débit fixe de 96 kbit/s pendant les premiers essais.
2. Mesurer la file du transport et les abandons pendant plusieurs secondes.
3. Ajouter ensuite les paliers 48, 64, 96 et 128 kbit/s.
4. Attendre au moins cinq secondes entre deux changements de palier.
5. Descendre rapidement en cas de congestion durable et remonter lentement.
6. Ne jamais changer fréquence, canaux ou durée de trame au milieu du flux.

## 18. Structure recommandée du code

Le but est d'éviter un unique `AudioShareManager` rempli de branches de codec.
Les responsabilités recommandées sont :

```text
audio_capture.py
  capture WASAPI et mélange PCM uniquement

audio_codecs.py
  interface commune du codec distant

audio_opus.py
  chargement de la DLL et codec Opus uniquement

audio_protocol.py
  validation et représentation des messages Opus, sans wxPython

audio_playback.py
  file temporelle et WavePlayer

audio_share.py
  état de session, consentement, négociation et orchestration
```

Règles de dépendance :

- `audio_opus.py` ne connaît pas TeleNVDA ;
- `audio_protocol.py` ne connaît ni WASAPI ni `wx` ;
- `audio_capture.py` ne connaît pas le transport ;
- `audio_playback.py` ne décode pas les messages JSON ;
- `audio_share.py` assemble ces briques mais ne réimplémente pas leurs calculs.

Une fonction pure doit être préférée pour la validation des messages, le calcul
des tailles, le mélange d'échantillons et la décision de palier de débit. Ce sont
les parties les plus simples à tester sans lancer NVDA.

## 19. Tests automatisés de non-régression obligatoires

### `tests/test_remote_audio_gesture.py`

Écrire les tests suivants avec de faux minuteurs et sans attendre réellement :

- `test_first_press_arms_timer_without_toggling` : le premier appui crée un
  minuteur, n'ouvre pas le dialogue et ne bascule pas la session ;
- `test_timer_expiry_toggles_once` : l'expiration remet
  `remote_audio_timer` à `None` et appelle une seule fois `toggle()` ;
- `test_second_press_cancels_timer_and_opens_sources` : le second appui arrête le
  minuteur, ouvre le dialogue et n'appelle jamais `toggle()` ;
- `test_double_press_works_while_sending_keys` : le comportement reste identique
  quand `sending_keys` vaut vrai ;
- `test_dialog_temporarily_returns_to_local_control` : le contrôle local est repris
  avant l'ouverture ;
- `test_dialog_restores_remote_control_after_close` : le contrôle distant revient
  après fermeture si la connexion existe encore ;
- `test_dialog_does_not_restore_after_disconnect` : aucune reprise distante si le
  poste contrôlé s'est déconnecté pendant le dialogue ;
- `test_dialog_restores_control_even_after_exception` : le `finally` reste actif ;
- `test_disconnect_cancels_pending_audio_timer` : aucun basculement retardé ne se
  produit après déconnexion ;
- `test_remote_audio_script_remains_in_guest_scripts` : le raccourci reste traité
  localement pendant le contrôle distant.

### `tests/test_audio_sources.py`

- `test_checked_application_is_not_excluded` ;
- `test_unchecked_application_is_excluded` ;
- `test_cancel_changes_nothing` ;
- `test_listener_selection_is_persisted_and_sent` ;
- `test_publisher_selection_is_session_only` ;
- `test_inactive_saved_exclusion_remains_visible` ;
- `test_added_name_is_trimmed_lowercase_and_unique` ;
- `test_exclusion_stops_running_capture_immediately` ;
- `test_source_list_updates_without_restarting_audio` ;
- `test_master_session_is_preferred_when_both_objects_exist` ;
- `test_slave_session_is_used_when_master_is_inactive`.

Les tests du dialogue doivent remplacer `AudioSourcesDialog` par un faux objet
pour vérifier la logique sans afficher de fenêtre. Les contrôles wxPython réels
restent couverts par la recette manuelle de la section 21.

### `tests/test_audio_mixer.py`

- une source stéréo ressort sans modification de canaux ;
- gauche seulement ne devient pas centre/mono ;
- droite seulement ne devient pas centre/mono ;
- deux sources sont additionnées canal par canal ;
- un dépassement positif ou négatif est borné correctement ;
- une source muette n'efface pas les autres ;
- la sortie Opus fait exactement 3 840 octets par trame de 20 ms ;
- le chemin Opus n'appelle jamais `downmix()`.

### `tests/test_audio_opus.py`

- la DLL correspondant à l'architecture courante est choisie ;
- une DLL absente rend Opus indisponible sans empêcher l'import de TeleNVDA ;
- une trame stéréo 48 kHz de 20 ms effectue un aller-retour ;
- la durée et le nombre de canaux sont conservés ;
- une taille PCM incorrecte est refusée avant l'appel natif ;
- un paquet invalide renvoie une erreur contrôlée ;
- `close()` peut être appelé deux fois ;
- l'encodeur et le décodeur ne sont pas partagés entre deux flux ;
- le débit peut changer sans recréer l'encodeur ;
- le gain de lecture respecte 0 %, 80 % et 100 %.

Un codec avec pertes ne doit pas être testé par égalité exacte des échantillons.
Vérifier la durée, l'absence de silence inattendu, la conservation des canaux et
des seuils raisonnables d'énergie. Utiliser des signaux générés dans les tests
pour éviter d'ajouter des enregistrements protégés au dépôt.

### `tests/test_audio_protocol.py`

- Opus est choisi seulement si les deux côtés le proposent ;
- une capacité absente refuse proprement la session ;
- une DLL indisponible refuse proprement la session ;
- un codec inconnu est refusé ;
- les séquences normales, manquantes, répétées et anciennes sont traitées ;
- plus de deux paquets par message est rejeté ;
- les chaînes Base64 invalides ou trop grandes sont rejetées ;
- un mauvais identifiant de flux est ignoré ;
- un message provenant d'un autre `origin` est ignoré ;
- l'arrêt et la déconnexion libèrent toutes les ressources ;
- une file saturée réduit le débit avant d'abandonner des trames ;
- aucune donnée audio n'est envoyée avant la négociation Opus.

### `tests/test_audio_playback.py`

- le lecteur Opus ouvre `WavePlayer` avec 48 kHz, 2 canaux et 16 bits ;
- la lecture attend le préchargement prévu ;
- une rafale ne dépasse pas le plafond temporel ;
- les données les plus anciennes sont supprimées en cas de retard excessif ;
- un trou court appelle PLC avec exactement 20 ms ;
- une longue coupure force un nouveau préchargement ;
- `stop()` débloque le thread et ferme le lecteur ;
- aucune lecture n'est effectuée sur le thread réseau.

### `tests/test_audio_consent.py`

- un refus n'ouvre ni capture ni encodeur ;
- une acceptation crée les ressources dans le bon ordre ;
- une erreur de capture répond `unavailable` et nettoie tout ;
- Windows compatible annonce la capture par applications ;
- l'ancien Windows avertit de la capture globale et de la voix NVDA ;
- une requête pendant une session active répond `busy`.

## 20. Commandes de validation à chaque étape

Les commandes doivent être lancées depuis la racine du dépôt. Ne pas attendre la
fin du projet pour les exécuter.

```powershell
python -m pytest tests/test_audio_opus.py -q
python -m pytest tests/test_audio_mixer.py -q
python -m pytest tests/test_audio_protocol.py -q
python -m pytest tests/test_audio_playback.py -q
python -m pytest tests/test_remote_audio_gesture.py tests/test_audio_sources.py -q
python -m pytest tests -q
python -m compileall addon/globalPlugins/remoteClient
python -m ruff check addon/globalPlugins/remoteClient tests
scons
```

Après `scons`, ouvrir le paquet `.nvda-addon` comme une archive et vérifier :

- présence des DLL x86 et x64 ;
- présence de la licence Opus ;
- absence de nouvel `.exe` lié à l'audio ;
- absence des sources, objets et outils de compilation Opus ;
- présence des modules Python ajoutés.

## 21. Recette manuelle NVDA obligatoire

Les tests automatisés ne prouvent pas le focus, les annonces vocales ni le
comportement réel du hook clavier. Effectuer cette recette avec deux postes ou
deux instances NVDA réellement connectées.

1. Démarrer sans partage audio et appuyer une fois sur
   `Insert+Contrôle+Maj+K` : la demande doit partir après le délai de double appui.
2. Refuser sur le poste contrôlé : aucune capture ni lecture ne doit rester active.
3. Recommencer et accepter : le contrôleur doit annoncer le démarrage.
4. Lire une voix, une musique stéréo, une vidéo et plusieurs notifications.
5. Vérifier que le contrôleur entend une stéréo réelle et des fréquences aiguës,
   sans conversion en 16 kHz mono.
6. Appuyer deux fois rapidement : le dialogue des sources doit s'ouvrir sans
   arrêter la session.
7. Vérifier que le focus initial est sur la liste des applications.
8. Parcourir la liste, le bouton Ajouter, OK et Annuler avec `Tab` et `Maj+Tab`.
9. Décocher une application puis valider : elle doit devenir muette immédiatement,
   tandis que les autres continuent.

10. Rouvrir, modifier puis annuler avec Échap ; recommencer avec la croix : aucune
  modification ne doit être appliquée.
11. Ajouter manuellement `vlc.exe`, vérifier l'absence de doublon et sa présence
  lorsqu'il ne tourne pas.
12. Refaire le double appui pendant que le clavier contrôle le poste distant : le
  dialogue doit rester local et utilisable.
13. Fermer le dialogue : le contrôle distant doit reprendre.
14. Refaire l'essai en déconnectant le poste distant pendant le dialogue : aucune
  fausse reprise de contrôle ne doit se produire.
15. Arrêter le son par un appui simple puis le redémarrer : aucun son ancien ne doit
  sortir du tampon.
16. Tester une capacité Opus absente ou une DLL indisponible : la demande doit être
  refusée avec un diagnostic clair et aucun paquet audio ne doit être envoyé.
17. Reconnecter après l'échec, puis vérifier qu'une session Opus valide démarre
  normalement lorsque la bibliothèque est disponible.

Le dialogue doit conserver son apparence Windows native, ses sizers, son ordre de
tabulation et son comportement modal. Cette amélioration ne justifie pas une
refonte visuelle du dialogue des sources.

## 22. État de la phase 1 au 10/09/2026

La phase 1 est lancée avec les éléments suivants :

- `audio_opus.py` charge `opus.dll` par chemin absolu, choisit x86 ou x64 selon la
  taille d'un pointeur, configure les signatures ctypes et possède séparément les
  encodeurs et décodeurs ;
- les trames PCM sont contrôlées avant l'appel natif, l'encodage est configuré en
  audio, VBR, signal automatique, 48 kHz et débit initial de 96 kbit/s ;
- `close()` est idempotent, les erreurs Opus sont converties en exceptions Python,
  et le gain du décodeur accepte 0, 80 et 100 % ;
- `tools/build_opus.py` télécharge `libopus 1.6.1`, vérifie le SHA-256 officiel
  `6ffcb593207be92584df15b32466ed64bbec99109f007c82205f0194572411a1`, puis
  construit les DLL x86 et x64 sans programmes de démonstration ni tests livrés ;
- la licence de redistribution est installée sous `native/opus-LICENSE.txt`, et
  la CI construit les deux DLL avant de lancer `tests/test_audio_opus.py`.

Validation locale effectuée : `py_compile`, Ruff et `scons` passent ; les six
tests de `tests/test_audio_opus.py` passent avec les DLL natives. La DLL x86 fait
370176 octets et porte le SHA-256
`7090BA4BBFFBC3B44C9665F46EC3B5EE7AB4593B0E8CCE8AF3C9AE49DCB658EE` ; la DLL
x64 fait 475136 octets et porte le SHA-256
`6B23B9E5EBB99CC9A2241A4D973C87FCAC7195453C93CC258A9BCDEFC2FF2E61`.
La construction locale a été réalisée avec Visual Studio 2022 et CMake.

## 23. État de la phase 2 au 10/09/2026

Le chemin local 48 kHz stéréo est maintenant isolé sans modifier le protocole
réseau :

- `audio_capture.Mixer` conserve son format 16 kHz mono historique par défaut ;
  le mode explicite `output_rate=48000, output_channels=2` conserve les canaux
  gauche et droit et additionne les sources canal par canal avec écrêtage 16 bits ;
- le mode stéréo ne passe jamais par `downmix()` ;
- `audio_playback.LocalOpusPlayer` encode puis décode localement chaque trame PCM
  avant de la remettre à `nvwave.WavePlayer` en 48 kHz, 2 canaux et 16 bits ;
- l'arrêt ferme le lecteur, le décodeur et l'encodeur, y compris après une erreur
  d'initialisation ;
- `AudioShareManager` et ses messages réseau continuent d'utiliser le chemin
  historique mono jusqu'à la négociation de la phase 3.

Les tests `tests/test_audio_mixer.py`, `tests/test_audio_playback_local.py` et
`tests/test_audio_opus.py` passent ensemble (10 tests). Ruff et la compilation des
modules audio passent également.

La comparaison d'écoute réelle et la mesure CPU x86/x64 restent à effectuer sur
des postes NVDA représentatifs. La phase 3 peut commencer pour ajouter la
négociation Opus et les messages réseau, mais elle ne doit pas supprimer le repli
mono tant que ces essais ne sont pas validés.

## 24. Journalisation nécessaire au diagnostic

Ajouter des messages `info` ou `debug` via `from logHandler import log`, jamais un
logger Python isolé qui ne serait pas visible dans le journal NVDA. Journaliser :

- architecture et version de `libopus` chargée ;
- disponibilité ou motif précis du refus ;
- codec et paramètres négociés ;
- démarrage et arrêt de l'encodeur, du décodeur, de la capture et du lecteur ;
- débit cible courant ;
- profondeur du tampon en millisecondes ;
- nombre de sous-alimentations, trames abandonnées et appels PLC ;
- liste des noms d'applications capturées ou exclues, comme aujourd'hui.

Ne jamais journaliser les octets PCM, les paquets Opus, la clé de chiffrement ou le
contenu audio. Limiter les compteurs périodiques pour ne pas écrire cinquante lignes
par seconde.

## 25. Gestion des échecs

Une erreur Opus doit être visible et réversible sans désinstaller l'add-on :

- si la DLL ne se charge pas, ne pas annoncer `remote_audio_opus_v1` ;
- si l'initialisation Opus échoue avant la réponse, refuser proprement la session
  avec un diagnostic exploitable ;
- ne pas basculer silencieusement de codec au milieu d'un flux actif ; arrêter puis
  recréer une session est plus sûr ;
- pouvoir désactiver temporairement l'annonce Opus par une constante interne lors
  des premières versions de test.

## 26. Erreurs fréquentes à éviter

- Encoder en Opus le résultat déjà réduit à 16 kHz mono.
- Conserver `downmix()` sur le chemin audio distant alors que la cible est stéréo.
- Charger une DLL x64 dans NVDA x86, ou l'inverse.
- Choisir la DLL d'après la version de Windows plutôt que l'architecture du
  processus NVDA.
- Recréer l'encodeur à chaque trame au lieu de conserver son état.
- Appeler un même encodeur ou décodeur depuis plusieurs threads.
- Décoder sans borner la taille fournie par le réseau.
- Faire jouer `WavePlayer` sur le thread qui lit les messages réseau.
- Utiliser le nombre de messages comme durée de tampon alors qu'un message peut
  contenir plusieurs trames.
- Activer DTX et conclure trop vite que les alertes faibles sont correctement
  conservées.
- Modifier le dialogue des sources en même temps que le codec.
- Persister les exclusions décidées par le poste qui partage son propre son.
- Utiliser `getLastScriptRepeatCount()` pour le double appui.
- Oublier d'annuler le minuteur lors d'une déconnexion ou de la fin du plugin.
- Tester seulement deux clients sans vérifier les capacités absentes et les erreurs
  d'initialisation de la DLL.

## 27. Définition de « terminé » pour chaque pull request

Une modification audio n'est prête à être intégrée que si :

- son périmètre correspond à une seule étape de la section 17 ;
- les tests nouveaux échouaient bien sans la modification lorsqu'il s'agit d'une
  correction de comportement ;
- tous les tests audio et de non-régression passent ;
- `compileall`, Ruff et `scons` passent ;
- le diff ne contient ni refactorisation étrangère ni binaire non prévu ;
- les ressources sont libérées sur réussite, refus, erreur et déconnexion ;
- le chemin Opus reste utilisable après arrêt, refus, déconnexion et reconnexion ;
- le journal NVDA permet de savoir quel codec a été choisi et pourquoi ;
- la documentation du protocole et la documentation utilisateur correspondent au
  comportement réellement livré ;
- la recette manuelle pertinente a été cochée sur NVDA x86 et x64 avant une
  publication stable.

## 28. État de la phase 4 au 10/09/2026

La phase 4 est implémentée :

- `audio_playback.TimedAudioBuffer` borne la lecture par durée, précharge 100 ms,
  conserve au maximum 500 ms et supprime les blocs les plus anciens en cas de
  rafale ; une sous-alimentation réarme le préchargement ;
- `audio_playback.OpusPlayer` décode en 48 kHz stéréo sur un thread dédié et
  journalise les abandons, la profondeur du tampon et les sous-alimentations ;
- les trous courts de séquence utilisent le PLC Opus, tandis qu'une coupure de
  plus de trois trames réinitialise le décodeur et le tampon ;
- `audio_protocol.BitrateController` applique les paliers 48, 64, 96 et
  128 kbit/s avec observations consécutives et cinq secondes d'hystérésis ;
- `audio_share.py` capture désormais le chemin Opus en PCM 48 kHz stéréo,
  groupe deux trames de 20 ms et abandonne les trames anciennes lorsque la file
  de transport est saturée ;
- les données audio restent bornées et chiffrées dans les messages existants,
  sans modification du relais.

Validation automatisée : 17 tests `pytest tests -q` réussis, compilation Python
et Ruff réussis. Il reste à effectuer le build `scons`, puis la recette manuelle
sur NVDA x86 et x64 : démarrage, arrêt, déconnexion, rafale, coupure courte et
liaison limitée. Les seuils de débit et la latence réelle doivent encore être
mesurés sur des postes représentatifs avant publication stable.

## 29. État de la phase 5 au 10/09/2026

La validation automatisée de la phase 5 est passée :

- `pytest tests -q` réussit avec 17 tests ;
- `compileall`, Ruff ciblé et `scons` réussissent ;
- les DLL `native/x86/opus.dll` et `native/x64/opus.dll` ainsi que leur licence
  sont présentes ;
- la CI exécute désormais toute la suite audio et `compileall` sur Linux et
  Windows, en plus du test du wrapper Opus.

La recette réelle n'est pas déclarée terminée sans deux postes NVDA. Il reste à
tester NVDA x86 et x64, TLS brut, WebSocket TLS sur 443, réseau local, bonne
liaison Internet, liaison limitée, Firefox, Chrome, lecteurs multimédias, sons
Windows, synthèses tierces et comparaison d'écoute à l'aveugle. Une version
prerelease doit être publiée sur le canal de test avec les journaux détaillés
avant toute publication stable. La matrice complète se trouve dans le
[rapport de validation de la phase 5](validation-audio-phase5.md).
