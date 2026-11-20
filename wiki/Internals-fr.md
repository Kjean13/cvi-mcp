Fonctionnement
===============================================================================

Retour a l'[Accueil](Home-fr) pour installer et brancher, ou
[Outils](Tools-fr) pour la reference des outils. In English:
[Internals](Internals).

La chaine
-------------------------------------------------------------------------------

1. **Resolution des sources.** `.cws` puis `.prj` puis, seulement si les deux
   n'ont rien rendu, un balayage du dossier. Les exclusions s'appliquent ici.
   Le `.prj` donne aussi chemins d'inclusion, `#define` du compilateur,
   configurations (32 ou 64 bits, debug ou release) et version de CVI.
2. **Preprocesseur rejoue.** Chaque `.c` du build est une unite de
   compilation; ses `#include` sont suivis et ses `#if` evalues. Les en-tetes
   atteints depuis le build et situes sous la racine du projet sont indexes.
3. **Lecture et empreinte.** Marque d'ordre des octets, puis UTF-8, puis
   cp1252, puis latin-1. L'empreinte porte sur les octets, elle ne depend donc
   pas de l'encodage retenu.
4. **Neutralisation des macros CVI.** `CVICALLBACK`, `CVIFUNC`,
   `__declspec(...)` et consorts sont remplaces par des espaces de meme
   longueur.
5. **Deux analyses** tree-sitter C: une avec les lignes exclues effacees (le
   code compile, `active=1`), une sur le texte brut pour garder ce qui ne vit
   que dans les blocs exclus (`active=0`). Lignes et colonnes restent celles
   du fichier.
6. **Extraction** des symboles, references (code, `#if`, valeurs de macros,
   tailles de tableaux, initialiseurs), arguments d'appel, metriques.
7. **Stockage** en SQLite, hors du projet, un index par projet.
8. **Service** des outils, avec la garde de sortie et le budget de taille.

Pourquoi les macros sont blanchies et non retirees
-------------------------------------------------------------------------------

`int CVICALLBACK StartCallback(...)` n'est pas du C valide pour un parseur
standard. Si on retire la macro, toutes les lignes et colonnes suivantes se
decalent, donc chaque position rapportee par le serveur serait fausse. La
remplacer par des espaces de longueur identique laisse le fichier exactement de
la meme taille, tree-sitter rend donc des positions exactes, et le texte
d'origine reste decoupable pour y chercher le marqueur `CVICALLBACK` qui
identifie une callback.

Schema de l'index
-------------------------------------------------------------------------------

| Table | Contenu |
|---|---|
| `files` | chemin, hash, lignes, taille, date, origine, encodage, `in_build`, garde, erreurs d'analyse |
| `symbols` | nom, nature, plage de lignes, signature, parametres, indicateurs, commentaire, empreinte de corps, valeur, complexite, complexite cognitive, profondeur, locales, pile estimee, empreinte de forme, `active` |
| `refs` | fichier, ligne, colonne, nom, nature (`call`, `read`, `write`, `ref`, `return`, `cond`, `macro`, `dim`, `decl`), fonction ou macro englobante, `active` |
| `call_args` | les arguments litteraux de chaque appel |
| `includes` | nom inclus, ligne, fichier resolu et comment (`dossier_du_fichier`, `include_path`, `dossier_du_projet`, non resolu) |
| `pp_blocks` | chaque branche `#if`, `#elif`, `#else`: expression, etat, confiance, raison |
| `sdk_files`, `sdk_symbols` | en-tetes et panneaux NI, espace separe |
| `snapshots`, `snapshot_symbols`, `snapshot_edges` | etats nommes pour le diff |

Le schema porte un numero de version. En cas de divergence l'index est detruit
et reconstruit plutot que migre, puisqu'il est entierement regenerable depuis
les sources.

Le preprocesseur simule
-------------------------------------------------------------------------------

