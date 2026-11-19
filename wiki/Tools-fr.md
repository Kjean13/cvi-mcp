Outils
===============================================================================

Retour a l'[Accueil](Home-fr) pour installer et brancher, ou
[Fonctionnement](Internals-fr) pour la construction de l'index. In English:
[Tools](Tools).

Trente-huit outils, tous en lecture seule. Chaque appel est trace dans le
journal d'audit. L'index se met a jour avant chaque appel, en incremental:
rien a lancer a la main.

Conventions communes:

- **Code compile seulement.** Le preprocesseur est rejoue avec les `#define`
  du `.prj`, ses chemins d'inclusion et la configuration de build. Le code
  exclu par un `#if` sort des comptes, graphes et analyses;
  `include_inactive=true` le remet la ou c'est utile.
- **Resume d'abord, puis pages.** Les longues listes portent `total`,
  `offset`, `limit` et `next_offset` (`null` sur la derniere page).
- **Budget de taille.** Aucune reponse ne depasse `CVI_MCP_MAX_CHARS` (40000
  par defaut): la plus grosse liste est coupee, `truncated` dit laquelle et
  `next_offset` mene a la suite. `format="compact"` (ou
  `CVI_MCP_FORMAT=compact`) passe les listes en colonnes + lignes.
- **Un vide s'explique.** Un resultat vide dit pourquoi: filtre de fichier
  inconnu, symbole present seulement dans du code exclu, nom seulement teste
  par un `#if`, ou quelques `did_you_mean`.
- **Confiance.** Les constats heuristiques portent `confidence`: `haute`,
  `moyenne` ou `faible`, avec la raison.
- **Chemins** relatifs a la racine commune du projet et de ses sources.
  **`index_info`** termine chaque reponse: version, fichiers, date
  d'indexation, configuration de build, changement de racine eventuel.

Chaque fichier a une **origine**: `project`, `generated` (le `.h` que CVI
ecrit pour un `.uir`) ou `library` (en-tete tiers), et un `in_build`.

Environnement et projets
-------------------------------------------------------------------------------

