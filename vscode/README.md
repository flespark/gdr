# GDR RTOS Views (VSCode extension)

An RTOS view for embedded debugging, driven by GDR(GDB enhance for RTOS) CLI.
For discover task/timer/IPC status, resource usage, corruption and potential bug.

## Co-work with CLI

GDR collect debugged ELF and target info as JSON envelope, this extension renders
what comes back. It does not decode kernel structures of its own, so a view can
never disagree with what GDR CLI prints.

## Stop snapshot

GDR is never driven while the target runs, and this extension never reads or writes
target memory itself, so target running always obeys firmware. GDR never call
target inferior function, because inferior function drive target running again may
over bug snapshot.

## How it works

1. **At launch** it checks that GDB has Python, then `source`s the bundled
   `gdr.py`. Without Python the session still starts, and the panel shows why.
2. **At the first stop** it asks which RTOS adapter this target is
   (`$gdr_json("probe")`) and initializes it.
3. **From then on** views read the same verbs the prompt does.

## Supported debug adapters

| Adapter | Loader injection | Notes |
| --- | --- | --- |
| `cortex-debug`, `mcu-debug` | `postLaunchCommands` / `postAttachCommands` | the reference setup |
| `cppdbg`, `gdbtarget`, `stlinkgdbtarget`, `jlinkgdbtarget` | `setupCommands` | needs a GDB-backed `MIMode` |
| anything else | — | refused with a reason: GDR runs inside GDB's Python, so an adapter without a GDB console cannot host it |

The session's GDB must have Python. xPack ships both variants:
`arm-none-eabi-gdb` has none, `arm-none-eabi-gdb-py3` does — point the adapter's
`gdbPath` (or `gdr.runtimePath`'s sibling setting on the adapter) at the `-py3`
binary. The extension says this explicitly instead of showing an empty panel.

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
| `gdr.runtimePath` | Directory holding `gdr.py` and the packages. Empty uses the bundled copy; set it to run against a development checkout. |
| `gdr.rtos` / `gdr.version` | Initialize exactly this adapter and version, overriding the probe. |
| `gdr.autoInit` | Initialize automatically when the probe is unambiguous (default). Turn it off to always be asked. |
| `gdr.refreshOnStop` | Refresh the panel at each stop (default on). Turn off to refresh only on initialization or with the panel's refresh button. |
| `gdr.refreshHeaps` | Include the heap snapshot in every refresh (default on). The heap walk is the most expensive part; turn off when stops feel slow. |
| `gdr.recordSnapshotsDir` | Write one JSON envelope per applied refresh. Empty disables recording. |
| `gdr.snapshotTransport` | `evaluate` (default) reads the envelope from DAP evaluate results; `file` has GDB write it to a file the extension reads back, which is cheaper for very large snapshots. `file` needs the filesystem GDB writes to be readable by the extension: a locally started GDB works (whether gdbserver is local or remote), and so do Remote-SSH and Dev Containers, where the extension runs beside GDB; a GDB in a separate filesystem namespace (for example WSL) cannot serve it. |

The probe answers with its evidence and its confidence (`exact`, `range`,
`undetected`); the extension initializes automatically only for an
unambiguous exact result, and otherwise asks — with the evidence attached. A
resolved answer is cached per launch configuration, so a reflash in place does
not ask again.

## Development

```bash
npm install
npm run vendor        # generate resources/gdr from this repository's runtime
npm run check-types
npm test              # units, plus a real QEMU/GDB session check when fixtures exist
npm run build         # emit dist/extension.js
```

`resources/gdr` is generated and ignored by git; `src/runtime.ts` refuses to
run when the bundled manifest's envelope schema differs from the schema this
client was built for, so an old extension never mis-renders a newer GDR.

## Known limits

- The panel's own DOM is not exercised end-to-end (that needs an Electron
  host); the renderer is pure and is asserted against a *real* QEMU snapshot in
  the integration lane, so the markup and the data it renders are both tested
  on real targets.
- `gdr.snapshotTransport: "file"` needs the extension to read the filesystem GDB
  writes to; a GDB in a separate filesystem namespace (for example WSL) must use
  the default `evaluate` transport.
- The probe reads versions from DWARF. A build without `-g3` exports no version
  macro, so the extension asks the user instead of guessing.
- GDR's console warnings (a rejected version, an unsupported RTOS) appear in the
  Debug Console; the extension reports success or failure from session state.