Seules les directives sont relues, jamais le code: rejouer chaque unite de
compilation reste donc peu couteux. L'environnement d'une unite part de ce
que fournit le compilateur (`_CVI_` d'apres la version du `.prj`, `_WIN32`,
`_WIN64` et `_NI_mswin64_` en 64 bits, `_CVI_DEBUG_` en debug), puis les
`#define` du `.prj`, puis `.cvi-mcp.json` et `CVI_MCP_DEFINES`. Une valeur
forcee par `config_diff` l'emporte sur tout `#define` des sources.

Les expressions sont evaluees comme en C: entiers, `defined`, tous les
operateurs, macros objet developpees. Un nom non defini vaut 0; la reponse dit
alors lequel et s'il est defini ailleurs dans le projet, signe habituel d'un
probleme d'ordre des includes. Une macro fonction ou une expression illisible
laisse la branche `unknown`: les deux variantes restent dans l'index, comme
avant la 0.3.2.

Un en-tete inclus par plusieurs unites garde, pour chaque branche, l'etat le
plus vivant vu: compile dans une unite suffit pour compter comme compile.
`varies` signale une branche dont l'etat depend de l'unite. Gardes
d'inclusion et `#pragma once` sont reconnus. Avant l'analyse, les lignes
exclues et les directives des chaines entierement tranchees sont effacees, ce
qui repare aussi le classique `#if` qui coupe un `if` en deux.

Comment `ui_map` deduit le cablage
-------------------------------------------------------------------------------

Un `.uir` est binaire et n'est jamais lu. Mais CVI regenere a chaque sauvegarde
un `.h` a cote, reconnaissable a sa banniere: les panneaux, puis chaque
controle avec son type et la callback cablee dans l'editeur, en commentaire.
Quand ces en-tetes sont indexes, ils font foi pour les panneaux, les controles
et le cablage de l'editeur. Le code apporte le reste: ou les panneaux sont
charges, quels controles sont lus ou ecrits, les callbacks installees a
l'execution.

Sans ces en-tetes, la carte retombe sur la deduction. Les macros et enumerateurs entiers sont des constantes d'interface candidates.
Une constante est un panneau quand une autre constante en derive par prefixe,
comme `PANEL_MAIN_START` derive de `PANEL_MAIN`, ou quand elle est passee a une
fonction dont le nom contient `panel`. Tout le reste est un controle, rattache
au plus long prefixe de panneau qui correspond.

Ensuite, dans un appel quelconque: un argument qui est une constante
d'interface est une cible, un argument qui est le nom d'une fonction du projet
est une callback. Les deux dans le meme appel donnent un cablage; une cible
seule est un acces; une callback seule est un cablage dont la cible n'a pas pu
etre identifiee, et c'est rapporte a part.

C'est pour cela qu'aucune liste de fonctions de l'API CVI n'apparait dans le
code. Cela marche pour `InstallCtrlCallback`, pour les menus, et pour des
fonctions dont l'auteur n'a jamais entendu parler.

L'heuristique `.fp`
-------------------------------------------------------------------------------

Le format des panneaux de fonction n'est pas documente. L'indexeur extrait les
identifiants imprimables des octets et ne garde que ceux qu'un en-tete a deja
confirmes comme prototype. Les candidats non confirmes sont comptes et ecartes,
jamais presentes comme une API. Le gain: savoir quelles fonctions du SDK sont
exposees dans un panneau de fonction, ce qui separe l'API publique des
declarations internes.

Comparer sans stocker de code
-------------------------------------------------------------------------------

Un instantane contient, par symbole, sa signature, sa valeur et une empreinte
de son corps. L'empreinte retire les commentaires de bloc et de ligne, ecrase
tous les espaces, et hache le resultat. Un fichier reformate ou un commentaire
reecrit donnent la meme empreinte; une expression changee non. CRLF et LF
s'ecrasent sur la meme valeur, donc un fichier repasse sous un autre editeur
n'est pas signale comme modifie.

