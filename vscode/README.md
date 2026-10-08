# GDB RTOS Views (VSCode extension)

An RTOS view for embedded debugging, driven by [GDR (GDB enhance for RTOS)](https://github.com/flespark) CLI.
For discover task/timer/IPC status, resource usage, corruption and potential bug.

## How it works

### Layers

Four layers sit between the panel and the target. Each one does one job, and
only GDR knows anything about the RTOS:

```mermaid
flowchart TB
    ext["VS Code panel (this extension)<br/>session lifecycle, transport, rendering"]
    da["Debug Adapter<br/>cortex-debug, cppdbg, gdbtarget, ..."]
    subgraph gdbproc["GDB"]
        gdb["GDB<br/>symbols, DWARF, memory, run/halt"]
        gdr["GDR (gdr.py, embedded Python)<br/>RTOS decoding, checks, envelope"]
        gdb <-->|"Python API"| gdr
    end
    target["Target<br/>halted while anything is read"]

    ext -->|"DAP evaluate request"| da
    da -.->|"JSON envelope (text or file)"| ext
    da <-->|"GDB/MI"| gdb
    gdb <-->|"remote protocol (OpenOCD, J-Link, pyOCD, QEMU gdbstub, ...)"| target
```

- **GDB** is the debugger itself. It owns the connection to the target, the
  ELF symbols and DWARF types, memory reads, and run/halt control. It also
  hosts the Python interpreter that GDR runs in, which is why the session's
  GDB must be built with Python.
- **The debug adapter** is the extension you already debug with (see
  [Supported debug adapters](#supported-debug-adapters)). It starts GDB,
  translates VS Code's Debug Adapter Protocol (DAP) into GDB/MI commands, and
  drives continue/step/breakpoints. This extension does not replace it. It
  only adds the GDR loader to the adapter's launch-time command hooks, and it
  asks questions through the adapter's DAP `evaluate` request, the same path
  a watch expression takes. The adapter's stop and run events reach the
  extension through the [tracker extension](#why-the-tracker-extension).
- **GDR** could be used as a independent CLI (`source gdr.py` at the GDB
  prompt). It is the only layer that understands kernel structures: it walks
  the RTOS's task lists, objects and heap through GDB, checks them for
  consistency, and returns the result as a versioned JSON envelope through
  the `$gdr_json` convenience function. The text commands and the envelope are
  built from the same collectors, so a view can never disagree with what a
  command such as `rtt threads` prints at the GDB prompt.
- **This extension** is a client of that envelope. It runs the session
  lifecycle (check Python, load GDR, probe, initialize), carries the envelope
  back (inline `evaluate` results reassembled from chunks, or a temporary file
  when the adapter cannot return the payload), refuses a bundled runtime
  whose envelope schema it was not built for, and renders the result in the
  panel. It decodes no kernel structure and reads no target memory of its
  own; everything it shows comes from a field GDR returned.

### Session lifecycle

1. **At launch** it checks that GDB has Python, then `source`s the bundled
   `gdr.py`. Without Python the session still starts, and the panel shows why.
2. **At the first stop** it asks GDR which RTOS and version this target is
   (`$gdr_json("probe")`) and initializes that adapter.
3. **At every later stop** the panel asks for one snapshot, which runs the
   same collectors as the commands at the GDB prompt.

### Stop snapshot

Nothing is read while the target runs: GDR answers only at a stop, and the
extension marks the snapshot on screen as stale as soon as the target
resumes. Neither GDR nor the extension writes target memory or calls a
function in the firmware (a GDB inferior call), because an inferior call
resumes the target and could change the very state the snapshot is meant to
capture. Running the target stays entirely under the firmware's and your
control.

## Supported RTOS

See [GDR readme](https://github.com/flespark/gdr#readme)

## Supported debug adapters

| Adapter | Loader injection | Notes |
| --- | --- | --- |
| `cortex-debug` | `postLaunchCommands` / `postAttachCommands` | the reference setup |
| `cppdbg`, `gdbtarget`, `stlinkgdbtarget`, `jlinkgdbtarget` | `setupCommands` | needs a GDB-backed `MIMode` |
| anything else | — | refused with a reason: GDR runs inside GDB's Python, so an adapter without a GDB console cannot host it |

The session's GDB must have Python. xPack ships both variants:
`arm-none-eabi-gdb` has none, `arm-none-eabi-gdb-py3` does — point the adapter's
`gdbPath` (or `gdr.runtimePath`'s sibling setting on the adapter) at the `-py3`
binary. The extension says this explicitly instead of showing an empty panel.

## Why the tracker extension

`mcu-debug.debug-tracker-vscode` is a declared `extensionDependencies` entry,
because VS Code's own debug API exposes neither the adapter's stopped/running
events nor the stack frame it stopped in. The plugin subscribes to the tracker's
v1 API for exactly four things:

- `started` — the session gets its tab before GDR has answered anything;
- `first-stack-trace` — the stop, carrying the frame id every `evaluate` read
  needs, which is what makes the panel refresh per stop instead of per poll;
- `running` — the snapshot on screen is marked stale instead of refreshed;
- `terminated` — the launch's temporary scripts are removed, the tab closes.

Every other event the tracker sends (`capabilities`, `protocol-event`,
`initializing`) is ignored. GDR itself is never driven while the target runs, so
a tracker that is missing or answers with a shape this client does not know
degrades to a logged "initialized, no data" panel rather than reading a moving
target.

## The RTOS panel

The panel lives in the bottom **Panel** area, next to Terminal, Problems and the
Debug Console — it appears there once a debug session GDR can drive starts, and
stays out of the way otherwise (no standing activity-bar icon). `GDR: Open RTOS
Views` focuses it from the Command Palette; the refresh button in its title bar
re-reads the target on demand.

One tab per debug session; each tab
shows the snapshot GDR took at the last stop: the task table (numeric columns
right-aligned, stack usage as a bar, waiters collapsible, addresses clickable),
object overview, checks, heap, and hint bars that name the configuration a
missing column needs (a capability note or envelope warning verbatim from GDR).
The panel refreshes at each stop (generation-checked, so a stale answer is
dropped) and shows the last snapshot under a *target running* banner while the
target runs.

Clicking an address opens the mcu-debug memory viewer at that address — install
`mcu-debug.memory-view`; without it the address is copied to the clipboard
instead. A snapshot can be kept to disk per refresh (see `gdr.recordSnapshotsDir`)
for bug reports and offline inspection.

## Settings

| Setting | Meaning |
| --- | --- |
| `gdr.runtimePath` | GDR CLI entry directory holding `gdr.py`. Empty uses the bundled copy; set it to run against a development checkout. |
| `gdr.rtos` / `gdr.version` | Initialize exactly this adapter and version, overriding the probe. A manual setting is treated as the user's assertion: it registers with GDR's `--allow-unverified`, so a modified or stripped kernel still works, and the panel then shows a hint saying the target could not confirm it. |
| `gdr.autoInit` | Initialize automatically when the probe is unambiguous (default). Turn it off to always be asked. |
| `gdr.refreshOnStop` | Refresh the panel at each stop (default on). Turn off to refresh only on initialization or with the panel's refresh button. |
| `gdr.refreshHeaps` | Include the heap snapshot in every refresh (default on). The heap walk is the most expensive part; turn off when stops feel slow. |
| `gdr.recordSnapshotsDir` | Directory to save snapshot JSON files on each refresh (for offline inspection and bug reports). Empty disables recording (default). |
| `gdr.snapshotTransport` | Transport method for snapshots from GDB to the extension: `evaluate` (default) transfers data in memory via DAP; `file` passes data via temporary files (faster for large snapshots, but requires GDB and VS Code to share a filesystem). GDR switches to `file` by itself when the adapter's `evaluate` cannot carry a payload (cortex-debug renders a returned string as its element count, so every Cortex-Debug session uses the file route). |

The probe answers with its evidence and its confidence (`exact`, `range`,
`undetected`); the extension initializes automatically only for an
unambiguous exact result, and otherwise asks — with the evidence attached. A
resolved answer is cached per launch configuration, so a reflash in place does
not ask again.

## Versioning

The extension version tracks GDR's own year.serial release (the `__version__` in
`gdr/__init__.py`), with the semver patch left free for extension-only fixes:
GDR `2026.3` ships as extension `2026.3.0`, and a packaging or UI fix may
publish `2026.3.1` against the same runtime. `ci/check-vendored-schema.py` fails
when the two disagree, so a bundle never claims a runtime it was not built
from.

## Development

```bash
npm install
npm run vendor        # generate resources/gdr from this repository's runtime
npm run check-types
npm test              # units, plus a real QEMU/GDB session check when fixtures exist
npm run build         # emit dist/extension.js
npm run package       # vendor + build + gdb-rtos-views.vsix (installable)
```

`resources/gdr` is generated and ignored by git; `src/runtime.ts` refuses to
run when the bundled manifest's envelope schema differs from the schema this
client was built for, so an old extension never mis-renders a newer GDR.

## Shipping

`npm run package` produces `gdb-rtos-views.vsix` (the vendored GDR runtime and
the compiled client included). CI builds the same VSIX on every push and keeps
it as a build artifact / commit attachment **for verification only**: a
maintainer compares it with a local build and then publishes to the Marketplace
by hand. CI never publishes, because Marketplace versions are immutable and the
credentials would be a leak in the open-lane config.

The byte-for-byte check that the VSIX carries the GDR runtime it claims is
`ci/check-vendored-schema.py`, which also enforces the version rule
(`GDR x.y` ships as extension `x.y.0`).

## Known limits

- The panel's own DOM is not exercised end-to-end (that needs an Electron
  host); the renderer is pure and is asserted against a *real* QEMU snapshot in
  the integration lane, so the markup and the data it renders are both tested
  on real targets.
- The file route needs the extension to read the filesystem GDB writes to. GDR
  picks that route by itself when `evaluate` cannot carry a payload (every
  Cortex-Debug session, see the `gdr.snapshotTransport` row above), so a GDB in
  a separate filesystem namespace (for example WSL) cannot use Cortex-Debug at
  all; the panel reports the failed bypass instead of showing nothing.
- The probe reads versions from DWARF. A build without `-g3` exports no version
  macro, so the extension asks the user instead of guessing.
- GDR's console warnings (a rejected version, an unsupported RTOS) appear in the
  Debug Console; the extension reports success or failure from session state.
