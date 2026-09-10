# Plan d’implémentation — bascule de l’interprétation clavier NVDA sur le poste contrôlé

## 1. Objectif fonctionnel

Ajouter à TeleNVDA une commande utilisable depuis le poste **master** lorsque le clavier contrôle encore le PC local.

Cette commande doit permettre de basculer entre deux états sur l’unique poste **slave** connecté :

1. **Mode normal** : les touches envoyées par TeleNVDA sont d’abord interprétées par NVDA sur le slave. Les raccourcis NVDA distants continuent donc de fonctionner.
2. **Mode clavier brut distant** : les touches injectées par TeleNVDA traversent NVDA sans être interprétées par lui. Windows et les autres applications du slave, notamment JAWS ou Narrator, peuvent alors recevoir leurs raccourcis normalement.

Le même raccourci doit rétablir le mode normal. Le master doit annoncer vocalement le résultat réellement confirmé par le slave.

Exemple de raccourci : `Insert+Maj+Échap`. Il devra rester réaffectable dans la boîte de dialogue **Gestes de commandes** de NVDA.

## 2. Point technique essentiel : ne pas arrêter réellement le hook global de NVDA

Le besoin utilisateur parle de « désactiver le hook clavier ». Il ne faut toutefois pas appeler directement `keyboardHandler.terminate()`, `winInputHook.terminate()` ou `winInputHook.setCallbacks()`.

Ces API internes pilotent l’infrastructure globale de saisie de NVDA. Le module `winInputHook` gère aussi la souris, utilise un compteur de références et fait partie du cycle de démarrage et d’arrêt de NVDA. Le désinstaller depuis TeleNVDA pourrait donc :

- casser le clavier local du slave ;
- casser la souris de NVDA ;
- désynchroniser le compteur interne du hook ;
- empêcher NVDA de restaurer correctement ses callbacks ;
- provoquer une régression dans d’autres extensions NVDA ;
- rendre la reprise difficile après une erreur ou une déconnexion.

La solution recommandée est plus ciblée : **demander à NVDA d’ignorer uniquement les événements clavier injectés par TeleNVDA pendant leur injection**.

Dans les versions NVDA récentes, le gestionnaire clavier expose le gestionnaire de contexte `keyboardHandler.ignoreInjection()`. Pendant ce contexte, une touche injectée est transmise à Windows mais n’est pas traitée comme un geste NVDA. Cela correspond au résultat attendu sans arrêter le hook global :

- le clavier physique du slave reste utilisable avec NVDA ;
- les touches distantes atteignent Windows, JAWS, Narrator et les applications ;
- la souris et les autres mécanismes NVDA ne changent pas ;
- la portée de la modification se limite au chemin d’injection TeleNVDA.

Le nom technique conseillé pour le code et le protocole est donc **remote keyboard passthrough** ou **clavier brut distant**, plutôt que « arrêt du hook ».

## 3. Hypothèses retenues pour la première version

Pour éviter toute ambiguïté dangereuse, la première version suivra ces règles :

- la commande n’est disponible que sur un master connecté ;
- le master doit être en mode de contrôle local, donc `sending_keys == False` ;
- il doit y avoir exactement un slave connu dans `MasterSession.slaves` ;
- le slave doit annoncer explicitement qu’il prend en charge la fonctionnalité ;
- l’état est temporaire et propre à la session réseau ;
- le mode normal est toujours l’état initial d’une nouvelle connexion ;
- une déconnexion ou le départ du dernier master remet l’état interne en mode normal ;
- aucune préférence NVDA et aucune valeur TeleNVDA ne sont modifiées sur disque ;
- le bureau sécurisé n’est pas pris en charge dans la première version ;
- aucun changement spécifique à TCP ou WebSocket n’est autorisé.

Cette restriction à un seul slave est importante : les messages ordinaires sont diffusés à tout le canal. Un simple champ `target` ne garantit pas le ciblage avec tous les relais et avec le serveur direct inclus dans le client.

## 4. Comportement utilisateur attendu

### 4.1 Activation

Préconditions :