Les macros et enumerateurs n'ont ni signature ni corps, ils sont donc compares
sur leur valeur. Sans cela, un identifiant de controle passant de 3 a 4
casserait l'interface sans apparaitre nulle part.

Origine d'un fichier
-------------------------------------------------------------------------------

Sur un vrai projet la plupart des macros ne viennent pas de l'equipe: sur les
9 842 macros de CL150, 7 508 viennent des en-tetes que CVI genere pour ses
`.uir` et 1 006 d'en-tetes de bibliotheques (735 du seul `nivision.h`).
L'equipe en a ecrit 1 328. Chaque fichier est donc etiquete:

- `generated` quand il commence par la banniere que CVI ecrit dans l'en-tete
  d'un `.uir`;
- `library` quand c'est un en-tete qui declare des prototypes dont aucun n'est
  defini dans le projet, ou un en-tete absent du `.prj`, atteint par un
  `#include`, dans un dossier sans aucun `.c` du projet et sans prototype
  implemente;
- `project` sinon.

La seconde regle n'a besoin d'aucune liste d'en-tetes de fournisseurs: un
en-tete est du code de bibliotheque parce que le projet ne fait qu'appeler ce
qu'il declare. Un appel a une fonction seulement declaree dans un tel en-tete
compte comme externe, au meme titre qu'un appel SDK.

Suivre verrous et ressources
-------------------------------------------------------------------------------

`lock_check` et les fuites de `risks` partagent un meme parcours chemin par
chemin, sur le code compile. `lock_check` reanalyse les fonctions qui prennent ou rendent un verrou et
parcourt leurs instructions en gardant, pour chaque verrou, combien de fois il
est tenu. Un verrou est nomme par sa fonction de liberation et son premier
argument, `CmtReleaseLock(gLock)`: deux verrous d'une meme fonction sont suivis
separement.

A un `if`, les deux branches partent du meme etat et sont fusionnees ensuite en
gardant le pire compte. Le corps d'une boucle est fusionne avec l'etat d'avant,
et un `break` emporte son etat hors de la boucle. Chaque `case` d'un `switch`
part de l'etat d'avant le switch. Une branche restee `unknown` pour le
preprocesseur est suivie comme deux variantes. Un `return` atteint avec un
compte positif est un `return_while_held`, y compris un `return` cache dans une
macro.

Une fonction qui garde le verrou recu en parametre, ou rend un verrou qu'elle
n'a pas pris, est une enveloppe: ses appels comptent comme prise ou
liberation chez l'appelant. Deux verrous pris dans les deux ordres sont
signales comme inversion, et une popup, une lecture TCP ou
`ProcessSystemEvents` verrou tenu comme appel bloquant.

Ou un verrou est tenu
-------------------------------------------------------------------------------

`threads` et les alertes `shared_global_unlocked`, `inconsistent_lock` et
`ui_control_unlocked` de `risks` doivent savoir si un verrou est tenu a un
acces precis, pas seulement si la fonction en prend un. Un enregistreur branche
sur le meme parcours note, pour chaque instruction, les verrous tenus apres
elle, par (ligne, colonne): `CmtGetLock(l); x = 1; CmtReleaseLock(l);` sur une
seule ligne ne s'annule pas. Apres un `return` anticipe qui rend le verrou, la
suite de la fonction reste sous verrou.

Une fonction herite aussi des verrous tenus a tous ses appels: `Seuillage`
appelle `LabelFunction` sous `giVerrouVinci`, donc `LabelFunction` et ce
qu'elle appelle tiennent `giVerrouVinci`. L'heritage est un plus petit point
fixe: une boucle d'appels ne se protege pas elle-meme. Une fonction que rien
n'appelle, un callback, une fonction lancee dans un thread, `main` ou une
fonction dont l'adresse est prise peut etre appelee de l'exterieur et n'herite
de rien. Deux reponses en sortent: les verrous tenus a coup sur (ce que
comparent `inconsistent_lock` et `ui_control_unlocked`), et la presence d'un
verrou quelconque, meme different a chaque appel.

