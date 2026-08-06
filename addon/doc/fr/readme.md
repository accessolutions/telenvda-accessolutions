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
vérification et choisissez le canal **Stable releases** ou **Development
releases**. Une vérification manuelle est disponible avec **Outils > TeleNVDA
> Check for updates**.

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
* **Request screenshot (PowerShell)** conserve la méthode bêta PowerShell
  pour les systèmes où la capture native n'est pas adaptée.

L'image reçue est ouverte localement dans l'application associée aux fichiers
PNG. Aucun helper Python séparé n'est installé ou publié.

## Fonctions disponibles

TeleNVDA fournit également le contrôle clavier et Braille, la parole et les
sons distants, le presse-papiers, l'envoi de fichiers jusqu'à 10 Mo, UPnP,
les liens de session, le contrôle de Ctrl+Alt+Suppr et le fonctionnement sur
le bureau sécurisé.

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