- le poste est master ;
- la connexion est active ;
- un seul slave compatible est présent ;
- le clavier contrôle le PC local.

Séquence :

1. L’utilisateur appuie sur `Insert+Maj+Échap`.
2. Le master envoie une requête au slave, sans changer immédiatement son état affiché.
3. Le slave valide la requête, active le mode clavier brut distant et renvoie son état réel.
4. Après réception de la confirmation, le master annonce : « L’interprétation clavier de NVDA est désactivée pour les touches distantes. »
5. L’utilisateur peut basculer vers le contrôle distant avec le raccourci TeleNVDA habituel.
6. Les touches envoyées au slave ne déclenchent plus les commandes NVDA du slave ; elles restent disponibles pour Windows, JAWS, Narrator ou l’application au premier plan.

### 4.2 Désactivation

1. L’utilisateur revient d’abord au contrôle local.
2. Il appuie à nouveau sur le raccourci.
3. Le master demande le retour au mode normal.
4. Le slave confirme l’état.
5. Le master annonce : « L’interprétation clavier de NVDA est réactivée pour les touches distantes. »

### 4.3 Cas refusés

Prévoir des annonces explicites :

- aucune connexion master : « Aucun ordinateur distant n’est connecté. » ;
- connexion au relais sans slave : « Aucun ordinateur contrôlé n’est connecté. » ;
- plusieurs slaves : « Plusieurs ordinateurs contrôlés sont connectés ; la commande clavier brut est annulée. » ;
- slave ancien ou incompatible : « L’ordinateur contrôlé ne prend pas en charge le mode clavier brut. » ;
- contrôle distant actuellement actif : « Revenez d’abord au contrôle local pour modifier le mode clavier distant. » ;
- requête déjà en attente : « Le changement du mode clavier distant est déjà en cours. » ;
- absence de réponse : « L’ordinateur contrôlé n’a pas confirmé le changement du mode clavier. » ;
- refus ou erreur sur le slave : annoncer le refus sans prétendre que l’état a changé.

## 5. Architecture cible

Le trajet normal d’une touche restera :

`GlobalPlugin` sur le master → `Transport.send(type="key")` → `SlaveSession.handle_key()` → `LocalMachine.send_key()` → `input.send_key()` → `SendInput`.

Le nouveau mode ne doit modifier que la dernière partie :

- mode normal : appel actuel à `SendInput` ;
- mode clavier brut : même appel à `SendInput`, exécuté dans `keyboardHandler.ignoreInjection()`.

Le transport continue d’injecter exactement les mêmes codes virtuels, codes de balayage, états appuyé/relâché et indicateurs de touche étendue.

### 5.1 Pourquoi le contexte doit entourer l’injection réelle

`LocalMachine.send_key()` programme actuellement `input.send_key()` avec `wx.CallAfter`. Il ne suffit donc pas d’ouvrir `keyboardHandler.ignoreInjection()` dans `SlaveSession.handle_key()` : le contexte serait déjà fermé lorsque l’injection différée aurait réellement lieu.

Le booléen de contournement doit être transmis jusqu’à la fonction exécutée par `wx.CallAfter`, et le contexte doit être ouvert dans cette fonction juste autour de `SendInput`.

## 6. Fichiers à modifier ou à créer

### 6.1 Nouveau fichier `addon/globalPlugins/remoteClient/remote_keyboard.py`

Créer un module isolé contenant :

- les noms des deux messages réseau ;
- le nom de la capacité ;
- les codes d’erreur stables ;
- une petite classe représentant l’état côté slave ;
- une fonction ou un gestionnaire de contexte de compatibilité pour ignorer une injection ;
- la validation des champs réseau ;
- éventuellement la génération des `request_id` côté master.

Noms proposés :

- capacité : `remote_keyboard_passthrough` ;
- requête : `remote_keyboard_passthrough_request` ;
- réponse : `remote_keyboard_passthrough_state`.

La classe côté slave peut s’appeler `RemoteKeyboardPassthrough` et exposer seulement :

