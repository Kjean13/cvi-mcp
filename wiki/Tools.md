Tools
===============================================================================

Back to [Home](Home) to install and connect, or [Internals](Internals) for
how the index is built. En francais: [Outils](Tools-fr).

Forty tools, all read-only. Every call is written to the audit log. The
index is refreshed before each call, incrementally: nothing to launch by hand.

Conventions that hold everywhere:

- **Compiled code only.** The preprocessor is replayed with the `.prj`
  defines, include paths and build configuration. Code excluded by an `#if` is
  left out of counts, graphs and analyses; `include_inactive=true` brings it
  back where it makes sense.
- **Summary first, then pages.** Long lists come with `total`, `offset`,
  `limit` and `next_offset` (`null` on the last page).
- **Size budget.** No answer exceeds `CVI_MCP_MAX_CHARS` (40000 by default):
  the largest list is cut, `truncated` says which one and `next_offset` still
  leads to the rest. When the cut list holds items with a `file`, `truncated`
  keeps `by_file`, the count per file for the whole list, so the overview
  survives the cut. `format="compact"` (or `CVI_MCP_FORMAT=compact`) turns
  lists into columns plus rows.
- **Empty means explained.** An empty result says why: unknown file filter,
  symbol only in excluded code, name only tested by an `#if`, a symbol that
  is defined but never used, or a few `did_you_mean` names (never the name you
  searched). An empty pattern in `search_text` or `call_sites` is refused.
- **Confidence.** Heuristic findings carry `confidence`: `haute`, `moyenne`
  or `faible`, with the reason.
- **Paths** are relative to the common root of the project and its sources.
  **`index_info`** closes every answer: version, files, last index date,
  build configuration, root change if any.

Files have an **origin**: `project`, `generated` (the `.h` CVI writes for a
`.uir`) or `library` (third-party header). They also carry `in_build`.

Environment and projects
-------------------------------------------------------------------------------

| Tool | What it gives |
|---|---|
| `cvi_environment()` | platform, CVI installations, SDK folders, build executable, settings, index state |
| `self_check()` | `.prj` reading (sources, include paths, missing libraries), unresolved includes (SDK or really missing), files outside the build, branches left undecided, parse errors, encodings |
| `use_project(name_or_path=None)` | switch project without restarting; without argument, current and known projects. One index per project |
| `reindex(force=False)` | incremental or full reindex, with headers reached and configuration |

Reading the project
-------------------------------------------------------------------------------

| Tool | What it gives |
|---|---|
| `project_overview(section, scope, contains, limit, offset)` | totals per origin, entry points, top lists. `section`: `files`, `domains`, `libraries`, `callbacks`, `static_functions`, `externals` (with `category` libc, sdk, unknown and SDK signature), `entry_points` |
| `list_symbols(kind, file, name_contains, scope, include_inactive, format)` | symbols; `file` accepts a name, a folder or a pattern (`Commun/Includes/*`) |
| `find_symbol(name, exact, kind, include_inactive)` | case-insensitive; several words must all appear (`turbine max`); common abbreviations match (`nb` finds `NOMBRE`) |
| `get_function(name, include_body, lines, skeleton)` | signature, size, cyclomatic and cognitive complexity, locals, stack estimate, callers, callees with SDK signature, and `touches`: globals read and written, locks, panels, controls, INI keys, SQL. `skeleton` gives the control structure without any expression. `lines`: `N` or `a-b` |
| `find_usages(name, kind, file, group_by, include_inactive, format)` | `call`, `read`, `write`, `ref`, plus `cond` (`#if`), `macro` (inside another macro), `dim` (array size), `decl` (initializer). `group_by`: `function` or `file` |
| `call_graph(function, direction, depth, include_external, include_inactive)` | callers or callees; edges through pointers or tables carry `confidence`. `name` is accepted for `function`; an unknown `direction` is an error |
| `file_outline(file)` | one file: includes with how they were resolved, symbols, encoding, `in_build`, guard, branches |

Search and impact
-------------------------------------------------------------------------------

| Tool | What it gives |
|---|---|
| `search_text(pattern, scope, regex, file, origin)` | text search in `code`, `comments` or `strings`: position, zone, enclosing function and the matching text only |
| `find_by_topic(topic)` | everything named after a theme: files, functions, callbacks, globals, macros, types, panels, controls, string literals |
| `related(name)` | same numbered series, used together, same file, similar names |
| `series(name, min_members)` | numbered identifiers (`X1`..`Xn`): range, gaps, and files that stop earlier than elsewhere, judged apart for definitions and usages (`in`). A `name` that matches nothing returns nothing |
| `impact(name)` | checklist to change or extend a symbol: definitions, `#if`, arrays, derived macros, functions with their panels and INI keys, numbered series to extend |
| `peek(file, line, before, after)` | a few lines around a position; requires `CVI_MCP_ALLOW_PEEK=1`, capped, logged |

Build configuration
-------------------------------------------------------------------------------

| Tool | What it gives |
|---|---|
| `config_map(scope)` | every macro tested by an `#if`: current value or off, blocks driven (active, excluded), commented-out definition to turn it on; build configuration, `.prj` defines, include paths |
| `macro_usages(name, defines)` | definitions (raw and evaluated), `#if` with state, functions, arrays sized, macros and initializers citing it |
| `config_diff(defines)` | what-if without touching files: branches switched, symbols appearing or disappearing, functions affected, derived macros, arrays resized |

