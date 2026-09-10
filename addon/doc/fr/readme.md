# TeleNVDA by Accessolutions

TeleNVDA est une extension NVDA pour assister une personne à distance,
effectuer une maintenance ou suivre une formation. Elle reste compatible
avec le protocole NVDA Remote.

Ce projet est maintenu par Accessolutions :
[github.com/Accessolutions/telenvda-accessolutions](https://github.com/Accessolutions/telenvda-accessolutions).

## Connexions

TeleNVDA conserve le transport TLS historique et ajoute un transport
WebSocket sécurisé :

* TCP/TLS reste disponible pour les serveurs classiques ;
* WebSocket utilise `wss://`, le sous-protocole `nvdaremote/2.0` et le port
  HTTPS 443 par défaut ;
* le chemin WebSocket est configurable, par exemple `/remote` ;
* les proxies HTTP et SOCKS peuvent être configurés dans les options ;
* le mode proxy propose une configuration manuelle, une détection automatique
  Windows (WinHTTP, PAC/WPAD et exclusions) ou l'absence de proxy ;
* les proxies HTTP d'entreprise peuvent utiliser `negotiate` (Kerberos ou
  NTLM via Windows SSPI) ou `ntlm` ;
* la reconnexion automatique et le chiffrement applicatif AES-GCM facultatif
  sont conservés.

Lorsque la reconnexion automatique est activée, elle peut être désactivée
automatiquement après une durée configurable sans activité de contrôle à
distance. La durée par défaut est de 30 jours et se règle au format
`jj:hh:mm` dans les options.

Dans **Outils > TeleNVDA > Se connecter**, choisissez le transport, le port
et le chemin WebSocket. Pour un relais, le même serveur, chemin, port et clé
doivent être utilisés par les deux ordinateurs.

Le mode **Configuration manuelle** conserve le comportement historique. Si
aucun hôte n'est indiqué, les variables d'environnement de proxy peuvent être
utilisées par les bibliothèques réseau. Le mode **Détection automatique du
proxy Windows** suit la configuration WinHTTP de l'utilisateur, y compris les
scripts PAC/WPAD et les exclusions par destination. Il n'enregistre ni
n'extrait le mot de passe Windows. Le mode **Aucun proxy** ignore aussi les
variables d'environnement.

Les modes `negotiate` et `ntlm` authentifient TeleNVDA auprès du proxy HTTP
avant le tunnel TLS/WebSocket. Si le nom d'utilisateur est vide, la session
Windows courante est utilisée. Pour des identifiants explicites, indiquez par
exemple `DOMAINE\\utilisateur` et le mot de passe. Le relais NVDA Remote n'a
pas besoin d'être modifié et ne reçoit pas ces identifiants.

Le mode **Serveur** direct est volontairement une connexion TCP/TLS classique
sur le port choisi. Le transport WebSocket concerne les connexions à un
serveur relais compatible ; il ne transforme pas le serveur direct local en
serveur WebSocket.

## Mises à jour

TeleNVDA peut vérifier les Releases publiques du dépôt GitHub au démarrage de
NVDA. Dans **Outils > TeleNVDA > Options**, activez ou désactivez cette
vérification. Seules les versions stables sont proposées. Une vérification
manuelle est disponible avec **Outils > TeleNVDA > Check for updates**.

Une mise à jour n'est jamais installée silencieusement. TeleNVDA demande une
confirmation, télécharge le paquet `.nvda-addon` en HTTPS, vérifie son hash
SHA-256 publié, puis demande s'il faut redémarrer NVDA. La vérification utilise
le mode proxy configuré pour TeleNVDA, y compris les types HTTP, SOCKS,
`negotiate` et `ntlm`, ainsi que la détection automatique Windows lorsqu'elle
est sélectionnée. Les erreurs réseau d'une vérification automatique restent
silencieuses et sont consignées dans le journal ; les erreurs d'une vérification
manuelle sont affichées.

## Vérifier la connectivité