- `enabled` en lecture ;
- `set_enabled(value)` ;
- `reset()` ;
- `inject(send_callable, *args, **kwargs)` ou un contexte équivalent.

Elle ne doit jamais écrire dans `config.conf`, `teleNVDA.ini` ou une autre configuration persistante.

### 6.2 `addon/globalPlugins/remoteClient/input.py`

Faire une modification minimale de `send_key()` :

1. ajouter un argument nommé explicite, par exemple `bypass_nvda=False` ;
2. conserver le chemin actuel sans changement lorsque sa valeur est fausse ;
3. lorsque sa valeur est vraie, importer `keyboardHandler` localement afin d’éviter les imports circulaires ;
4. exécuter l’unique appel `SendInput` dans `keyboardHandler.ignoreInjection()` ;
5. garantir avec un `with` ou un `try/finally` que l’état d’ignorance est restauré même si `SendInput` échoue.

Pour faciliter les tests, extraire si nécessaire la construction de la structure `INPUT` de l’appel système lui-même, sans reformater le reste du fichier.

Ne pas modifier le chemin de la souris.

### 6.3 `addon/globalPlugins/remoteClient/local_machine.py`

Étendre `LocalMachine.send_key()` avec le même argument `bypass_nvda=False`, puis transmettre cet argument à `input.send_key()` dans l’appel `wx.CallAfter`.

Vérifier que :

- la valeur par défaut garde exactement le comportement historique ;
- les anciens appels `send_key(**kwargs)` continuent de fonctionner ;
- aucun autre appelant ne doit être modifié s’il n’utilise pas la nouvelle fonction.

Ce point est un garde-fou majeur contre les régressions.

### 6.4 `addon/globalPlugins/remoteClient/capabilities.py`

Ajouter une constante `FEATURE_REMOTE_KEYBOARD_PASSTHROUGH` et l’inclure dans les capacités locales disponibles.

Ajouter une méthode simple de lecture par pair, par exemple `peer_supports(peer_id, feature)`, qui doit :

- renvoyer `False` si le pair n’a rien annoncé ;
- renvoyer `False` si la capacité est absente ;
- ne pas modifier les données internes.

Ne pas utiliser seulement `all_peers_support()` pour cette fonctionnalité : cette méthode considère tous les membres du canal, y compris ceux qui ne sont pas le slave ciblé.

### 6.5 `addon/globalPlugins/remoteClient/session.py`

#### Côté slave

Dans `SlaveSession.__init__()` :

- créer l’objet d’état `RemoteKeyboardPassthrough` ;
- enregistrer le callback de la requête `msg_remote_keyboard_passthrough_request`.

Dans le nouveau handler :

1. vérifier que `origin` existe ;
2. vérifier que `origin` appartient à `self.masters` ;
3. refuser si plusieurs masters sont présents dans cette première version ;
4. vérifier que `request_id` est une chaîne de longueur raisonnable ;
5. vérifier strictement que `enabled` est un booléen, et non `0`, `1`, une chaîne ou `None` ;
6. appliquer l’état de manière idempotente ;
7. renvoyer systématiquement une réponse contenant l’état réel ;
8. journaliser les requêtes malformées avec `from logHandler import log` sans inclure de donnée sensible.

Modifier `handle_key()` pour appeler :

- `self.local_machine.send_key(..., bypass_nvda=False)` en mode normal ;
- `self.local_machine.send_key(..., bypass_nvda=True)` en mode clavier brut.

Conserver `configuration.record_activity()` dans les deux modes.

Appeler `reset()` :

- dans `handle_transport_closing()` ;
- dans `handle_transport_disconnected()` si cet événement est effectivement enregistré pour la session ;
- dans `handle_client_disconnected()` lorsque le dernier master part.

Même si le mode ne désinstalle aucun hook, cette remise à zéro évite qu’un futur master hérite de l’état d’une ancienne session.

#### Côté master

Dans `MasterSession.__init__()` :

- enregistrer le callback de `msg_remote_keyboard_passthrough_state` ;
- initialiser l’état connu à `False` ou, plus rigoureusement, à `None` tant qu’aucune réponse n’a été reçue ;
- conserver au maximum une requête en attente avec son `request_id`, l’état demandé et un minuteur d’expiration.