`inconsistent_lock` signale une globale ecrite sous un verrou ici et un autre
ailleurs, depuis deux contextes qui tournent ensemble. `ui_control_unlocked`
prend, pour chaque constante de controle (`TELE_CANVAS`), le verrou que prend
le plus de fonctions autour d'elle et signale les acces qui ne le tiennent pas:
tenir un autre verrou, comme `giVerrouVinci`, n'exclut pas l'affichage qui
prend `giVerrouTele`. Deux fonctions static de meme nom dans deux fichiers ne
sont pas distinguees.

Le verrou qui protege une donnee s'apprend dans le code. Pour une globale, c'est
le verrou que prennent le plus de fonctions autour de ses acces, a condition
qu'au moins deux fonctions le prennent et qu'elles soient au moins 40 % de
celles qui touchent la globale (`USUAL_LOCK_SHARE`): en dessous, le verrou est
tenu par hasard. Un acces qui ne le tient pas est un `lock_gap`, qui remplace
`shared_global_unlocked` pour cette globale. Les fonctions comptent, pas les
lignes, et les verrous herites ne pesent pas: la question est quel verrou les
auteurs prennent a dessein.

Les ressources suivent le meme parcours: un handle range dans une variable
locale doit etre libere, renvoye ou range hors de la fonction sur chaque
chemin. Un test comme `p == NULL`, `!p`, `h < 0`, ou `status == 0` sur le code
retour de la prise, retire la ressource de la branche ou elle n'existe pas. Un
`goto` arrete le parcours; les alertes portent alors `confidence: moyenne`.

Un handle qui sort de la fonction est suivi a travers les fonctions du projet.
Chaque fonction recoit un resume: les parametres qu'elle libere (directement,
ou en les confiant a une fonction qui le fait), ceux qu'elle garde (ranges
ailleurs ou renvoyes), et les familles de ressource qu'elle renvoie. Un appel
fait alors ce que dit le resume: liberer le handle, le faire echapper, le
laisser suivi (l'appelee ne fait que l'utiliser, donc une fuite apres l'appel
se voit), ou demarrer le suivi d'une nouvelle ressource quand l'appelee en
renvoie une, comme un `fopen` enrobe. Les resumes se recalculent jusqu'a ne
plus bouger (quatre tours au plus); une fonction sans resume, ou hors du
projet, n'est pas supposee prendre la propriete: les appels a la bibliotheque,
comme `fprintf`, ne font qu'emprunter. Les resumes sont gardes avec le projet.

