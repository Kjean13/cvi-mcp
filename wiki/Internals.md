Internals
===============================================================================

Back to [Home](Home) to install and connect, or [Tools](Tools) for the tool
reference. En francais: [Fonctionnement](Internals-fr).

The pipeline
-------------------------------------------------------------------------------

1. **Resolve sources.** `.cws` then `.prj` then, only if both yielded nothing, a
   directory scan. Exclusions apply here. The `.prj` also gives include paths,
   compiler defines, configurations (32 or 64 bits, debug or release) and the
   CVI version.
2. **Replay the preprocessor.** Every `.c` of the build is a compilation unit;
   its `#include` are followed and its `#if` evaluated. Headers reached from
   the build and lying under the project root are indexed too.
3. **Read and fingerprint.** Byte order mark, then UTF-8, then cp1252, then
   latin-1. The fingerprint is taken on the bytes, so it does not depend on the
   encoding chosen.
4. **Neutralize CVI macros.** `CVICALLBACK`, `CVIFUNC`, `__declspec(...)` and
   friends are replaced by spaces of the same length.
5. **Parse twice** with tree-sitter C: once with excluded lines blanked (the
   compiled code, `active=1`), once raw to keep what only lives in excluded
   blocks (`active=0`). Line and column numbers are those of the file.
6. **Extract** symbols, references (code, `#if`, macro values, array sizes,
   initializers), call arguments, metrics.
7. **Store** in SQLite, outside the project, one index per project.
8. **Serve** the tools, with the output guard and the size budget in front.

Why macros are blanked rather than removed
-------------------------------------------------------------------------------

`int CVICALLBACK StartCallback(...)` is not valid C to a standard parser. Strip
the macro and every line and column after it shifts, so every position the
server reports would be wrong. Replacing it with spaces of identical length
keeps the file byte-for-byte the same length, so tree-sitter reports exact
positions and the original text can still be sliced for the `CVICALLBACK`
marker that identifies a callback.

Index schema
-------------------------------------------------------------------------------

| Table | Holds |
|---|---|
| `files` | path, hash, line count, size, mtime, origin, encoding, `in_build`, guard, parse errors |
| `symbols` | name, kind, line range, signature, params, flags, doc comment, body fingerprint, value, complexity, cognitive complexity, depth, locals, stack estimate, shape fingerprint, `active` |
| `refs` | file, line, column, name, kind (`call`, `read`, `write`, `ref`, `return`, `cond`, `macro`, `dim`, `decl`), enclosing function or macro, `active` |
| `call_args` | the literal arguments of each call |
| `includes` | included name, line, resolved file and how (`dossier_du_fichier`, `include_path`, `dossier_du_projet`, unresolved) |
| `pp_blocks` | each `#if`, `#elif`, `#else` branch: expression, state, confidence, reason |
| `sdk_files`, `sdk_symbols` | NI headers and function panels, separate namespace |
| `snapshots`, `snapshot_symbols`, `snapshot_edges` | named states for diffing |

The schema carries a version. On a mismatch the index is dropped and rebuilt
rather than migrated, because it is fully regenerable from the sources.

The simulated preprocessor
-------------------------------------------------------------------------------

Only directives are reread, never the code, so replaying every compilation
unit stays cheap. The environment of a unit starts with what the compiler
provides (`_CVI_` from the `.prj` version, `_WIN32`, `_WIN64` and
`_NI_mswin64_` in 64 bits, `_CVI_DEBUG_` in debug), then the `.prj` defines,
then `.cvi-mcp.json` and `CVI_MCP_DEFINES`. A value forced by `config_diff`
wins over any `#define` of the sources.

Expressions are evaluated as C does: integers, `defined`, every operator,
object-like macros expanded. An undefined name is 0; the answer then says which
name it was and whether it is defined elsewhere in the project, which usually
means an include order issue. A function-like macro or an unparsable
expression leaves the branch `unknown`: both variants stay in the index, as
before 0.3.2.

A header included by several units keeps, for each branch, the most alive
state seen: compiled in one unit is enough to count as compiled. `varies`
flags a branch whose state depends on the unit. Include guards and
`#pragma once` are recognized. Before parsing, excluded lines and the
directives of fully decided chains are blanked, which also repairs the classic
`#if` that splits an `if` in two.

How `ui_map` infers the wiring
-------------------------------------------------------------------------------

A `.uir` is binary and is never read. But CVI regenerates a `.h` next to it on
every save, recognisable by its banner: panels, then each control with its
type and the callback wired in the editor, in a comment. When those headers are
indexed they are the source of truth for panels, controls and editor wiring.
The code adds the rest: where panels are loaded, which controls are read or
written, callbacks installed at run time.