Ajouter une méthode de requête qui :

1. vérifie qu’il existe exactement un slave ;
2. retrouve son identifiant ;
3. vérifie sa capacité ;
4. refuse une seconde requête tant que la première attend une réponse ;
5. génère un `request_id` avec `uuid.uuid4()` ;
6. envoie la requête par `self.transport.send()` ;
7. démarre un délai de réponse court, par exemple cinq secondes.

Le handler de réponse doit :

- vérifier que `origin` est l’unique slave attendu ;
- vérifier que le `request_id` correspond à la requête en attente ;
- ignorer et journaliser les réponses anciennes, dupliquées ou étrangères ;
- annuler le minuteur ;
- enregistrer uniquement l’état confirmé ;
- demander au `GlobalPlugin` d’annoncer le résultat, via un callback fourni ou une méthode de session clairement nommée.

Lors d’une déconnexion, vider l’état connu, la requête en attente et le minuteur.

### 6.6 `addon/globalPlugins/remoteClient/__init__.py`

Ajouter un script NVDA, par exemple `script_toggle_remote_keyboard_passthrough()`.

Le script doit :

1. vérifier `_is_master_connected()` ;
2. vérifier `sending_keys == False` ;
3. vérifier `_remote_slave_available()` ;
4. demander à `MasterSession` l’état opposé à l’état confirmé ;
5. ne jamais annoncer le succès avant la réponse du slave.

Ajouter ce script à `guestScripts`. Cette précaution évite que son geste soit injecté par erreur sur le slave lorsque le contrôle distant est actif. Le script doit néanmoins refuser le changement et demander un retour au contrôle local lorsque `sending_keys` vaut vrai.

Déclarer le script avec une description traduisible et lui affecter par défaut le geste NVDA `kb:shift+insert+escape`, correspondant à `Insert+Maj+Échap`. Le geste devra rester réaffectable dans **Gestes de commandes**.

Éviter d’utiliser `getLastScriptRepeatCount()` : les scripts présents dans `guestScripts` peuvent être appelés directement et court-circuiter le mécanisme normal de comptage des répétitions.

### 6.7 `addon/globalPlugins/remoteClient/bridge.py`

Ajouter les deux nouveaux types de messages à `BridgeTransport.excluded`.

Motif : le bureau sécurisé utilise une seconde instance de NVDA et un pont local. Faire traverser la requête pourrait appliquer la commande dans deux contextes distincts ou produire deux réponses. La première version doit donc annoncer que la fonction n’est pas disponible sur le bureau sécurisé plutôt que d’avoir un comportement incertain.

### 6.8 `protocol.md`

Documenter :

- la capacité `remote_keyboard_passthrough` ;
- le message de requête ;
- le message de réponse ;
- la signification précise de `enabled` ;
- le rôle de `request_id` ;
- les contrôles de `origin` ;
- les codes d’erreur ;
- la limitation à un seul slave et un seul master ;
- la remise à zéro lors d’une déconnexion ;
- le fait que les messages sont ordinaires et diffusés par les relais historiques.

Schéma proposé pour la requête :

```json
{
  "type": "remote_keyboard_passthrough_request",
  "request_id": "UUID",
  "enabled": true
}
```

Schéma proposé pour la réponse :

```json
{
  "type": "remote_keyboard_passthrough_state",
  "request_id": "UUID",
  "success": true,
  "enabled": true,
  "reason": ""
}
```

Codes de refus suggérés :

- `invalid_origin` ;
- `multiple_masters` ;
- `invalid_request` ;
- `unsupported_nvda_version` ;
- `internal_error`.

Ne pas ajouter ces messages à `EXCLUDED_FROM_ENCRYPTION` dans `transport.py`. Ils doivent bénéficier automatiquement du chiffrement applicatif AES-GCM lorsqu’une clé de chiffrement de session est configurée.

### 6.9 Documentation utilisateur et traductions

Mettre à jour :