Trois autres verifications utilisent les memes arbres. `unread_return` suit un
code de retour range dans une variable: jamais lu dans la fonction, c'est
`haute`, ecrase avant d'etre lu `moyenne`; une valeur utilisee dans la meme
expression, ou relue au tour suivant d'une boucle, est laissee. `uninitialized_use`
signale une locale dont la premiere occurrence est une lecture; une ecriture,
`&x` (l'appele la remplit), `va_start`, un tableau ou un `static` la met hors de
cause, donc une initialisation qui ne couvre que certaines branches n'est pas
vue.

Performance
-------------------------------------------------------------------------------

L'index est rafraichi avant chaque appel d'outil. Pour que ce soit peu couteux,
un fichier dont la taille et la date de modification n'ont pas bouge n'est pas
rouvert du tout. Des que l'un des deux a change, le fichier est lu et hache, et
le hash tranche. Cela evite qu'une restauration d'archive, qui ne preserve ni
l'un ni l'autre de facon fiable, passe inapercue.

Le projet assemble (sources, etat du preprocesseur) est aussi garde d'un appel
a l'autre. Il est reutilise tant que rien de ce dont il depend n'a bouge: les
reglages, la liste des sources du `.prj`, et la taille et la date de chaque
fichier lu par le preprocesseur, en-tetes du SDK compris. Le moindre
changement, ou `reindex`, le reconstruit. `CVI_MCP_INDEX_UNLISTED=1`
desactive la reutilisation.

Sur CL150 (74 fichiers, 74 000 lignes, 87 avec les en-tetes atteints) un
premier appel prend trois a cinq secondes, puis un appel coute moins d'un
dixieme de seconde. Les analyses qui reanalysent des fonctions (`lock_check`,
`risks`, `threads`, `consistency`) restent sous trois secondes,
`cvi_environment` autour de quatre car il parcourt les dossiers d'installation.

Limites connues
-------------------------------------------------------------------------------

- **`.prj` et `.cws`**: valides sur des projets CVI 2019, y compris les chemins
  coupes sur plusieurs lignes et les sources hors du dossier du projet.
  `self_check` montre ce qui a ete lu.
- **Preprocesseur**: les en-tetes du SDK et de Windows ne sont pas suivis, une
  macro qu'ils definissent se lit donc non definie (la reponse le dit). Une
  macro fonction dans un `#if` laisse la branche `unknown`.
- **Collisions de noms**: l'index est clef par nom. Deux fonctions `static`
  homonymes dans deux fichiers sont confondues par `find_usages` et
  `call_graph`.
- **`.uir`**: binaire, jamais lu. Son `.h` genere porte panneaux, controles,
  types et callbacks; attributs, libelles, mise en page et type de donnee d'un
  controle restent invisibles, donc les incoherences de type de `GetCtrlVal`
  ne sont pas verifiees.
- **Le code mort** suit les noms: une fonction atteinte seulement par un
  pointeur calcule parait morte; une fonction dont l'adresse est prise compte
  comme vivante. D'ou `confidence`.
- **Les risques sont heuristiques**: tailles de tampons lues seulement dans les
  declarations locales et globales `char`, bornes de `sprintf` sur des valeurs
  flottantes raisonnables (un champ numerique compte sa largeur quand le format
  la fixe, `%02d`), ressources suivies dans les variables locales seulement.
  Un verrou est suivi par instruction et d'appelant en appelant, mais deux
  verrous differents ne s'excluent jamais, et un verrou pris par une fonction
  que l'outil ne reconnait pas comme enveloppe n'est pas vu.
- **Non couvert**: tables de traduction (fichiers de langue), bornes des
  tableaux d'E/S face aux limites des protocoles, constantes magiques.

Depannage
===============================================================================

Quand un resultat sur le code surprend, lance `self_check`: entrees du `.prj`
non lues, includes absents, branches que le preprocesseur n'a pas pu trancher,
fichiers qui s'analysent mal.

Commence par `cvi-mcp --check`, ou l'outil `cvi_environment`. Il rapporte
ce qui est reellement sur la machine et porte une liste `warnings` qui
nomme en general le probleme directement.

Aucune installation CVI detectee
-------------------------------------------------------------------------------

Attendu sous Linux, ou il n'y a rien a trouver. Sous Windows, cela veut dire
que rien n'a correspondu a `CVI*` sous les racines National Instruments. Le
millesime n'est jamais code en dur, donc c'est une question d'emplacement et
non de version.

Regle explicitement les deux choses qui dependent de l'installation:

```bat
set CVI_MCP_SDK_PATHS=C:\Program Files (x86)\National Instruments\CVI2019\include
set CVI_MCP_BUILD_CMD=C:\Program Files (x86)\National Instruments\CVI2019\compile.exe
```

L'analyse statique n'a besoin ni de l'une ni de l'autre. Seuls `sdk_index`,
`sdk_lookup` et `build_check` en dependent.

`build_check` dit que l'executable est indisponible
-------------------------------------------------------------------------------

NI documente le compilateur en ligne de commande sous le nom `compile.exe`
(`cvicc` sous Linux), a cote de `cvi.exe`. Le serveur le cherche dans le PATH
puis sous chaque installation detectee, et s'arrete au lieu de deviner. Pointe
`CVI_MCP_BUILD_CMD` dessus s'il est ailleurs.

`compile.exe projet.prj` construit la cible release. Pour une autre, utilise
`CVI_MCP_BUILD_ARGS`, un gabarit ou `{project}` et `{target}` sont
substitues: `{project} -debug`, `{project} -debug64` ou
`{project} -config="Debug64"`. Le build ecrit ses sorties a cote du projet,
comme dans l'environnement. La reponse renvoie toujours la ligne de commande
exacte qui a ete lancee.

`project_overview` renvoie peu ou pas de fichiers
-------------------------------------------------------------------------------

Regarde `project_file_note` dans la reponse. Si le `.prj` n'a rendu aucune
source, le serveur est retombe sur un balayage du dossier et liste les sections
qu'il a trouvees. Envoie ces sections et le lecteur pourra etre ajuste.

Verifie aussi `CVI_MCP_EXCLUDE`. Les motifs s'appliquent au chemin relatif a la
racine comme au nom de fichier nu, donc un motif comme `*.h` est plus large
qu'il n'en a l'air.

Un fichier que je viens d'ajouter n'apparait pas
-------------------------------------------------------------------------------

La liste des sources vient du `.prj`. Un fichier pose sur le disque que le
projet ne declare pas n'est pas indexe, et n'apparait donc pas non plus dans un
diff. Ajoute-le au projet, ou pointe `CVI_MCP_PROJECT` sur un dossier sans
`.prj` pour forcer le balayage.

Les commentaires accentues reviennent mutiles
-------------------------------------------------------------------------------

Ils ne devraient pas. Le decodage essaie la marque d'ordre des octets, puis
UTF-8, puis cp1252, puis latin-1. Si un commentaire reste faux, le fichier est
dans un encodage qu'aucun de ceux-la ne couvre; ouvre une issue avec les
premiers octets de la ligne.

`ui_map` ne renvoie aucun panneau
-------------------------------------------------------------------------------

Il a besoin que le `.h` genere par l'editeur CVI fasse partie des sources
indexees, ce qui est le cas quand le `.prj` le declare. Si le fichier de constantes est exclu, ou n'est pas declare dans le
`.prj`, il n'y a aucune constante d'interface a classer. Verifie que
`list_symbols(kind="macro")` montre bien tes constantes `PANEL_*`.

Un panneau sans constante derivee et jamais passe a une fonction de panneau est
classe comme un controle, pas comme un panneau. C'est la limite d'une deduction
qui ne lit pas le `.uir`.

`sdk_lookup` ne trouve rien
-------------------------------------------------------------------------------

L'index SDK est separe et n'est pas construit automatiquement. Lance
`sdk_index` une fois. La reponse a une recherche infructueuse inclut l'etat de
l'index et le dit.

L'index semble perime ou faux
-------------------------------------------------------------------------------

`reindex(force=True)` reconstruit entierement l'index projet. Supprimer
`~/.cvi-mcp/index.db` est tout aussi sur: l'index est entierement regenerable
et ne contient rien que tu ne puisses recalculer. Les instantanes vivent dans
ce meme fichier, les supprimer les perd.

Le client ne voit pas le serveur
-------------------------------------------------------------------------------

Verifie que `cvi-mcp` se resout dans le PATH qu'utilise le client, qui n'est
souvent pas celui de ton shell, en particulier dans un environnement virtuel.
La forme fiable est le chemin complet du script, ou `python -m cvi_mcp.server`.

Pour OpenCode en particulier, trois formes different des autres clients et
toutes echouent en silence: les serveurs vont sous `mcp.servers`, la commande
est un tableau, et le bloc de variables s'appelle `environment` et non `env`.
Confirme avec `opencode mcp list`.