User interface
-------------------------------------------------------------------------------

`ui_map(panel, contains, sort, limit, offset, uir)`. Without `panel`: one line
per panel, totals, `callbacks_reconciliation`, callbacks registered by code
(timer, thread, TCP, pointer table), unwired callbacks with reason and
confidence, `panel_variants` (panels that are copies of each other and what
differs), unused controls by type. `sort`: `name`, `controls`, `unused`,
`callbacks`, `wired`, `file`. `uir` keeps one `.uir`. `limit=0` returns all.
With `panel`: each control, its type, its callback and what it calls.

Quality and risks
-------------------------------------------------------------------------------

| Tool | What it gives |
|---|---|
| `dead_code(section)` | functions unreachable from entry points (`never_called`, `only_called_by_dead_code`, `only_used_in_excluded_code`), unwired callbacks, globals unused or write-only, macros, panels (`never_loaded`, `only_in_excluded_code`, `indirect_load_possible` with evidence) |
| `lock_check(kind)` | locks followed path by path on compiled code; wrapper functions, lock order inversions, blocking calls while held |
| `risks(check, file, min_confidence, group)` | `leak_on_return`, `double_release`, `leak_at_end`, `unchecked_alloc`, `uninitialized_use`, `unread_return`, `ignored_return`, `lock_gap`, `inconsistent_lock`, `ui_control_unlocked`, `ui_from_thread`, `unbounded_copy`, `format_mismatch`, `shared_global_unlocked`. Findings come by confidence, then in that order of gravity, so a cut keeps the worst. `unread_return` is a return code stored in a variable and never read; `uninitialized_use` a local read before any write; `lock_gap` a global that several functions protect with the same lock and one access does not. `group` (default true) folds the repeats of one function (copies into one buffer, return codes into one variable) into a single finding (`occurrences`, `lines`, `calls`); `group=false` lists each. A resource handle is followed through the project's functions (released, kept, merely used, or returned by a wrapper), and an unknown `check` is an error |
| `consistency(check)` | `duplicate_definition`, `prototype_mismatch` (parameter names ignored, `long long` equal to `long long int`, a case-only difference is `faible`), `declared_not_defined`, `could_be_static`, `extern_mismatch`, `callback_signature`, `control_on_wrong_panel` |
| `review(limit, min_confidence, file)` | one call, one sorted report: findings of `risks`, `threads`, `lock_check` and `consistency` in four sections (crashes and leaks, threads and locks, ignored errors, consistency), each with its count per check and its `limit` worst items, then dead-code counts and what was not reviewed. Each section names the tool that gives the detail |
| `complexity(sort, file, min_risk, section)` | lines, cyclomatic, cognitive, depth, params, locals, stack; `section`: `by_file` or `duplicates` (functions copied from one model) |
| `module_graph(file, format)` | file dependencies, cycles, mutual pairs with the lightest edge to cut; `format`: `json`, `mermaid`, `dot` |
| `threads(global_name)` | execution contexts (UI, thread pool, async timers), what each reaches, UI timers, shared globals with their unprotected accesses (`unprotected_accesses`) and locks that do not agree (`conflicting_locks`); with `global_name`, who writes and reads it from where, and on which lines no lock is held |

Industrial I/O and persistence
-------------------------------------------------------------------------------

| Tool | What it gives |
|---|---|
| `call_sites(pattern, position)` | constant arguments passed to `Modbus*`, `*Wago*`, `MBT*`...: addresses, registers, bits, evaluated macros, calling functions |
| `persistence(kind, section)` | `ini`: sections, keys, readers and writers, keys written never read and the reverse. `sql`: tables and statements per function. Any other `kind` is an error |

SDK and build
-------------------------------------------------------------------------------

| Tool | What it gives |
|---|---|
| `sdk_index(paths, force)` | index NI headers and function panels; done automatically the first time it is needed if CVI is installed |
| `sdk_lookup(name, exact)` | prototype, parameters and comment of an SDK function |
| `build_check(target, dry_run, config)` | compile with `compile.exe`; diagnostics per file, change since the previous build, warning when the project's CVI version is not installed. `config`: `Debug`, `Release`, `Debug64`, `Release64` or a `.prj` configuration |

Versions
-------------------------------------------------------------------------------

| Tool | What it gives |
|---|---|
| `snapshot_save(label)`, `snapshot_list()`, `snapshot_delete(label)` | named states of the index |
| `diff_versions(before, after, path, files)` | `headline` in a few sentences, then added, removed, moved, signature, value and body changes, symbols switched between compiled and excluded, UI constants and INI keys added or removed, calls. Sides: a snapshot, `current` or `path:<folder>` (another version or variant). `files` restricts both sides |
| `change_impact(before, after, path, files, limit)` | what a change between two versions may break, from the same sides as `diff_versions`: the changed functions ranked to read first (callers to review, signature changes, in a thread, takes a lock, `risks` alerts when `after` is `current`), calls broken by a removed function that is still called, and changed macros with what they size |
| `history(target)` | git history when the project is a repository: commits on a function's lines, on a file, or the latest ones (at most 15 files listed per commit, `files_total` gives the rest) |
