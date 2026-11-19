Accueil
===============================================================================

`cvi-mcp` est un serveur MCP en lecture seule pour projets LabWindows/CVI. Il
indexe les sources C dans une base SQLite hors du projet et expose trente-huit
outils, pour qu'un agent reponde a des questions sur un code qu'il decouvre.

Il existe pour une situation precise: tu ouvres un projet CVI inconnu et tu
dois savoir qui appelle quoi, quel bouton declenche quelle callback, et ce qui
a change depuis la derniere version, sans lire cinquante fichiers a la main.

Trois pages: celle-ci pour demarrer, [Outils](Tools-fr) pour les parametres et
retours exacts, [Fonctionnement](Internals-fr) pour la mecanique, ce qu'elle
rate et quoi faire quand ca deraille. In English: [Home](Home).

Installer
-------------------------------------------------------------------------------

Python 3.10 ou plus recent. L'analyse statique tourne sous Windows et Linux; la
detection de l'installation CVI et `build_check` demandent Windows.

Windows:

```bat
py -m venv .venv
.venv\Scripts\activate
pip install -e .
cvi-mcp --check
```

Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cvi-mcp --check
```

`cvi-mcp --check` rapporte ce que la machine offre reellement et non ce que la
documentation suppose: installations CVI detectees quel que soit le millesime,
dossiers d'en-tetes du SDK, executable de build, reglages actifs, etat de
l'index et avertissements. Il sort en code non nul des qu'un avertissement est
present, donc il s'utilise aussi dans un script. A lancer avant tout le reste.

Brancher un client
-------------------------------------------------------------------------------

### Claude Code

Portee projet, fichier `.mcp.json` a la racine du depot:

```json
{
  "mcpServers": {
    "cvi": {
      "command": "cvi-mcp",
      "env": {
        "CVI_MCP_PROJECT": "C:\\projets\\appli",
        "CVI_MCP_ALLOW_BODY": "1"
      }
    }
  }
}
```

Ou en ligne de commande. Les options passent avant le nom et `--` separe la
commande du serveur:

```bat
claude mcp add --scope user --env CVI_MCP_PROJECT=C:\projets\appli --transport stdio cvi -- cvi-mcp
```

Portees: `local` par defaut et privee au projet, `project` partagee via
`.mcp.json`, `user` partout.

### OpenCode

`opencode.json` du projet, ou `~/.config/opencode/opencode.json`. Trois
differences avec la forme precedente, et les trois echouent en silence si tu te
trompes: les serveurs vont sous `mcp.servers`, la commande est un tableau, et
le bloc de variables s'appelle `environment`.

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "servers": {
      "cvi": {
        "type": "local",
        "command": ["cvi-mcp"],
        "environment": {
          "CVI_MCP_PROJECT": "C:\\projets\\appli",
          "CVI_MCP_ALLOW_BODY": "1"
        }
      }
    }
  }
}
```

Les versions anterieures placent le serveur directement sous `mcp` et utilisent
`enabled` au lieu de `disabled`. Verifier avec `opencode mcp list`.

### Claude Desktop

```json
{
  "mcpServers": {
    "cvi": {
      "command": "cvi-mcp",
      "env": { "CVI_MCP_PROJECT": "C:\\projets\\appli" }
    }
  }
}
```

Si `cvi-mcp` n'est pas dans le PATH du client, qui n'est souvent pas celui de
ton shell, donne le chemin complet du script ou utilise `"command": "python"`
avec `"args": ["-m", "cvi_mcp.server"]`.

Configuration
-------------------------------------------------------------------------------