- `readme.md` ;
- `addon/doc/fr/readme.md` ;
- les autres documentations localisées seulement selon la politique habituelle du projet.

Expliquer clairement que :

- seul le traitement NVDA des touches injectées par TeleNVDA est contourné ;
- le hook clavier global de NVDA n’est pas arrêté ;
- le clavier physique du slave continue de piloter NVDA ;
- le mode sert notamment à envoyer les raccourcis JAWS, Narrator ou applicatifs ;
- le mode revient à la normale après une nouvelle connexion ;
- la première version ne fonctionne pas avec plusieurs slaves ni sur le bureau sécurisé.

Après stabilisation des chaînes anglaises :

1. lancer `scons pot` ;
2. lancer `scons updatePo` ;
3. vérifier les catalogues avec `msgfmt --check` ;
4. traduire au minimum les nouvelles chaînes françaises ;
5. ne pas modifier manuellement `messages.mo` sans suivre la procédure de build du projet.

## 7. Prototype de compatibilité NVDA à réaliser avant le protocole définitif

La présence et le comportement de `keyboardHandler.ignoreInjection()` doivent être vérifiés sur toutes les versions NVDA officiellement prises en charge par la version de TeleNVDA qui recevra la fonction.

### 7.1 Vérifications minimales

Pour chaque version retenue :

1. vérifier que `keyboardHandler.ignoreInjection` existe ;
2. vérifier qu’une touche injectée dans ce contexte traverse bien NVDA ;
3. vérifier que cette touche atteint l’application active ;
4. vérifier que JAWS ou Narrator peut la recevoir ;
5. vérifier qu’une touche physique utilisée en même temps reste traitée normalement par NVDA ;
6. vérifier les événements appuyé et relâché séparément ;
7. vérifier qu’une exception ne laisse pas `keyboardHandler.ignoreInjected` actif ;
8. vérifier que l’appel peut être effectué sur le thread principal via `wx.CallAfter`.

### 7.2 Politique de compatibilité

Ordre de préférence :

1. utiliser `keyboardHandler.ignoreInjection()` lorsqu’il existe ;
2. si une ancienne version prise en charge ne l’expose pas, créer dans `remote_keyboard.py` un adaptateur très court qui sauvegarde puis restaure `keyboardHandler.ignoreInjected` dans un `try/finally` ;
3. si même cette variable n’existe pas ou si son comportement n’est pas fiable, ne pas annoncer la capacité sur cette version de NVDA ;
4. ne jamais compenser en arrêtant le hook global.

La fonction `available_features()` peut décider dynamiquement de publier ou non la capacité selon le résultat de ce contrôle.

## 8. Gestion des touches maintenues

Les messages `key` représentent séparément l’appui et le relâchement. Un changement de mode au milieu d’une combinaison pourrait donc créer une incohérence : par exemple, `Ctrl` appuyé en mode normal puis relâché en mode brut.

Pour éviter ce cas :

- la bascule est interdite pendant le contrôle distant ;
- le retour au contrôle local utilise déjà `_release_remote_keys()` ;
- le script n’envoie la requête qu’après ce retour ;
- les tests doivent maintenir volontairement `Ctrl`, `Alt`, `Maj`, Windows et la touche NVDA pendant les transitions ;
- aucune modification ne doit être faite à la logique existante de `key_modifiers` sans test de régression dédié.

## 9. Sécurité et validation réseau

Le slave reçoit des données provenant du réseau. Même si la connexion utilise TLS, chaque champ doit être validé.

Règles :

- ne faire confiance qu’au champ `origin` ajouté par le relais ;
- confirmer que cette origine est enregistrée comme master ;
- ne jamais accepter un rôle fourni dans la charge utile ;
- limiter `request_id`, par exemple à 128 caractères ;
- accepter uniquement le type booléen exact pour `enabled` ;
- ignorer les champs inconnus ;
- rendre `set_enabled()` idempotent ;
- limiter les requêtes répétées, par exemple une requête active à la fois côté master ;
- ne jamais inclure de clé de connexion ou de chiffrement dans les journaux ;
- utiliser `from logHandler import log`, car un logger Python ordinaire ne remonte pas correctement les niveaux information et débogage dans le journal NVDA de ce projet.