La commande **Outils > TeleNVDA > Connectivity test** teste la résolution
DNS, l'établissement TLS et, pour WebSocket, la négociation HTTPS. Le résultat
est présenté à l'écran et ajouté au journal local
`teleNVDA-connectivity.log`. Les mots de passe et clés de session ne sont
jamais écrits dans ce journal.

## Connexion directe

Le mode **Serveur** permet d'héberger une connexion directe sur le port 6837
par défaut. Le port peut être redirigé manuellement ou avec UPnP.

Le certificat TLS du serveur est auto-signé et généré au premier démarrage
dans le profil NVDA sous `teleNVDA-server.pem`. La clé privée n'est pas
incluse dans les sources, dans l'extension ou dans le dépôt. Lors de la
première connexion, l'empreinte SHA-256 est automatiquement enregistrée afin
de ne pas bloquer la connexion sur une demande de confirmation. Vérifiez
l'empreinte attendue avec l'administrateur du serveur avant cette connexion.

## Captures d'écran

Une session où l'utilisateur contrôle un autre ordinateur peut demander une
capture depuis le menu TeleNVDA. Deux méthodes sont disponibles :

* **Request screenshot** utilise la capture native TeleNVDA ;
* **Request screenshot (PowerShell)** fonctionne également lorsque l'ordinateur
  contrôlé utilise le Remote standard de NVDA ou le TeleNVDA d'origine, qui ne
  connaissent pas les captures d'écran.

**Problème connu :** la capture compatible décrite ci-dessous ne fonctionne pas
encore. La boîte Exécuter n'est jamais ouverte sur l'ordinateur contrôlé, donc
aucune image ne revient. Utilisez la capture native en attendant la correction.

Avec la méthode PowerShell, une capture est d'abord demandée à l'ordinateur
contrôlé. Si rien ne répond après quelques secondes, la capture est pilotée avec
les messages que le protocole standard implémente : le script de capture est
placé dans le presse-papiers de l'ordinateur contrôlé, la boîte Exécuter y lance
un PowerShell masqué qui y réécrit l'image encodée, puis la commande « pousser
le presse-papiers » de cet ordinateur la renvoie.

Cette méthode compatible a des limites connues :

* une session interactive doit être ouverte sur l'ordinateur contrôlé, et la
  capture ne fonctionne ni sur le bureau sécurisé ni sur l'écran de verrouillage ;
* le presse-papiers de l'ordinateur contrôlé est remplacé et quelques messages y
  sont annoncés pendant la capture ;
* la touche NVDA de l'ordinateur contrôlé doit comporter Insertion, car sa
  commande d'envoi du presse-papiers est déclenchée à distance ;
* PowerShell et la boîte Exécuter ne doivent pas être bloqués par une stratégie
  de sécurité.

L'image reçue est ouverte localement dans l'application associée aux fichiers
JPEG. Le dossier d'enregistrement des captures reçues est configurable dans
les options ; le dossier temporaire de l'utilisateur est utilisé par défaut.
Aucun helper Python séparé n'est installé ou publié.

## Fonctions disponibles

TeleNVDA fournit également le contrôle clavier et Braille, la parole et les
sons distants, le presse-papiers, l'envoi de fichiers, UPnP,
les liens de session, le contrôle de Ctrl+Alt+Suppr et le fonctionnement sur
le bureau sécurisé.

## Transfert de fichiers

Les deux ordinateurs se déclarent mutuellement leurs fonctions au moment de la
connexion. Lorsque les deux extrémités utilisent une version de TeleNVDA qui
prend en charge le nouveau système, le fichier est envoyé par blocs successifs,
dans les deux sens : de l'ordinateur qui contrôle vers l'ordinateur contrôlé
comme l'inverse. Une boîte de dialogue affiche alors la progression, le volume
transféré, le débit et le temps restant estimé, et permet d'interrompre le
transfert des deux côtés. La taille n'est plus limitée qu'à l'espace disque
disponible et à la limite éventuellement annoncée par l'ordinateur destinataire.
L'intégrité du fichier est vérifiée par une empreinte SHA-256 avant son
enregistrement définitif.

