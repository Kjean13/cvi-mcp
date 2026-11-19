Home
===============================================================================

`cvi-mcp` is a read-only MCP server for LabWindows/CVI projects. It indexes the
C sources into SQLite outside the project and exposes thirty-eight tools, so an
agent can answer questions about a codebase it has never seen.

It exists for one situation: you open an unfamiliar CVI project and need to
know what calls what, which button runs which callback, and what changed since
the last release, without reading fifty files by hand.

Three pages: this one to get running, [Tools](Tools) for the exact parameters
and returns, [Internals](Internals) for how it works, what it misses and what
to do when it misbehaves. En français: [Accueil](Home-fr).

Install
-------------------------------------------------------------------------------

Python 3.10 or newer. Static analysis runs on Windows and Linux; detecting a
CVI installation and running `build_check` need Windows.

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

`cvi-mcp --check` reports what the machine actually offers rather than what the
documentation assumes: detected CVI installations whatever their year, SDK
header folders, build executable, active settings, index state and warnings. It
exits non-zero when a warning is present, so it also works in a script. Run it
before anything else.

Connect a client
-------------------------------------------------------------------------------

### Claude Code

Project scope, `.mcp.json` at the repository root:

```json
{
  "mcpServers": {
    "cvi": {
      "command": "cvi-mcp",
      "env": {
        "CVI_MCP_PROJECT": "C:\\projects\\app",
        "CVI_MCP_ALLOW_BODY": "1"
      }
    }
  }
}
```

Or from the CLI. Options come before the name and `--` separates the server
command:

```bat
claude mcp add --scope user --env CVI_MCP_PROJECT=C:\projects\app --transport stdio cvi -- cvi-mcp
```

Scopes: `local` is the default and private to the project, `project` is shared
through `.mcp.json`, `user` applies everywhere.

### OpenCode

`opencode.json` in the project, or `~/.config/opencode/opencode.json`. Three
differences from the shape above, and all three fail quietly if you get them
wrong: servers sit under `mcp.servers`, the command is an array, and the
variables block is called `environment`.

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "servers": {
      "cvi": {
        "type": "local",
        "command": ["cvi-mcp"],
        "environment": {
          "CVI_MCP_PROJECT": "C:\\projects\\app",
          "CVI_MCP_ALLOW_BODY": "1"
        }
      }
    }
  }
}
```

Older versions put the server directly under `mcp` and use `enabled` instead of
`disabled`. Check with `opencode mcp list`.

### Claude Desktop

```json
{
  "mcpServers": {
    "cvi": {
      "command": "cvi-mcp",
      "env": { "CVI_MCP_PROJECT": "C:\\projects\\app" }
    }
  }
}
```

If `cvi-mcp` is not on the client's PATH, which is often not the PATH of your
shell, give the full path to the script or use `"command": "python"` with
`"args": ["-m", "cvi_mcp.server"]`.

Configuration
-------------------------------------------------------------------------------

| Variable | Default | Role |
|---|---|---|
| `CVI_MCP_PROJECT` | `.` | project folder, or directly its `.prj` / `.cws` |
| `CVI_MCP_PROJECTS` | empty | named projects for `use_project`: `App=C:\\a;Other=D:\\b` |
| `CVI_MCP_DB` | `~/.cvi-mcp/index-<hash>.db` | SQLite index, one per project |
| `CVI_MCP_SDK_DB` | `~/.cvi-mcp/sdk.db` | SDK index, shared by all projects |
| `CVI_MCP_AUDIT_LOG` | `~/.cvi-mcp/audit.jsonl` | call log |
| `CVI_MCP_EXCLUDE` | empty | excluded patterns, separated by `;` |
| `CVI_MCP_ALLOW_BODY` | `0` | `1` allows function bodies out |
| `CVI_MCP_ALLOW_PEEK` | `0` | `1` allows short excerpts with `peek` |
| `CVI_MCP_MAX_SNIPPET_LINES` | `40` | line cap per snippet |
| `CVI_MCP_BUILD_CONFIG` | first Debug | `.prj` configuration replayed by the preprocessor and `build_check` |
| `CVI_MCP_DEFINES` | empty | extra or forced macros: `OPTION=1;OTHER` |
| `CVI_MCP_INDEX_UNLISTED` | `0` | `1` also indexes `.c` and `.h` of the folder missing from the `.prj` (`in_build: false`) |
| `CVI_MCP_MAX_CHARS` | `40000` | size budget of one answer |
| `CVI_MCP_FORMAT` | `json` | `compact`: lists as columns and rows |
| `CVI_MCP_AUDIT_PATHS` | `1` | `0` hashes file names in the audit log |
| `CVI_MCP_SETTINGS` | `.cvi-mcp.json` | project settings file |
| `CVI_MCP_SDK_PATHS` | auto | NI SDK folders, separated by `;` |
| `CVI_MCP_BUILD_CMD` | auto | path to the CVI build executable |
| `CVI_MCP_BUILD_ARGS` | `{project}` | argument template, `{project}` and `{target}` |
| `CVI_MCP_BUILD_TIMEOUT` | `300` | build timeout in seconds |

Exclusions match both the path relative to the project root and the bare file
name, so `vendor/*` and `*_generated.c` both work. Use them for a third-party
tree you do not want indexed, or for files no agent should ever read.

An optional `.cvi-mcp.json` next to the `.prj` tunes analyses for one project.
Every key is optional:

```json
{
  "build_config": "Release64",
  "defines": {"NOMBRE_MAX": "12"},
  "domains": {"camera": ["*Camera*", "Pylon*"], "plc": ["*Wago*", "*Modbus*", "MBT*"]},
  "resource_pairs": [["PylonDeviceOpen", "PylonDeviceClose", "arg0"]],
  "checked_calls": ["MyDriverWrite"],
  "thresholds": {"lines": [100, 200, 400], "cyclomatic": [10, 20, 50]}
}
```

`resource_pairs` adds acquire and release functions to the leak tracking; the
third field says where the handle is: `return` (assigned result) or `argN`.

How much code actually leaves
-------------------------------------------------------------------------------

With the defaults, an agent sees that `StartCallback` exists in `main.c` at
line 13, takes six parameters, is called by nobody, and calls `CommsInit` then
`SetCtrlVal`. It does not see a single line of the body.

That is enough to navigate and to map an architecture. It is not enough to spot
a bug inside a function. Set `CVI_MCP_ALLOW_BODY=1` when you want real
analysis, keep the line cap at a value you are comfortable with, and read
`~/.cvi-mcp/audit.jsonl` whenever you want to know exactly what went out: every
call is logged with the bytes returned and the lines of code that left.

`build_check` is the one tool that runs something. It invokes the CVI build
command and parses the output. It applies no fix and touches no source, but a
compilation produces its usual output files.
