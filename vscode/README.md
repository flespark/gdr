# GDR RTOS Views (VSCode extension)

An RTOS view for embedded debugging, driven by GDR itself.

GDR stays the single source of truth: the extension asks the GDB session it is
attached to for GDR's JSON envelope, and renders what comes back. It does not
decode kernel structures of its own, so a view can never disagree with what
`gdr api` prints at the prompt.

## How it works

1. **At launch** the extension appends `source <bundled>/gdr.py` to the debug
   configuration, so GDR exists in the session's own GDB before the first `p`.
2. **At the first stop** it confirms that GDB has a Python interpreter, asks
   GDR which adapter this target is (`$gdr_json("probe")`), and initializes it.
3. **From then on** views read the same verbs the prompt does.

GDR is never driven while the target runs, and this extension never reads or
writes target memory itself.

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

## Settings

| Setting | Meaning |
| --- | --- |
| `gdr.runtimePath` | Directory holding `gdr.py` and the packages. Empty uses the bundled copy; set it to run against a development checkout. |
| `gdr.rtos` / `gdr.version` | Initialize exactly this adapter and version, overriding the probe. |
| `gdr.autoInit` | Initialize automatically when the probe is unambiguous (default). Turn it off to always be asked. |

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

The real-session check boots the repository's Cortex-A9 QEMU profile and drives
GDR through the same expressions the extension issues. It skips (never fails)
when `GDR_GDB`, QEMU or the fixture cache is missing; see
`RT_THREAD_FIXTURE_CACHE` and `GDR_GDB` in the repository's `AGENTS.md`.

## Known limits

- This release covers the data path (load, probe, initialize, envelope transport);
  a webview-based panel is not implemented yet.
- The probe reads versions from DWARF. A build without `-g3` exports no version
  macro, so the extension asks the user instead of guessing.
- GDR's console warnings (a rejected version, an unsupported RTOS) appear in the
  Debug Console; the extension reports success or failure from session state.
