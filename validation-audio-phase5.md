# Validation audio Opus - phase 5

Date du controle automatise : 10/09/2026

Cette phase separe les controles reproductibles dans le depot des essais qui
necessitent deux postes Windows equipes de NVDA. La phase 5 ne doit pas etre
consideree comme entierement valide tant que la matrice manuelle n'est pas passee.

## Controles automatises realises

Depuis `D:\VS-Code\projets\NVDA-Remote\Client` :

- `.venv\Scripts\python.exe -m pytest tests -q` : 17 tests reussis ;
- `.venv\Scripts\python.exe -m compileall -q addon tests` : reussi ;
- Ruff cible sur les modules audio et leurs tests : aucun diagnostic ;
- `scons` : paquet genere avec les DLL Opus presentes ;
- les DLL `native\x86\opus.dll` et `native\x64\opus.dll` sont presentes ;
- `native\opus-LICENSE.txt` accompagne les DLL ;
- la CI execute maintenant la suite `tests` et `compileall` sur Linux et Windows,
  en plus du test aller-retour du wrapper.

Ces controles couvrent la validation du protocole, le round-trip Opus local, le
melange stereo, le tampon temporel, la dissimulation de pertes et la construction
du paquet. Ils ne prouvent pas le bon fonctionnement d'une session NVDA reelle.

## Matrice manuelle obligatoire

| Essai | Statut | Resultat attendu |
|---|---|---|
| NVDA x86 avec DLL x86 | A executer | Capacite Opus annoncee, session utilisable, aucune erreur NVDA |
| NVDA x64 avec DLL x64 | A executer | Capacite Opus annoncee, session utilisable, aucune erreur NVDA |
| Transport TCP/TLS brut | A executer | Negociation puis audio sans perte de session |
| Transport WebSocket TLS sur 443 | A executer | Negociation puis audio via `wss://` |
| Reseau local | A executer | Latence capture-lecture inferieure a 300 ms |
| Bonne liaison Internet | A executer | Lecture continue, debit proche du palier adapte |
| Liaison limitee et file chargee | A executer | Abandons bornes, baisse a 48/64 kbit/s, pas de croissance memoire |
| Firefox et Chrome | A executer | Sources visibles, exclusion immediate et audio correct |
| Lecteur multimedia | A executer | Stereo conservee, musique et transitoires intelligibles |
| Sons Windows | A executer | Notifications audibles sans blocage |
| Synthese tierce | A executer | Voix intelligible et comportement conforme au consentement |
| Comparaison d'ecoute a l'aveugle | A executer | Preference Opus sur musique, videos et sons systeme |
| Canal de test | A executer | Publication prerelease avec journaux detailles, avant stable |

## Procedure de recette

1. Installer le paquet construit sur deux postes de test, un scenario x86 et un
   scenario x64. Ne jamais utiliser un poste de production pour la premiere
   recette.
2. Connecter les deux postes avec le meme relais, la meme cle et, si utilise,
   le meme mot de passe AES-GCM. Executer ensuite la recette TCP/TLS puis la
   recette WebSocket sur 443.
3. Rejouer les memes extraits de voix, musique, video, notification et silence
   sur le reseau local, une bonne liaison Internet et une liaison volontairement
   limitee.
4. Verifier successivement Firefox, Chrome, un lecteur multimedia, un son
   Windows et une synthese tierce. Dans la boite Sources audio, exclure puis
   retablir chaque application et verifier l'effet pendant la session.
5. Relever dans les journaux NVDA le codec, le debit, la profondeur du tampon,
   les sous-alimentations, les abandons et les changements de sources. Ne pas
   publier de cle, mot de passe ni charge audio.
6. Faire la comparaison a l'aveugle avec le chemin historique, a niveau sonore
   egal et sur les memes extraits. Conserver la grille de resultat hors du
   paquet.
7. Publier uniquement une version prerelease sur le canal de test. La release
   stable reste bloquee tant que chaque essai critique n'a pas un resultat et un
   journal associe.

## Etat

La validation automatisee de la phase 5 est passee. Les essais manuels Windows,
reseau, qualite d'ecoute et publication de test restent a executer avec deux
postes NVDA et un relais accessible. Ce document devient le compte rendu a
completer avant la premiere publication stable Opus.