Without such headers, the map falls back to inference. Integer macros and enumerators are candidate UI constants. A constant is a
panel when another constant derives from it by prefix, as `PANEL_MAIN_START`
does from `PANEL_MAIN`, or when it is passed to a function whose name contains
`panel`. Everything else is a control, attached to the longest matching panel
prefix.

Then, in any call: an argument that is a UI constant is a target, an argument
that is the name of a project function is a callback. Both in the same call is
a wiring; a target alone is an access; a callback alone is a wiring whose
target could not be identified, which is reported separately.

This is why no list of CVI API functions appears anywhere in the code. It works
for `InstallCtrlCallback`, for menus, and for functions the author has never
heard of.

The `.fp` heuristic
-------------------------------------------------------------------------------

The function panel format is not documented. The indexer extracts printable
identifiers from the bytes and keeps only those a header already confirmed as a
prototype. Unconfirmed candidates are counted and discarded, never presented as
API. The gain is knowing which SDK functions are exposed in a function panel,
which separates public API from internal declarations.

Diffing without storing code
-------------------------------------------------------------------------------

A snapshot holds, per symbol, its signature, its value and a fingerprint of its
body. The fingerprint strips block and line comments, collapses all whitespace,
and hashes the result. A reformatted file or a rewritten comment produces the
same fingerprint; a changed expression does not. CRLF and LF collapse to the
same value, so a file reopened in another editor is not reported as modified.

Macros and enumerators carry no signature and no body, so they are compared on
their value. Without that, a control identifier moving from 3 to 4 would break
the interface and show up nowhere.

Origin of a file
-------------------------------------------------------------------------------

On a real project most macros are not the team's: of the 9 842 macros of
CL150, 7 508 come from the headers CVI generates for its `.uir` files and
1 006 from library headers (735 from `nivision.h` alone). The team wrote
1 328. Each file is therefore tagged:

- `generated` when it starts with the banner CVI writes in a `.uir` header;
- `library` when it is a header declaring prototypes and none of them is
  defined anywhere in the project, or a header absent from the `.prj`,
  reached by an `#include`, in a folder without any project `.c` and with no
  prototype implemented;
- `project` otherwise.

The second rule needs no list of vendor headers: a header is library code
because the project only calls what it declares. Calls to functions only
declared in a library header count as external, as SDK calls do.

Following locks and resources
-------------------------------------------------------------------------------

`lock_check` and the leak checks of `risks` share one path walker, run on the
compiled code. `lock_check` reparses the functions that take or give a lock and walks their
statements while keeping, per lock, how many times it is held. A lock is named
after its release function and first argument, `CmtReleaseLock(gLock)`, so two
locks in the same function are followed separately.

At an `if`, both branches start from the same state and are merged afterwards,
keeping the worst count. A loop body is merged with the state before it, and
`break` carries its state out of the loop. Each `case` of a `switch` starts
from the state before the switch. A branch left `unknown` by the preprocessor
is followed as two variants. A `return` reached with a count above zero is a
`return_while_held`, including a `return` hidden in a macro.

A function that keeps the lock it receives as a parameter, or releases one it
did not take, is a wrapper: its calls count as acquire or release in the
caller. Two locks taken in both orders are reported as an inversion, and a
popup, a TCP read or `ProcessSystemEvents` while a lock is held as a blocking
call.

Resources follow the same walk: a handle stored in a local variable must be
released, returned or stored outside the function on every path. A test such
as `p == NULL`, `!p`, `h < 0`, or `status == 0` on the return code of the
acquiring call, removes the resource from the branch where it does not exist.
A `goto` stops the walk; findings then carry `confidence: moyenne`.

Performance
-------------------------------------------------------------------------------

The index is refreshed before every tool call. To keep that cheap, a file whose
size and modification time are unchanged is not reopened at all. When either
moved, the file is read and hashed, and the hash decides. That keeps an
archive restore, which preserves neither reliably, from going unnoticed.

On CL150 (74 files, 74 000 lines, 87 once the reached headers are added) a
first indexing takes about six seconds, then a call costs one to one and a half
seconds: listing the sources, replaying the preprocessor on directives, checking
dates. Analyses that reparse functions (`lock_check`, `risks`, `consistency`)
stay under three seconds, `cvi_environment` around five because it walks the
installation folders.

Known limits
-------------------------------------------------------------------------------

- **`.prj` and `.cws`**: validated on CVI 2019 projects, including paths split
  over several lines and sources outside the project folder. `self_check`
  shows what was read.
- **Preprocessor**: SDK and Windows headers are not followed, so a macro they
  define reads as undefined (the answer says so). Function-like macros in an
  `#if` leave the branch `unknown`.
- **Name collisions**: the index is keyed by name. Two `static` functions with
  the same name in different files are conflated by `find_usages` and
  `call_graph`.