Lorsque l'ordinateur distant utilise le TeleNVDA d'origine ou l'accès distant
standard de NVDA, l'ancien format est utilisé et la limite de 10 Mo s'applique
de nouveau. Une option permet de dépasser cette limite avec ces ordinateurs :
elle reste compatible avec eux, mais le fichier entier est chargé en mémoire des
deux côtés et la session est bloquée pendant le transfert, elle est donc
désactivée par défaut. Une autre option limite la taille des fichiers acceptés
en réception.

## Partage d'écran

L'ordinateur qui contrôle peut afficher l'écran de l'ordinateur contrôlé et,
lorsque son utilisateur l'accepte, déplacer sa souris. **NVDA+Contrôle+Maj+V**
démarre ou arrête le partage. Le raccourci fonctionne des deux côtés :
l'ordinateur contrôleur démarre la session, et l'un ou l'autre peut y mettre
fin.

L'image circule directement entre les deux ordinateurs chaque fois que le réseau
le permet, elle ne passe donc pas par le serveur relais et ne consomme pas sa
bande passante. Lorsqu'aucune route directe n'existe, un serveur TURN annoncé
par le relais est utilisé en dernier recours. Rien n'est enregistré d'un côté ni
de l'autre.

Avant tout partage, l'ordinateur contrôlé demande son accord à son utilisateur.
Accepter autorise du même coup l'ordinateur contrôleur à utiliser la souris de
cet ordinateur, et cette réponse unique est oubliée à la fin de la session.
Aucune frappe clavier ne circule par cette liaison.

Le partage d'écran a besoin d'un navigateur Chromium installé sur les deux
ordinateurs, d'un relais démarré avec le partage d'écran activé, et des deux
ordinateurs équipés d'une version de TeleNVDA qui le prend en charge. Microsoft
Edge est utilisé lorsqu'il est présent, ce qui est le cas sur tout Windows à
jour ; Google Chrome et Brave servent de solutions de repli. Lorsqu'il manque
l'un de ces éléments, la commande le signale et rien d'autre ne change.

Le navigateur ne sert que de moteur vidéo. Sur l'ordinateur partagé, il tourne
dans une fenêtre maintenue hors de l'écran, afin de ne jamais se capturer
lui-même et de ne jamais prendre le focus à NVDA. Sur l'ordinateur contrôleur,
il affiche l'image. Aucun profil de navigation de l'utilisateur n'est touché :
un profil temporaire est créé pour la session puis supprimé.

Trois réglages ajustent l'image dans les options : le nombre maximal d'images
par seconde, la largeur maximale à laquelle l'image est réduite avant d'être
encodée, et la qualité, qui fixe le plafond de bande passante. Réduire la
largeur est de loin le moyen le plus efficace de fluidifier une session sur un
grand écran ou un ordinateur lent.

## Interprétation du clavier distant

Lorsque le clavier est rendu à l'ordinateur local, **Contrôle+Maj+F1** permet
de modifier l'interprétation des touches injectées à distance sur l'ordinateur
contrôlé. NVDA confirme chaque changement avant de l'annoncer. Dans le mode
normal, les touches distantes peuvent déclencher des gestes NVDA sur cet
ordinateur. En mode clavier brut distant, seules les touches injectées par
TeleNVDA contournent l'interprétation des gestes NVDA et atteignent Windows,
l'application au premier plan, JAWS ou Narrator. Le clavier physique, la souris
et le hook clavier global de NVDA ne sont pas désactivés. Le même geste rétablit
l'interprétation normale.

Cette commande exige exactement un ordinateur contrôlé compatible et le
contrôle du clavier local avant toute modification. Elle n'est pas disponible
sur le bureau sécurisé, avec plusieurs ordinateurs contrôlés, ni avec un pair
ancien qui n'annonce pas cette fonction. L'état est temporaire et revient au
mode normal à la fin de la connexion. Le geste reste réaffectable dans la boîte
de dialogue Gestes de commandes de NVDA.

## Son distant

L'image n'est pas toujours ce qui manque. L'ordinateur qui contrôle peut aussi
entendre ce que l'ordinateur contrôlé joue : une vidéo, une alerte, un programme
qui se met à parler tout seul. **NVDA+Contrôle+Maj+K** démarre ou arrête
l'écoute. Le son est une session à part entière et n'a besoin d'aucune image, il
peut donc servir seul ; le démarrer ne prend pas le clavier.