| Outil | Ce qu'il donne |
|---|---|
| `cvi_environment()` | plateforme, installations CVI, dossiers SDK, executable de build, reglages, etat de l'index |
| `self_check()` | lecture du `.prj` (sources, chemins d'inclusion, bibliotheques absentes), includes non resolus (SDK ou vraiment absents), fichiers hors build, branches restees incertaines, erreurs d'analyse, encodages |
| `use_project(name_or_path=None)` | change de projet sans relancer; sans argument, projet courant et projets connus. Un index par projet |
| `reindex(force=False)` | reindexation incrementale ou complete, en-tetes atteints et configuration |

Lire le projet
-------------------------------------------------------------------------------

| Outil | Ce qu'il donne |
|---|---|
| `project_overview(section, scope, contains, limit, offset)` | totaux par origine, points d'entree, haut des listes. `section`: `files`, `domains`, `libraries`, `callbacks`, `static_functions`, `externals` (avec `category` libc, sdk, unknown et signature SDK), `entry_points` |
| `list_symbols(kind, file, name_contains, scope, include_inactive, format)` | symboles; `file` accepte un nom, un dossier ou un motif (`Commun/Includes/*`) |
| `find_symbol(name, exact, kind, include_inactive)` | sans casse; plusieurs mots doivent tous apparaitre (`turbine max`); abreviations usuelles (`nb` trouve `NOMBRE`) |
| `get_function(name, include_body, lines, skeleton)` | signature, taille, complexite cyclomatique et cognitive, locales, pile estimee, appelants, appeles avec signature SDK, et `touches`: globales lues et ecrites, verrous, panneaux, controles, cles INI, SQL. `skeleton` donne la structure sans aucune expression. `lines`: `N` ou `a-b` |
| `find_usages(name, kind, file, group_by, include_inactive, format)` | `call`, `read`, `write`, `ref`, plus `cond` (`#if`), `macro` (dans une autre macro), `dim` (taille de tableau), `decl` (initialiseur). `group_by`: `function` ou `file` |
| `call_graph(function, direction, depth, include_external, include_inactive)` | appelants ou appeles; les aretes par pointeur ou table portent `confidence` |
| `file_outline(file)` | un fichier: includes et leur resolution, symboles, encodage, `in_build`, garde, branches |

Recherche et impact
-------------------------------------------------------------------------------

| Outil | Ce qu'il donne |
|---|---|
| `search_text(pattern, scope, regex, file, origin)` | recherche dans `code`, `comments` ou `strings`: position, zone, fonction englobante et seul le texte trouve |
| `find_by_topic(topic)` | tout ce qui porte le nom d'un theme: fichiers, fonctions, callbacks, globales, macros, types, panneaux, controles, chaines |
| `related(name)` | meme suite numerotee, utilises ensemble, meme fichier, noms proches |
| `series(name, min_members)` | identifiants numerotes (`X1`..`Xn`): etendue, trous, fichiers qui s'arretent plus tot qu'ailleurs |
| `impact(name)` | checklist pour modifier ou etendre un symbole: definitions, `#if`, tableaux, macros derivees, fonctions avec panneaux et cles INI, suites a prolonger |
| `peek(file, line, before, after)` | quelques lignes autour d'une position; exige `CVI_MCP_ALLOW_PEEK=1`, plafonne, journalise |

Configuration de compilation
-------------------------------------------------------------------------------

| Outil | Ce qu'il donne |
|---|---|
| `config_map(scope)` | chaque macro testee par un `#if`: valeur courante ou desactivee, blocs pilotes (actifs, exclus), definition commentee a reactiver; configuration de build, `#define` du `.prj`, chemins d'inclusion |
| `macro_usages(name, defines)` | definitions (brute et evaluee), `#if` avec leur etat, fonctions, tableaux dimensionnes, macros et initialiseurs qui la citent |
| `config_diff(defines)` | what-if sans toucher aux fichiers: branches qui basculent, symboles qui apparaissent ou disparaissent, fonctions touchees, macros derivees, tableaux redimensionnes |

Interface
-------------------------------------------------------------------------------

`ui_map(panel, contains, sort, limit, offset, uir)`. Sans `panel`: une ligne
par panneau, totaux, `callbacks_reconciliation`, callbacks enregistrees par le
code (timer, thread, TCP, table de pointeurs), callbacks orphelines avec
raison et confiance, `panel_variants` (panneaux copies l'un de l'autre et leurs
ecarts), controles inutilises par type. `sort`: `name`, `controls`, `unused`,
`callbacks`, `wired`, `file`. `uir` garde un seul `.uir`. `limit=0` renvoie
tout. Avec `panel`: chaque controle, son type, sa callback et ce qu'elle
appelle.

Qualite et risques
-------------------------------------------------------------------------------

| Outil | Ce qu'il donne |
|---|---|
| `dead_code(section)` | fonctions inatteignables depuis les points d'entree (`never_called`, `only_called_by_dead_code`, `only_used_in_excluded_code`), callbacks orphelines, globales inutilisees ou ecrites jamais lues, macros, panneaux (`never_loaded`, `only_in_excluded_code`, `indirect_load_possible` avec preuve) |
| `lock_check(kind)` | verrous suivis chemin par chemin sur le code compile; fonctions enveloppes, inversions d'ordre de prise, appels bloquants verrou tenu |
| `risks(check, file, min_confidence)` | `unbounded_copy`, `format_mismatch`, `ignored_return`, `unchecked_alloc`, `leak_on_return`, `leak_at_end`, `double_release`, `ui_from_thread`, `shared_global_unlocked` |
| `consistency(check)` | `duplicate_definition`, `prototype_mismatch`, `declared_not_defined`, `could_be_static`, `extern_mismatch`, `callback_signature`, `control_on_wrong_panel` |
| `complexity(sort, file, min_risk, section)` | lignes, cyclomatique, cognitive, imbrication, parametres, locales, pile; `section`: `by_file` ou `duplicates` (fonctions copiees d'un meme modele) |
| `module_graph(file, format)` | dependances entre fichiers, cycles, paires mutuelles avec l'arete la plus legere a couper; `format`: `json`, `mermaid`, `dot` |
| `threads(global_name)` | contextes d'execution (interface, pool de threads, timers asynchrones), ce que chacun atteint, timers d'interface, globales partagees; avec `global_name`, qui l'ecrit et la lit, d'ou, avec ou sans verrou |

E/S industrielles et persistance
-------------------------------------------------------------------------------

| Outil | Ce qu'il donne |
|---|---|
| `call_sites(pattern, position)` | arguments constants passes a `Modbus*`, `*Wago*`, `MBT*`...: adresses, registres, bits, macros evaluees, fonctions appelantes |
| `persistence(kind, section)` | `ini`: sections, cles, lecteurs et ecrivains, cles ecrites jamais relues et l'inverse. `sql`: tables et requetes par fonction |

SDK et build
-------------------------------------------------------------------------------

| Outil | Ce qu'il donne |
|---|---|
| `sdk_index(paths, force)` | indexe en-tetes et panneaux de fonction NI; fait tout seul au premier besoin si CVI est installe |
| `sdk_lookup(name, exact)` | prototype, parametres et commentaire d'une fonction du SDK |
| `build_check(target, dry_run, config)` | compile avec `compile.exe`; diagnostics par fichier, ecart avec le build precedent, alerte si la version de CVI du projet n'est pas installee. `config`: `Debug`, `Release`, `Debug64`, `Release64` ou une configuration du `.prj` |

Versions
-------------------------------------------------------------------------------

| Outil | Ce qu'il donne |
|---|---|
| `snapshot_save(label)`, `snapshot_list()`, `snapshot_delete(label)` | etats nommes de l'index |
| `diff_versions(before, after, path, files)` | `headline` en quelques phrases, puis ajouts, suppressions, deplacements, changements de signature, de valeur et de corps, symboles passes entre compile et exclu, constantes d'interface et cles INI ajoutees ou retirees, appels. Cotes: un instantane, `current` ou `path:<dossier>` (autre version ou variante). `files` restreint les deux cotes |
| `history(target)` | historique git si le projet est un depot: commits sur les lignes d'une fonction, sur un fichier, ou les derniers |