| Variable | Defaut | Role |
|---|---|---|
| `CVI_MCP_PROJECT` | `.` | dossier du projet, ou directement son `.prj` / `.cws` |
| `CVI_MCP_PROJECTS` | vide | projets nommes pour `use_project`: `App=C:\\a;Autre=D:\\b` |
| `CVI_MCP_DB` | `~/.cvi-mcp/index-<hash>.db` | index SQLite, un par projet |
| `CVI_MCP_SDK_DB` | `~/.cvi-mcp/sdk.db` | index du SDK, commun a tous les projets |
| `CVI_MCP_AUDIT_LOG` | `~/.cvi-mcp/audit.jsonl` | journal des appels |
| `CVI_MCP_EXCLUDE` | vide | motifs exclus, separes par `;` |
| `CVI_MCP_ALLOW_BODY` | `0` | `1` autorise la sortie des corps |
| `CVI_MCP_ALLOW_PEEK` | `0` | `1` autorise les extraits courts de `peek` |
| `CVI_MCP_MAX_SNIPPET_LINES` | `40` | plafond de lignes par extrait |
| `CVI_MCP_BUILD_CONFIG` | premiere Debug | configuration du `.prj` rejouee par le preprocesseur et `build_check` |
| `CVI_MCP_DEFINES` | vide | macros ajoutees ou forcees: `OPTION=1;AUTRE` |
| `CVI_MCP_INDEX_UNLISTED` | `0` | `1` indexe aussi les `.c` et `.h` du dossier absents du `.prj` (`in_build: false`) |
| `CVI_MCP_MAX_CHARS` | `40000` | budget de taille d'une reponse |
| `CVI_MCP_FORMAT` | `json` | `compact`: listes en colonnes et lignes |
| `CVI_MCP_AUDIT_PATHS` | `1` | `0` remplace les noms de fichiers par une empreinte dans le journal |
| `CVI_MCP_SETTINGS` | `.cvi-mcp.json` | fichier de reglages du projet |
| `CVI_MCP_SDK_PATHS` | auto | dossiers du SDK NI, separes par `;` |
| `CVI_MCP_BUILD_CMD` | auto | chemin de l'executable de build CVI |
| `CVI_MCP_BUILD_ARGS` | `{project}` | gabarit d'arguments, `{project}` et `{target}` |
| `CVI_MCP_BUILD_TIMEOUT` | `300` | delai maximal du build, en secondes |

Les exclusions s'appliquent au chemin relatif a la racine comme au nom de
fichier nu, donc `vendor/*` et `*_generated.c` fonctionnent tous les deux.
Utile pour un arbre tiers que tu ne veux pas indexer, ou pour des fichiers
qu'aucun agent ne doit lire.

Un `.cvi-mcp.json` facultatif a cote du `.prj` ajuste les analyses d'un
projet. Chaque cle est facultative:

```json
{
  "build_config": "Release64",
  "defines": {"NOMBRE_MAX": "12"},
  "domains": {"camera": ["*Camera*", "Pylon*"], "automate": ["*Wago*", "*Modbus*", "MBT*"]},
  "resource_pairs": [["PylonDeviceOpen", "PylonDeviceClose", "arg0"]],
  "checked_calls": ["MonDriverEcrit"],
  "thresholds": {"lines": [100, 200, 400], "cyclomatic": [10, 20, 50]}
}
```

`resource_pairs` ajoute des fonctions de prise et de liberation au suivi des
fuites; le troisieme champ dit ou est le handle: `return` (resultat affecte)
ou `argN`.

Ce qui sort reellement
-------------------------------------------------------------------------------

Avec les reglages par defaut, un agent voit que `StartCallback` existe dans
`main.c` ligne 13, prend six parametres, n'est appelee par personne, et appelle
`CommsInit` puis `SetCtrlVal`. Il ne voit pas une seule ligne du corps.

C'est suffisant pour naviguer et cartographier une architecture. C'est
insuffisant pour reperer un bug a l'interieur d'une fonction. Mets
`CVI_MCP_ALLOW_BODY=1` quand tu veux une vraie analyse, garde le plafond de
lignes a une valeur qui te convient, et lis `~/.cvi-mcp/audit.jsonl` des que tu
veux savoir precisement ce qui est sorti: chaque appel y figure avec le volume
renvoye et les lignes de code qui ont quitte la machine.

`build_check` est le seul outil qui lance quelque chose. Il appelle la commande
de build CVI et analyse la sortie. Il n'applique aucune correction et ne touche
a aucune source, mais une compilation produit ses fichiers de sortie habituels.