Ce qui circule n'est pas tout ce que joue la carte son. Windows sait distinguer
le son d'un programme de celui d'un autre, et c'est ce qui est utilisé : chaque
application est capturée séparément puis les résultats sont mélangés. Le lecteur
d'écran de l'ordinateur contrôlé n'en fait jamais partie, sa voix n'est donc pas
entendue deux fois.

Le bouton **Sources audio** des options énumère les applications entendues
pendant la session en cours et permet d'en faire taire n'importe laquelle. Seules
les applications refusées sont mémorisées, par le nom de leur programme : un
ordinateur qui assiste beaucoup d'autres postes ne constitue jamais l'inventaire
de tout ce qu'ils ont pu exécuter. Une application qui ne tourne pas peut être
désignée à l'avance avec **Ajouter une application**, et une application refusée
précédemment reste dans la liste pour que la décision puisse être annulée.

Avant toute écoute, l'ordinateur contrôlé demande son accord à son utilisateur,
et cette réponse ne vaut que pour la session. Rien n'est enregistré d'un côté ni
de l'autre.

Contrairement à l'image, le son est porté par la session elle-même : l'ordinateur
contrôlé conserve le PCM stéréo à 48 kHz jusqu'à son encodage Opus, puis l'envoie
comme un message ordinaire du protocole. Le débit cible est de 96 kbit/s et
s'adapte entre 48 et 128 kbit/s lorsque la file de transport varie. Il est donc
chiffré avec tout le reste lorsqu'un mot de passe de chiffrement est défini, il
n'a besoin d'aucun navigateur, d'aucun serveur TURN ni d'aucun relais prévu pour
le partage d'écran, et il passe partout où la session passe déjà. Un tampon de
lecture borné entre 100 et 500 ms absorbe les rafales courtes sans croissance
infinie ; sur une liaison locale stable, l'objectif est de rester sous 300 ms
entre la capture et la lecture. Rien n'est envoyé lorsqu'aucune application
sélectionnée ne joue.

Deux réglages supplémentaires sont disponibles dans les options :

* **Autoriser le partage du son de cet ordinateur, après confirmation**, qui
  désactive la fonction lorsqu'elle est décochée, sur cet ordinateur seulement ;
* **Volume du son provenant de l'autre ordinateur**, qu'il vaut mieux baisser,
  puisque ce son est joué par-dessus la parole du lecteur d'écran local.

Avant Windows 10 version 2004, Windows ne sait pas séparer le son d'un programme
de celui d'un autre. Sur un tel ordinateur, c'est toute la carte son qui est
partagée, ce qui comprend la parole du lecteur d'écran qui y tourne ; les deux
utilisateurs en sont avertis.

## Sécurité

N'utilisez pas une clé de session prévisible et ne partagez pas votre clé
avec une personne non autorisée. Les certificats non reconnus sont acceptés
et mémorisés automatiquement pour éviter de bloquer la connexion. Vérifiez
l'empreinte attendue avec l'administrateur du serveur avant la première
connexion.

Les journaux et fichiers de configuration locaux peuvent contenir des
paramètres sensibles : ne les publiez pas. En particulier, ne copiez jamais
`teleNVDA-server.pem` dans les sources ou dans un paquet distribué.

## Développement

Le projet utilise Python `>=3.13,<3.14`, SCons et gettext. Les dépendances
nécessaires au fonctionnement de l'extension sont embarquées dans
`addon/globalPlugins/remoteClient/lib32` et `lib64`.

Pour construire l'extension, installez les outils de développement requis,
puis exécutez `scons` à la racine du dépôt. Le paquet généré est un artefact
de distribution et ne doit pas être commité.

## Licence

TeleNVDA est distribué sous licence GNU GPL version 2 ou ultérieure. Consultez
[COPYING.txt](../../../COPYING.txt) et [LICENSE](../../../LICENSE).

[[!tag dev stable]]