Une personne connaissant la clé du canal peut actuellement se déclarer master. Cette fonctionnalité ne doit donc pas être présentée comme un mécanisme d’autorisation forte. Sa portée limitée aux injections TeleNVDA réduit néanmoins le risque par rapport à l’arrêt global du hook.

## 10. Stratégie de tests automatisés

Le dépôt ne contient pas encore de véritable suite de tests unitaires. `connectivity_test.py` est une fonctionnalité de diagnostic, pas une suite automatisée.

Il est recommandé d’introduire une petite base de tests avec `pytest`, en gardant les dépendances NVDA simulées.

### 10.1 Infrastructure proposée

Créer :

- `tests/conftest.py` pour installer de faux modules `wx`, `keyboardHandler`, `ui`, `buildVersion` et autres dépendances NVDA strictement nécessaires ;
- `tests/test_remote_keyboard.py` pour la logique isolée ;
- `tests/test_remote_keyboard_session.py` pour les handlers réseau ;
- `tests/test_input.py` pour le chemin d’injection ;
- éventuellement `tests/fakes.py` pour un faux transport et un faux gestionnaire de callbacks.

Ajouter `pytest` aux dépendances de développement dans `pyproject.toml`, puis ajouter une étape de test à la CI avant le build. Ne pas mélanger les tests et le paquet livré dans `addon/`.

### 10.2 Tests de `input.send_key()`

Tester au minimum :

- comportement historique lorsque `bypass_nvda=False` ;
- un seul appel `SendInput` par message ;
- entrée et sortie du contexte lorsque `bypass_nvda=True` ;
- restauration du contexte lorsque `SendInput` lève une exception ;
- conservation de `vk`, `scan`, `extended` et `pressed` ;
- aucun changement dans les fonctions de souris.

### 10.3 Tests de `LocalMachine.send_key()`

Avec un faux `wx.CallAfter` :

- l’appel historique transmet `bypass_nvda=False` ;
- le nouveau mode transmet `True` ;
- l’appel reste différé ;
- les arguments réseau supplémentaires inconnus ne cassent pas la fonction.

### 10.4 Tests de la session slave

Tester :

- état initial normal ;
- activation valide ;
- désactivation valide ;
- double activation idempotente ;
- double désactivation idempotente ;
- origine absente ;
- origine inconnue ;
- origine qui n’est pas master ;
- plusieurs masters ;
- `enabled` absent ou non booléen ;
- `request_id` absent, vide ou trop long ;
- réponse contenant toujours l’état réel ;
- `handle_key()` utilise le bon booléen ;
- `configuration.record_activity()` reste appelé ;
- départ du dernier master ;
- fermeture du transport ;
- exception interne transformée en réponse d’échec et journalisée.

### 10.5 Tests de la session master

Tester :

- aucune connexion ;
- aucun slave ;
- état des slaves pas encore connu ;
- plusieurs slaves ;
- slave sans capacité ;
- envoi correct de la demande ;
- impossibilité d’empiler deux demandes ;
- corrélation correcte du `request_id` ;
- réponse provenant d’un autre pair ;
- réponse ancienne ou dupliquée ;
- réponse de succès ;
- réponse d’échec ;
- expiration du délai ;
- nettoyage à la déconnexion ;
- annonce uniquement après confirmation.

### 10.6 Tests du script global

Avec une fausse `MasterSession` :

- refus hors connexion master ;
- refus sans slave ;
- refus pendant `sending_keys=True` ;
- demande de l’état opposé à l’état confirmé ;
- aucune annonce prématurée ;
- présence du script dans `guestScripts` ;
- description traduisible et commande visible dans Gestes de commandes.

### 10.7 Tests de non-régression obligatoires

Puisque `input.send_key()` et `LocalMachine.send_key()` sont des chemins existants sensibles, ajouter des tests qui prouvent que le mode par défaut reste inchangé :