- **`.uir`**: binary, never read. Its generated `.h` carries panels, controls,
  types and callbacks; attributes, labels, layout and the data type of a
  control stay invisible, so `GetCtrlVal` type mismatches are not checked.
- **Dead code** follows names: a function reached only through a computed
  pointer looks dead; one whose address is taken counts as alive. Hence
  `confidence`.
- **Risks are heuristics**: buffer sizes come from local declarations only,
  `sprintf` bounds assume reasonable float values, resources are followed in
  local variables only, and thread analysis decides lock protection per
  function, not per line.
- **Not covered**: translation tables (language files), bounds of I/O arrays
  against protocol limits, magic constants.

Troubleshooting
===============================================================================

Start with `cvi-mcp --check`, or the `cvi_environment` tool. It reports
what is actually on the machine and carries a `warnings` list that usually
names the problem directly. When a result about the code surprises you, run
`self_check`: unread `.prj` entries, missing includes, branches the
preprocessor could not decide, files that do not parse.

No CVI installation detected
-------------------------------------------------------------------------------

Expected on Linux, where there is nothing to find. On Windows it means nothing
matched `CVI*` under the National Instruments roots. The year is never
hardcoded, so this is about the location rather than the version.

Set the two things that depend on the installation explicitly:

```bat
set CVI_MCP_SDK_PATHS=C:\Program Files (x86)\National Instruments\CVI2019\include
set CVI_MCP_BUILD_CMD=C:\Program Files (x86)\National Instruments\CVI2019\compile.exe
```

Static analysis does not need either of them. Only `sdk_index`, `sdk_lookup`
and `build_check` do.

`build_check` says the executable is unavailable
-------------------------------------------------------------------------------

NI documents the command line compiler as `compile.exe` (`cvicc` on Linux),
next to `cvi.exe`. The server looks for it on the PATH and under each detected
installation, then stops rather than guessing. Point `CVI_MCP_BUILD_CMD` at it
if it lives elsewhere.

`compile.exe project.prj` builds the release target. Choose another with
`CVI_MCP_BUILD_ARGS`, a template where `{project}` and `{target}` are
substituted: `{project} -debug`, `{project} -debug64`, or
`{project} -config="Debug64"`. The build writes its outputs next to the
project, as in the environment. The answer always echoes the exact command
line it ran.

`project_overview` returns few or no files
-------------------------------------------------------------------------------

Look at `project_file_note` in the answer. If the `.prj` yielded no sources,
the server fell back to scanning the directory and lists the sections it did
find. Send those sections and the reader can be adjusted.

Also check `CVI_MCP_EXCLUDE`. Patterns match both the path relative to the root
and the bare file name, so a pattern like `*.h` is wider than it looks.

A file I just added does not appear
-------------------------------------------------------------------------------

The source list comes from the `.prj`. A file sitting on disk that the project
does not declare is not indexed, and therefore never shows up in a diff
either. Add it to the project, or point `CVI_MCP_PROJECT` at a folder with no
`.prj` to force a directory scan.

Accented comments come back mangled
-------------------------------------------------------------------------------

They should not. Decoding tries the byte order mark, then UTF-8, then cp1252,
then latin-1. If a comment is still wrong, the file is in an encoding none of
those cover; open an issue with the first bytes of the line.

`ui_map` returns no panels
-------------------------------------------------------------------------------

It needs the `.h` generated by the CVI editor to be part of the indexed
sources, which is the case when the `.prj` declares it. If the constants file is excluded, or not declared in the `.prj`,
there are no UI constants to classify. Check that `list_symbols(kind="macro")`
shows your `PANEL_*` constants.

A panel with no derived constants and never passed to a panel function is
classified as a control, not a panel. That is a limitation of inference without
reading the `.uir`.

`sdk_lookup` finds nothing
-------------------------------------------------------------------------------

The SDK index is separate and is not built automatically. Run `sdk_index`
once. The answer to a failed lookup includes the index state and says so.

The index looks stale or wrong
-------------------------------------------------------------------------------

`reindex(force=True)` rebuilds the project index completely. Deleting
`~/.cvi-mcp/index.db` is equally safe: the index is fully regenerable and
holds nothing you cannot recompute. Snapshots live in that same file, so
deleting it loses them.

The client does not see the server
-------------------------------------------------------------------------------

Check that `cvi-mcp` resolves on the PATH the client uses, which is often not
the PATH of your shell, especially inside a virtual environment. The reliable
form is the full path to the script, or `python -m cvi_mcp.server`.

For OpenCode specifically, three shapes differ from other clients and all fail
quietly: servers go under `mcp.servers`, the command is an array, and the
variables block is `environment` rather than `env`. Confirm with
`opencode mcp list`.