- les raccourcis NVDA distants fonctionnent toujours en mode normal ;
- les touches ordinaires arrivent toujours dans l’application distante ;
- les appuis et relâchements des modificateurs restent équilibrés ;
- la bascule local/distant existante fonctionne toujours ;
- `ignoreNextGesture` fonctionne toujours ;
- le clavier master n’est pas avalé lorsqu’aucun slave n’est disponible ;
- la souris, le Braille, la parole, le presse-papiers, les fichiers, l’audio et le partage d’écran ne changent pas.

## 11. Matrice de tests manuels

Utiliser deux installations portables de NVDA, puis répéter les scénarios avec :

- master TCP et slave TCP ;
- master WebSocket et slave WebSocket ;
- master TCP et slave WebSocket si le relais autorise ce mélange ;
- master WebSocket et slave TCP ;
- chiffrement applicatif activé ;
- chiffrement applicatif absent ;
- reconnexion après coupure réseau ;
- connexion via le serveur direct inclus dans TeleNVDA.

Pour chaque combinaison :

1. connecter le master et le slave ;
2. rester en contrôle local ;
3. activer le mode clavier brut ;
4. attendre l’annonce de confirmation ;
5. passer au contrôle distant ;
6. vérifier qu’un raccourci NVDA distant n’est plus exécuté par NVDA ;
7. vérifier un raccourci JAWS ou Narrator ;
8. vérifier un raccourci Windows, par exemple `Windows+R` ;
9. vérifier la saisie de texte simple ;
10. vérifier les caractères accentués et AltGr sur un clavier français ;
11. revenir au contrôle local ;
12. réactiver l’interprétation NVDA ;
13. repasser au contrôle distant ;
14. vérifier qu’un raccourci NVDA distant fonctionne à nouveau ;
15. déconnecter pendant chaque état ;
16. reconnecter et vérifier que le mode normal est rétabli ;
17. examiner le journal NVDA des deux postes.

Tests particuliers :

- Caps Lock utilisé comme touche NVDA ;
- Insert et Insert pavé numérique utilisés comme touche NVDA ;
- touches répétées maintenues ;
- `Ctrl`, `Alt`, `Maj`, Windows et AltGr ;
- verrouillage de session ;
- changement de disposition clavier ;
- présence simultanée de NVDA et JAWS ;
- présence simultanée de NVDA et Narrator ;
- un deuxième slave rejoint le canal avant ou pendant la demande ;
- un deuxième master rejoint le canal côté slave ;
- ancien TeleNVDA ou NVDA Remote standard ne publiant pas la capacité.

## 12. Ordre d’implémentation conseillé à un développeur débutant

### Phase 1 — Faire un prototype local sans réseau

1. Créer une branche dédiée.
2. Écrire un petit prototype temporaire exécuté dans NVDA.
3. Injecter une touche normalement, puis dans `keyboardHandler.ignoreInjection()`.
4. Vérifier la différence dans Bloc-notes, NVDA, JAWS et Narrator.
5. Tester toutes les versions NVDA ciblées.
6. Noter les résultats et supprimer le prototype temporaire.

**Livrable de phase** : tableau de compatibilité NVDA et décision définitive sur l’adaptateur.

### Phase 2 — Isoler l’injection

1. Ajouter l’argument `bypass_nvda` à `input.send_key()`.
2. Le transmettre depuis `LocalMachine.send_key()`.
3. Écrire les tests de ces deux fonctions avant de toucher au réseau.
4. Vérifier que tous les tests du chemin historique passent.

**Livrable de phase** : injection brute testée, désactivée par défaut et sans changement réseau.

### Phase 3 — Ajouter l’état slave

1. Créer `remote_keyboard.py`.
2. Ajouter l’objet d’état à `SlaveSession`.
3. Faire utiliser cet état par `handle_key()`.
4. Ajouter les remises à zéro du cycle de vie.
5. Écrire les tests de session slave.

**Livrable de phase** : un état local contrôlable dans les tests, sans commande utilisateur.

### Phase 4 — Ajouter la négociation et le protocole

1. Ajouter la capacité.
2. Ajouter `peer_supports()`.
3. Enregistrer les deux messages.
4. Valider `origin`, `request_id` et `enabled`.
5. Ajouter la corrélation et le délai côté master.
6. Tester les clients anciens et les réponses incorrectes.

**Livrable de phase** : aller-retour master/slave confirmé avec un faux transport.

### Phase 5 — Ajouter le raccourci et les annonces

1. Ajouter le script global.
2. L’ajouter à `guestScripts`.
3. Faire respecter le contrôle local.
4. Tester `Insert+Maj+Échap` sur AZERTY.
5. Ajouter les annonces traduisibles.
6. Vérifier la visibilité dans Gestes de commandes.

**Livrable de phase** : fonctionnalité utilisable de bout en bout avec deux NVDA portables.

### Phase 6 — Protéger les cas particuliers

1. Exclure les messages du bridge de bureau sécurisé.
2. Refuser les configurations multi-master et multi-slave.
3. Tester les déconnexions et reconnexions.
4. Tester toutes les touches maintenues.
5. Vérifier TCP et WebSocket sans branche de code spécifique.

**Livrable de phase** : matrice manuelle complétée et journaux sans erreur.

### Phase 7 — Documentation, traduction et CI

1. Mettre à jour le protocole.
2. Mettre à jour les guides utilisateur.
3. Générer et mettre à jour les catalogues de traduction.
4. Ajouter `pytest` et la commande de tests à la CI.
5. Lancer Ruff, Pyright, les tests, le build SCons et le contrôle des traductions.

**Livrable de phase** : changement prêt pour revue, paquet construit et documentation complète.

## 13. Commandes de validation prévues

À exécuter dans l’environnement de développement du projet, sans correction automatique tant que les différences n’ont pas été relues :

- `pytest -q` ;
- `ruff check .` ;
- `ruff format --check .` ;
- `pyright` ;
- `scons` ;
- `scons pot` ;
- `scons updatePo` uniquement après stabilisation des chaînes ;
- `msgfmt --check --statistics` pour chaque catalogue modifié.

Les artefacts `.nvda-addon` générés ne doivent pas être ajoutés au commit.

## 14. Critères d’acceptation

La fonctionnalité est terminée uniquement si tous les points suivants sont vrais :

- le raccourci est exécuté localement sur le master ;
- il ne fonctionne qu’avec exactement un slave compatible ;
- il fonctionne avec TCP et WebSocket sans logique dupliquée ;
- le master annonce uniquement un état confirmé ;
- les touches distantes contournent NVDA en mode clavier brut ;
- les raccourcis JAWS, Narrator, Windows et applicatifs sont utilisables ;
- le clavier physique du slave continue de contrôler NVDA ;
- la souris NVDA continue de fonctionner ;
- le retour au mode normal restaure les raccourcis NVDA distants ;
- une nouvelle connexion commence toujours en mode normal ;
- aucun état n’est enregistré dans la configuration ;
- une erreur ou une déconnexion ne laisse pas `ignoreInjected` actif ;
- les modificateurs ne restent pas bloqués ;
- les clients anciens sont refusés proprement ;
- le bureau sécurisé et les connexions multiples sont refusés explicitement ;
- les tests de non-régression du chemin historique passent ;
- Ruff, Pyright, pytest, SCons et les contrôles de traduction passent.

## 15. Points à confirmer avant le développement

Ces choix peuvent être ajustés sans remettre en cause l’architecture proposée :

1. conserver `Insert+Maj+Échap` comme geste par défaut après le test sur AZERTY, ou publier la commande sans geste par défaut ;
2. maintenir la restriction à un seul slave et un seul master pour la première version ;
3. nom utilisateur final : « mode clavier brut distant », « ignorer les commandes NVDA distantes » ou autre formulation ;
4. versions NVDA réellement supportées et donc incluses dans la matrice de compatibilité ;
5. comportement souhaité sur le bureau sécurisé dans une future version.

Aucun de ces points ne justifie d’arrêter le hook global de NVDA. La première implémentation doit rester limitée au contournement des événements injectés par TeleNVDA afin de minimiser l’impact sur le reste du module.
