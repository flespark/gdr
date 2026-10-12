# Architecture

## Goals

Reduce the cognitive load of debugging complex RTOS-based embedded
firmware in GDB by:

- Folding noisy wrapper-type output (pretty-printers).
- Solving object navigation once (convenience functions).
- Aggregating multi-object state and Debug orientated diagnostics data gather (commands).

Non-goals: replacing GDB expressions, wrapping QEMU monitor commands, or
duplicating what `rust-gdb` / `gdb` already display well.

## Layering

```text
                     gdr.py  (entry: arg parse, bootstrap, register)
                        |
        +---------------+----------------+
        |                                |
       gdr/  (RTOS-agnostic core)      selected RTOS adapter
         |                                |
  adapter_api.py / commands.py      layout.py / navigation.py
  api.py / serialize.py             adapter.py / diagnostics.py
  formatting.py / gdb_bridge.py     commands.py / version.py
  layout.py / printers.py
```

### `gdr/` — core

| Module | Responsibility |
| -------- | --------------- |
| `gdb_bridge.py` | Wraps GDB registration, identifier-only symbol lookup, memory/type access, terminal probing, output and error guards. Inferior function evaluation is not used. `read_cstring` reads `char[]` to the terminator GDB finds, and reads `char*` as a bounded window (`GDR_MAX_CSTRING_LENGTH` bytes, `errors="replace"`) truncated at the first NUL, because a pointer carries no length; a short name whose window crosses into unmapped memory therefore degrades to `None`. |
| `constants.py` / `formatting.py` | Shared traversal/presentation defaults and pure optional/address/symbol/table formatting. Formatting has no GDB dependency. |
| `layout.py` | Generic `StructLayout` / `StructField` / `ListHook` dataclasses and accessors (`read_field`, `iter_list`, `container_of`). It interprets adapter-supplied paths but contains no target type names, symbols, or list conventions. |
| `printers.py` | Generic pretty-printer registration and rendering. Display labels, summary fields, enum maps, and pointee display paths come from the adapter layout. Type matching uses `strip_typedefs().unqualified().tag` so that typedef-spelled and cv-qualified values resolve to their underlying struct tag. |
| `version.py` | RTOS-neutral version parsing, range checks, formatting and declared decimal/packed-hex decoding. |
| `adapter_api.py` | `RtosAdapter`, presentation dataclasses, session identity and `gdr reset`. `ColumnSpec`/`Capabilities` are optional plugin metadata; text tables stay the default. |
| `serialize.py` | Pure envelope and chunk protocol for the machine-readable API, plus the shared `system_summary_pairs` builder the text renderer prints. `SCHEMA_VERSION` is independent of the GDR release version. |
| `api.py` | Neutral verb layer (`tasks`/`objects`/`detail`/`system`/`heap`/`capabilities`/`snapshot`) over the same collectors the text renderers use. |
| `derive.py` | RTOS-neutral derived-value helpers shared by the adapters: wrap-safe timer expiry (with/without the `overdue` label), fill-byte stack watermark counting, and the `count@names` waiter cell. Scalar-in/string-out, never adapter model objects. |
| `commands.py` / `functions.py` | Generic output coordination — the `collect_*` functions plus their guarded text renderers — and convenience functions including the `$gdr_json` transport; task columns and object vocabulary remain adapter-owned. |
| `help.py` | RTOS-neutral dataclasses and terminal/Markdown renderers for help trees. Command metadata stays structured so the same source can feed GDB now and MkDocs later. |

### `rtthread/` — adapter

| Module | Responsibility |
| -------- | --------------- |
| `layout.py` | **The only place that knows RT-Thread struct layouts.** Defines `RtConfig`, `detect_config()` (symbol-presence probing), and `build_layouts(config) -> KernelLayout`. Handles config-conditional fields (SMP, heap manager, IPC toggles) via factory branches, not version-branched files. |
| `columns.py` | RT-Thread column contracts: each table's headers, `ColumnSpec` metadata, machine-value semantics and shrink priority. `adapter.py` builds the rows from these specs, and the same specs feed the capability claim, so a table and what it advertises cannot disagree. |
| `navigation.py` | RT-Thread object navigation: registry/current-thread/tick entry symbols, type codes, timer traversal, and the halted system-heap snapshot. Returns raw `gdb.Value` objects using the layouts supplied by `layout.py`. |
| `adapter.py` | RT-Thread intermediate object models, `gdb.Value` conversion, adapter-owned task/object tables, detail dispatch and system summaries. These models are presentation inputs, not ABI layout descriptions. |
| `diagnostics.py` | Consumes adapter intermediate models for detail rendering and performs bounded raw mailbox/message-queue/mempool and system-heap walks and consistency diagnostics. |
| `tableviews.py` | Preserves timer callback/parameter source navigation, waiter entry links and mailbox slot addresses in merged detail tables; vertical CLI details remain available. |
| `version.py` | RT-Thread support ranges, exported target symbols, encoding order and RT-Thread-specific diagnostics. |
| `commands.py` / `help_docs.py` | The `rtthread` / `rtt` router and its structured help tree. `rtt help <topic>` renders command fields, tips, configuration dependencies and implementation limits; printer subtopics are derived from active layout metadata. |

### `freertos/` — adapter

| Module | Responsibility |
| -------- | --------------- |
| `layout.py` | FreeRTOS config/DWARF probes, logical struct paths, `FreeRtosLayout`, and the complete `FreeRtosTask`-related capability metadata. Config and layout stay together because the detected TCB fields directly determine the built paths. Version detection relies on `-g3` macro debug info, which is read from the image's debug strings, so it depends neither on the build tree still existing nor on the compilation unit GDB happens to have selected. |
| `columns.py` | FreeRTOS column contracts: the same single-source rule as the other adapter, plus the build-flag-dependent specs (`task_specs`, `queue_specs`) that keep a column the build cannot populate out of the table entirely. |
| `navigation.py` | Pure scheduler-list and current-task traversal functions plus the object discovery channels (`iter_registry_entries`, `iter_static_symbol_objects` with its session cache, `iter_active_timer_hosts`, `iter_mpu_pool_objects`, `iter_waiter_hosts`) and their aggregation (`DiscoveredObject`, `discover`, `discover_all`, `resolve_object`). Queue-family candidates are refined by the `Queue_t` discriminator (`classify_queue`: `ucQueueType` when `configUSE_TRACE_FACILITY` is on, else the `pcHead == NULL` mutex marker and `uxItemSize == 0` semaphore marker), deduplicated across the family so one address never lands in two tables; `discover_all` shares one waiter-channel scan per command call. The symbol channel names timer objects by their `pcTimerName` (read from the cast `Timer_t`) instead of the handle/buffer variable, so dormant timers stay reachable by the name the firmware gave them. List member access uses logical `end`/`next`/`owner`/`count` fields from `FreeRtosLayout`; walks are bounded and corruption-guarded. |
| `timers.py` | Software-timer decode: active-list epoch, wrap-safe `ExpiresIn`, and the daemon command queue ring-read with the 12 `tmrCOMMAND_*` names. |
| `events.py` | Event-group bits and per-waiter decode (control bits derived from tick width, ALL/ANY missing, mid-unblock marker). |
| `streams.py` | Stream/message/batching ring geometry, trigger asymmetry, deleted-buffer short-circuit, and the assumed `size_t` `NextMsg` length prefix. |
| `heap.py` | Heap-1..5 snapshot for `frt heap` and `frt system`: kernel counters (never resynthesised), canary decode, bounded free-list/linear walks with a three-way `CrossCheck` verdict. Walks start at the kernel's own `align_up(&ucHeap)` base, never the free-list head. |
| `adapter.py` | Task/queue/timer/event/stream models, value conversion, list tables, summaries, and the `RtosAdapter` protocol surface. |
| `version.py` | FreeRTOS support ranges, exported target symbols, encoding order and FreeRTOS-specific diagnostics. |
| `commands.py` / `help_docs.py` | The `freertos` / `frt` router and structured help tree for the 7 plural lists, 7 singular details, `system`/`objects`/`heap`, aliases, functions and layout-derived pretty-printers. `frt help <topic>` is rendered from metadata shared with the Markdown exporter rather than assembled output strings. |
| `checks.py` | On-demand `frt checks [scope] [name]` reports every invariant with verdict, object address and concrete evidence; normal views retain only problems. |
| `details.py` | Vertical detail builders for tasks and the queue/timer/event-group/stream-buffer families; queue entries expose FIFO storage addresses without reading payload bytes. |
| `tableviews.py` | Merges the same detail builders into typed object tables, retaining timer callback/ID navigation, waiter names and queue item addresses. |
| `diagnostics.py` | Bounded raw list walks and consistency checks (list/task/system/queue/timer/event), each reporting `ok`/`fail`/`skipped` — never a clean short walk on corruption or truncation. |

## Key decisions

### No RTOS / version auto-detection

Previous versions attempted to detect the RTOS and parse its version string
from symbols, then match struct patterns. This was fragile (failed on
attach, failed across remote configs) and duplicated logic. Users now
specify the RTOS and exact version, for example `gdr init rtthread 4.0.5`.

### Config features are probed, not specified

RTOS kernels vary by *configuration* far more than by version. For RT-Thread:
`RT_USING_SMP` adds `oncpu` to `rt_thread`; the heap manager
(`small_mem` / `slab` / `memheap`) changes the heap data structures
entirely; IPC components (`RT_USING_MUTEX`, etc.) may be absent.
Probing these by symbol presence (`rt_cpu_index`, `rt_sem_init`,
`rt_mutex_take`, ...) is reliable and cheap, and spares users from
reciting their `.config`. Probing falls back to safe defaults with a
warning when a symbol is ambiguous. Symbol presence uses DWARF/ELF
lookup, never a GDB expression that could call into the target.

### No inferior function calls

GDR inspects a halted RTOS. `gdb.parse_and_eval("foo()")` resumes the
core until the dummy frame returns, so SysTick and pending peripheral
ISRs can mutate `rt_tick`, thread state, and IPC objects. Kernel
collection therefore uses only:

- `gdb.lookup_symbol` / `lookup_global_symbol` / `lookup_static_symbol`
  for identifiers (`rt_tick`, `_object_container` / `rt_object_container`,
  `_cpus`)
- `gdb.Value` field and index access for struct members and arrays
- identifier-only `eval_identifier` → `gdb.parse_and_eval("NAME")` so GDB
  expands macros such as packed version constants on the host. Call and
  index expressions are rejected by the wrapper.

Target helpers such as `rt_tick_get()`, `rt_object_get_information()`,
`rt_hw_cpu_id()`, and `rt_memory_info()` are not called. SMP current-CPU
identity comes from the halted register file or GDB's selected thread.

### Adapter-owned dataclass layouts, not YAML schemas

Considered an external YAML schema + loader. Rejected because:

- Structs vary by **config**, not version. YAML would need conditional
  fields / overlays, turning the "lightweight loader" into a mini
  interpreter — a new failure surface.
- Version-to-version struct deltas are small; per-version YAML files
  would be 99% duplicate.
- Python dataclasses handle config-conditional fields naturally via
  factory functions (`build_thread_layout(config)`) with no extra
  syntax or parser.
- The adapter owns concrete type names, field paths, display labels, state
  encodings, target symbols, and object-registry traversal. The core only
  consumes generic layout metadata, so another RTOS can use different
  wrappers and object types without changing `gdr/`.

### Coupling is explicit and localised

All RT-Thread coupling lives under `rtthread/`: `layout.py` owns ABI field
paths, display metadata and state encodings; `navigation.py` owns symbols and
raw registry traversal; `adapter.py` owns intermediate presentation models,
conversion and tables; `diagnostics.py` consumes those models and owns bounded
raw-memory detail walks. Navigation remains pure-function based and receives a
layout explicitly only when it reads it. `gdr.py` is the composition root;
modules inside `gdr/` never import or identify an RTOS.

The dependency rules are enforced as follows:

- `gdr.py` may import the RTOS packages because it is the composition root.
- `gdr/` may depend only on Python/GDB and other `gdr/` modules; it must never
  import `rtthread` or `freertos`.
- An RTOS adapter may depend on `gdr/` contracts/helpers and modules inside
  its own package, but never on another RTOS adapter.
- `navigation.py` does not own adapter instances or hidden layout state. Its
  public walks are functions; callers pass a layout only when field paths or
  type mappings are required.
- Presentation flows inward as `adapter -> ObjectTable/ObjectDetail -> core
  renderer`; the core does not inspect RTOS model fields.

### Commands only aggregate

Per the Asterinas experience: commands should provide the multi-object
presentation that GDB expressions cannot conveniently produce: tables, trees,
and derived summaries. Convenience functions may navigate a collection, but
leave element inspection to native GDB expressions. Single-object field
inspection is left to `$gdr_task(name)` + `p $gdr_task(name).field`. This
keeps the command set small and avoids commands silently breaking when a field
is renamed (the function returns the raw `gdb.Value`).

### One machine-readable contract, rendered from the same collectors

Text tables cannot be consumed by an IDE, and DAP `evaluate` with a `repl`
context never returns command output, so the client contract is a versioned
JSON envelope reached either inline through `$gdr_json` or as base64 frames
through `$gdr_json_chunk`. Frames of one transfer are frozen from a single
envelope, so a target that moves mid-pull cannot reassemble two snapshots.
`gdr/api.py` routes every verb through the same `collect_*` functions the text
renderers print, so a verb result and its command output cannot drift apart;
`SCHEMA_VERSION` is bumped only on a breaking envelope change and is
deliberately independent of the release version. The verbs and the transport
are tooling surface: they are documented for design readers here and in the
changelog, not in the user command index.

## Main design

### Unified routing through the active adapter

`gdr/commands.py` holds the shared renderers, but every renderer resolves the
target through `adapter_api.active()` and dispatches on the active adapter's
`RtosAdapter` protocol (`task_table`, `object_table`, `object_detail`,
`system_summary`, `find_task`, `find_object`, `iter_tasks`). There is exactly
one routing entry point — the session adapter selected by `gdr init` — so
`gdr/` never branches on an RTOS name or hardcodes a command vocabulary. A new
RTOS only needs to implement the protocol to obtain the shared command tree,
pretty-printers and convenience functions unchanged. `gdr reset` (and
`gdr init --force`) clears that entry point together with the adapter's
session caches and the layout-derived printer lookup, so re-initializing into
another RTOS cannot fold objects through the previous adapter's layout;
registration still refuses a second active adapter.

### Typed presentation contract for machine clients

The envelope carries more than rendered text, and one construction path keeps
the two views honest:

- **Cells.** A row is built from `Cell(text, value, source)` objects
  (`text_cell`, `number_cell`, `address_cell`, `symbol_cell`, `waiters_cell`,
  `count_cell`, `raw_cell`), and `ObjectTable.from_specs` / `from_cells` derives
  `rows`, `value_rows` and, when a cell names code, `source_rows` from the same
  cells. A column therefore cannot be typed into the
  wrong position, and a text-only table still serializes exactly as it did
  before typing existed.
- **Columns.** Each adapter declares its columns once in `<rtos>/columns.py`
  (`ColumnSpec` kinds: `text`, `int`, `hex`, `addr`, `percent`, `waiters`,
  `symbol`, `enum`). Headers, alignment and the shrink *priority* come from
  that declaration, and the capability claim is derived from it too.
- **Capabilities.** `capabilities()` reports per kind whether it is enumerable,
  whether a detail exists, its columns, and a `note` naming the configuration
  that would enable a missing column or channel. This is the structured form of
  the "enable this config macro" hints the text tables print as messages.
- **Stable ids.** A table names its identity column (`id_key`, usually `Addr`);
  the serializer emits those machine addresses as row `ids`, so a client keeps
  a selection or expansion state across refreshes.
- **Verdicts.** `diagnostics_report()` returns `CheckFinding` rows for the
  `checks` verb. A detail block keeps `pairs` as the complete flat view the
  text renderer prints and adds `sections` built by `sections_from_pairs`,
  which classifies the two documented verdict forms (a verdict-prefixed value,
  or a `*Check` key) into `ok`/`fail`/`skipped` severity. Grouping therefore
  adds severity without reordering or replacing terminal output.
- **Schema 2.** A `ColumnSpec` carries `group` (`basic` for a property, `diag`
  for a verdict column), which the panel's checkbox children read. A located
  `source_rows` cell carries both `file` (GDB's resolved path) and `name` (the path
  as the debug information recorded it), because a build directory may be absent
  on the machine that opens it. `snapshot` no longer carries `checks`: the verdicts
  ride in their table (the heap `sections`, the diagnostic columns). `gdr api
  checks` stays a verb. Two verbs are new: `table <kind>` returns every object of
  a kind as one merged detail table (`ObjectTable` rows, columns as the union of
  the properties, repeating entries collapsed into one column), and `heap-dump`
  returns the block layout with its palette.

### The extension is a client of the envelope, not a layer

The VSCode extension lives in `vscode/` and consumes the machine-readable
surface described above. The dependency runs one way: the extension reads the
envelope through `$gdr_json`, and nothing in `gdr/`, `rtthread/` or `freertos/`
knows it exists. Three consequences are worth recording:

- `vscode/` is never imported by the runtime, and the runtime must not grow a
  plugin-specific branch: anything a view needs has to be a verb or an
  adapter-owned metadata field first (that is what the cells,
  capabilities and checks contracts are for).
- The bundled runtime under `vscode/resources/gdr` is *generated* by
  `vscode/scripts/vendor-gdr.sh` and is ignored by git and by the Python tooling,
  so the repository never carries two runtime copies in version control and no
  tool analyses the copy.
- `src/runtime.ts` compares the bundle's envelope schema with the schema the
  client was built for and refuses to run on a mismatch. That is the one failure
  which would otherwise corrupt data instead of failing loudly.

The extension's own closed loop follows the same rule as the Python lanes: it
boots a real QEMU profile, loads a real fixture ELF, and drives GDR through the
same expressions the extension issues, so a contract change on either side fails
in a test rather than in a user's session.

### Two notification surfaces: build advice versus runtime faults

The machine-readable `snapshot` envelope reaches the extension through the
transport used by `SnapshotFetcher`; the panel does not receive GDB command
stdout or adapter objects directly. General `messages` remain available to
CLI/API consumers, while panel-facing `data.build_hints` is a separate,
explicit list. It contains only actionable RTOS build/configuration advice,
for example enabling a missing config macro. Registry provenance, kernel tick,
object-discovery limitations, release-layout notes, and allocator walk prose
stay in the CLI/API response but are not panel hints. A hint block therefore
helps users produce a more informative firmware image; it is not an error log.

Errors and state failures use the extension host's popup surface: session
initialization failures, malformed/failed snapshots, failed heap-map requests,
source/memory navigation failures, and missing optional integrations are shown
with `showErrorMessage` or `showWarningMessage`, with a per-session gate so a
refresh loop cannot repeat the same popup. Startup notifications do not depend
on resolving the webview: runtime/transport failures are errors, missing adapter
selection is a warning, and tracker subscription failures report before any
session can populate the panel. Each session failure retains its concrete cause;
terminated sessions cannot publish a late initialization result. Informational status is written to
the GDR output channel rather than being presented as an alert. The popup is
for a condition requiring attention now; the inline block is for improving a
future build. Its viewport defaults to 88 px, has a bottom-centre resize handle
(48–480 px, pointer and keyboard), and preserves height/scroll per session.
New advice is appended at the bottom and scrolled into view; the border pulses
three times, respecting reduced-motion preferences. Unchanged advice does not
pulse on refresh, and advice that no longer applies is removed.

### The panel renders host-side and never decodes

The panel (`vscode/src/panel.ts`) is a webview view that follows the tracker's
stop events: every refresh asks one `snapshot` verb (chunked transport, with a
file bypass via ``gdr api snapshot --json --out`` for very large sessions) and
applies the answer only if its `generation` did not race a newer stop. The
rendering lives in `vscode/src/webview/render.ts` as pure functions that turn an
envelope into HTML host-side; the webview script only swaps the markup and posts
clicks back, so the XSS boundary is a single audited `escapeHtml` seam instead
of a DOM codebase inside the webview. Panel semantics derive only from the
envelope's own fields: alignment from `ColumnSpec.align`, machine values from
`value_rows`, waiter lists from `waiters` cells, and actionable build advice
from `data.build_hints` (see the notification policy above). The one arithmetic the panel performs is a used/stack
ratio bar against the adapter-provided numeric pair -- presentation, never
kernel interpretation. The bar is a background class (`gdr-pct-N`) chosen by
the host, not an inline `style`: the panel's Content-Security-Policy allows
styles only from its own stylesheet and silently refuses style attributes.

Cost control rides the verb layer, not new surface: `snapshot` accepts
`--no-heap` (skipping the only full-memory walk among panel verbs), and
`gdr.refreshHeaps`/`gdr.refreshOnStop` say whether the panel asks for it and how
often. Address cells navigate through the mcu-debug `memory-view` extension's
URI protocol (`vscode://mcu-debug.memory-view?expr=<addr>`), the same channel
cortex-debug uses; the viewer evaluates the bare address literal in the stopped
session, never an inferior call. A symbol cell that GDR resolves to a source
line (`source_rows`, from `gdb.find_pc_line`, which reads only the debug line
table) opens that file at that line on click; the host accepts only absolute
paths, and the webview's client stays import-free so a refresh can restore the
scroll position of the session still shown. Typed pointer links carry a raw
hexadecimal label on hover/focus; one fixed, selectable overlay escapes table
clipping and permits normal text selection/keyboard copying, without adding a
button or evaluating an expression.
FreeRTOS waiter records carry a proven saved-PC/source when available, otherwise
a TCB address; ambiguous task names remain non-actionable rather than guessing
an entry or selecting a different task.

### A thread's last PC is proven, never guessed

`struct rt_thread` holds no program counter, so the panel's PC column is read
from the thread's saved switch frame. `rt_hw_context_switch_exit` restores the
return address from one fixed slot, `thread->sp + offset`, and the same path
serves cooperative and interrupt-driven switches. The offset differs by port and
release (cortex-a9 3.1.x: 60; 4.x: 68; RISC-V 4.x: 0 with an 8-byte slot), so the
table in `rtthread/stackpc.py` is keyed by version and pointer width, and a release
it does not list gets no PC at all. A decoded value must then pass one of three
proofs: it equals the thread's entry (the thread never ran); the instruction before
it is a call to `rt_hw_context_switch` (a cooperative switch); or it names a
symbolised code address (an interrupt frame has no call site to check). A thread
running on another CPU has a stale frame and reports no PC. Anything unproven
renders `N/A`; the column is never filled with a plausible-looking address.

### A FreeRTOS task's last PC is keyed by port, not by version

A FreeRTOS TCB keeps `pxTopOfStack` but no PC, so the PC column is read from the
saved context frame the port pushed. The slot is fixed per port, and the ports
disagree: Cortex-M3 keeps the PC 56 bytes above `pxTopOfStack`, while Cortex-M33
(its PendSV also saves `PSPLIM`, `CONTROL` and `EXC_RETURN`) keeps it 24 bytes
below. Both use `PendSV_Handler`, so the symbol cannot tell them apart and a
version number would not either. `freertos/stackpc.py` therefore decides the port
from evidence: `xPortStartFirstTask` means RISC-V; `PendSV_Handler` plus the ARM
build attribute `Tag_CPU_name` (`7-M` or `8-M.MAIN`, read from the image's
`.ARM.attributes` section) means Cortex-M3 or M33. Anything undecided reads `N/A`.

A decoded value must pass a proof. On Cortex-M a switched frame is admitted when
the PC names code and the xPSR word beside it has the Thumb bit set, which a
hardware frame always has. RISC-V has no such bit and relies on the symbol alone.
A never-started task has no run-time proof here: the TCB keeps no entry function,
and the fixture-only entry table used during verification has no counterpart in
firmware. Such a task reads `N/A`, which is deliberate. The port symbols live in
`freertos/layout.py`, beside `PROBE_MARKERS`, because the boundary rule keeps symbol
spellings out of the task-table consumers.

### The heap map is one grid shared by the PNG and canvas

`gdr/heapmap.py` computes address-ordered, size-weighted cell runs, reserving at
least one 6×6-pixel cell per block before distributing the remaining cells by
largest remainder. Tiny blocks therefore remain visible; this minimum distorts
relative area slightly, so hover and legend sizes always report the original
bytes. Adaptive rectangular dimensions use every cell: no trailing area is
painted outside the arena. Genuine unreadable gaps are explicit unknown runs,
never free memory. The additive `heap-dump.grid` field carries dimensions, scale
and block-index runs, so the PNG, canvas and hover consume one mapping rather
than independently rounding addresses.

The PNG is encoded with `zlib`; `gdr/bitmapfont.py` adds owner/free/N/A colour
swatches and aggregate byte totals below the map without runtime dependencies.
Unsupported name glyphs are losslessly escaped. Palette colours are shared with
the canvas. Used blocks without recorded owners read `owner:N/A`; incomplete
walks retain an explicit legend warning.

### Layout and highlights are data, not style

The table and column order, the shown tables and columns, and the widths the reader set are a
`PanelLayout` the host stores per workspace and sends back into the markup.
Widths travel as `data-width` attributes and the client applies them through the
CSSOM after each render, because the panel's CSP refuses `style=` attributes.
Resize grips cover the full right header edge; pointer capture and temporarily
non-draggable headers isolate resizing from column reordering. Once a width is
set, fixed-layout `colgroup` tracks and their summed table width prevent automatic
layout or long values from overriding it; tracks follow column keys on reorder.
Table cards and scroll wrappers shrink to content with a viewport-width cap;
independent Heap tables close their own last-column borders. Wide tables retain
horizontal scrolling rather than stretching the page or forcing narrow tables wide.
Column groups default to native collapsed disclosures; same-session redraws
restore their open state (including the heap-map disclosure) and keyed
table/heap-map scroll offsets after layout. The checkbox tree consumes the same
saved column order as the table renderer, rather than regrouping diagnostic
columns; DOM drag gestures synchronize the tree before the host redraws it.
Card titles themselves are draggable (not their ancestor sections), so native
`dragstart` targets are unambiguous and canvas interaction remains independent.
The Display selection tree uses the same table/column layout for its parent and
child drag sources, including hidden entries; child moves stay within a table.
Checkboxes change presentation only, without collecting another snapshot. A
value that changed since the last applied stop is found by `vscode/src/diff.ts`,
which compares cells under the same key the renderer uses (table kind, row id,
column). The highlight colour is the theme's number-token colour, deliberately not
the link accent, and applies to links as well as plain values. Comparison includes
machine values so a symbol label hiding an address change still highlights; numeric
row ids are normalized without losing their positions. System and Heap properties
and verdicts use the same diff path.

Session selectors sit between RTOS identity and snapshot metadata. Waiter cells
show a count with a fixed-position, scrollable disclosure that overlays following
rows, preserving expansion by kind/object/column across same-session refreshes.
RT-Thread waiter links open the retained thread entry, not a guessed creation site;
FreeRTOS waiter navigation follows the proven saved-PC/TCB fallback described above. Timer
parameters/IDs use data-symbol declarations when available, otherwise memory
navigation; mailbox slots and FreeRTOS queue-item storage addresses open memory.
Queue Head/Tail/WriteTo/ReadFrom/Set and mutex Owner retain machine pointers for
memory navigation even when their labels are symbolized; scalar sizes/ticks are
never promoted to pointers.
RT-Thread merged tables omit redundant object `Type` and `KernelTick` columns;
soft/hard `TimerType` remains meaningful, while FreeRTOS subtype distinctions are
retained under `Subtype`.

### One evidence ladder for `probe` and `init`

`gdr probe` and `gdr init` must not disagree about a target, so they read the
same evidence in the same order: the adapter's presence markers (`PROBE_MARKERS`
in `rtthread/navigation.py` and `freertos/layout.py`, imported rather than
copied), then the version constants. They differ only in what they do with a
missing answer: `probe` reports `undetected` and leaves the decision to the
user, while `init` refuses to register -- an adapter registered against a target
with no matching symbol answers every read with empty tables, which looks like a
broken kernel rather than a wrong adapter.

Two deliberate asymmetries keep real workflows working:

- **Markers present but no exported version** is *accepted* with a note: the
  user asserted the version, which is exactly what the plugin's detection
  wizard needs after an `undetected` probe.
- **`--allow-unverified`** registers anyway, for a kernel whose symbols were
  deliberately modified or stripped. The override is not silent: the verdict's
  note is stored on the session (`adapter_api.register(verification_note=...)`)
  and prepended to every later envelope's `warnings`, so a client can keep it
  available without silently trusting the reads. That is why the note lives on the
  session and not in `gdr init`'s output, which a plugin cannot read.

### Exception guard for kernel data collection

RTOS debugging routinely touches target memory through GDB, and any read can
raise `gdb.error` / `gdb.MemoryError` (target halted, unmapped memory, remote
link dropped). Without a guard these bubble up as GDB "Python Exception"
noise and abort the rest of the command.

- **Command guards.** Every command tree's dispatch
  (`gdr._invoke_command` for ``gdr init``, and each adapter's
  `_invoke_command` for ``rtt``/``frt``) is wrapped with
  `@gdb_command_guard`. This is the one choke point that catches anything an
  inner renderer, adapter or navigation walk failed to contain — including
  `gdr init`'s config probing. The shared renderers in `gdr/commands.py` are
  also guarded, providing defense-in-depth and keeping direct calls safe.
- **Convenience-function guards.** `gdb.Function.invoke` must return a
  `gdb.Value`, so the command guard's `None` fallback cannot be used.
  Instead `gdb_function_guard` converts errors into `gdb.GdbError`, whose
  message GDB prints cleanly; an explicit `gdb.GdbError` from a body passes
  through unchanged. Adapting to a missing object is *not* an error and still
  returns a null `gdb.Value`.
- **Probe helpers.** Bridge primitives (`safe_int`, `safe_dereference`,
  `value_address`, `read_int`, `lookup_symbol`, `lookup_type`, `read_bytes`,
  `type_size`, `array_bound`, `type_field_names`, `arch_or_default`), and
  GDB tab completion degrade to safe defaults (`None`, `0`, `[]`). These are
  the building blocks guards rely on.  They catch only *expected* types
  (`gdb.error`, `gdb.MemoryError`, `IndexError`, `TypeError`, `ValueError`,
  `AttributeError` — the shared `TARGET_ACCESS_ERRORS` defined once in
  `gdr/gdb_bridge.py`); anything unexpected now bubbles to a guard for a full
  diagnostic instead of being silently swallowed.

Three deliberate broad catches are sanctioned beyond the probe-helper set:

- **GDB completion** must never print or raise, so `_object_names` keeps
  `except Exception` that degrades to no candidates (the readline prompt
  would be corrupted by a guard's warn/err output).
- **System summary/check sections are best-effort per item.** One broken heap
  or unreadable scheduler list during `frt system` rendering must not take
  down the whole summary; each such section degrades to its empty form
  (`N/A` heap fields, no check section) on `TARGET_ACCESS_ERRORS`.  A
  *programming* error (KeyError on a layout dict, a refactor-induced
  TypeError) is not in that set and bubbles to the guard for a diagnostic.
- **The API envelope boundary never raises.** `gdr.api.api_call` owes a
  machine client an answer, so it converts failures instead of letting them
  escape: `TARGET_ACCESS_ERRORS` becomes `target_unavailable`, because a
  failed target read is degradation the client retries after the next stop,
  while any other exception becomes `internal` and, in a `GDR_DEBUG` build,
  prints the full `show_last_exception()` diagnostic first and re-raises under
  `GDR_PROPAGATE_EXCEPTION`. Unlike the command guard it adds no `err()`
  one-liner: the envelope carries the same message and every caller (the
  `gdr api` renderer, the plugin) already shows it.

The guard's reporting policy:

- `gdb.error` / `gdb.MemoryError` → `warn()` (recoverable, continues);
- any other `Exception` → `err()` one-liner, or a full
  `show_last_exception()` diagnostic when `GDR_DEBUG` is enabled;
- `GDR_PROPAGATE_EXCEPTION` additionally re-raises after a debug diagnostic
  so a reported bug can be surfaced or tested.

### Derived data and state for debugging targets

List and detail output supplement raw kernel fields with derived state that
has direct diagnostic value, always computed from the target's own structures
rather than hardcoded assumptions:

- **Field Symbolization** dereference address, flag or bit mask value to
  meaningful symbol as possible. Otherwise roll back to hex number regard
  gdb output-radix setting.
- **IPC waiter summaries** are derived by traversing each suspend list
  (`count@names`), never from cached counters that's unstable over kernels.
  A version without a sender wait list renders `N/A`, never a fabricated `0`.
- **Capacity and policy columns** derive from kernel counters and object
  flags: `Free = capacity - entry`, `Used = total - free`, and a
  `FIFO`/`PRIO` policy decoded from the IPC flag.
- **Detail diagnostics** extend the list columns with bounded, corruption-
  guarded raw-memory walks and consistency verdicts (event waiter
  `event_set`/mode pairing, mailbox FIFO offset check, message-queue chain
  counts, memory-pool free-list and alignment checks, timer wrap-safe
  `ExpiresIn`).

### Width-adaptive lists

`rtos <objects>` tables adapt to the current terminal width without ever
changing their column set. The effective width is probed in priority order
(`set width`, `shutil.get_terminal_size`, fallback 120) in
`gdb.gdb_bridge.terminal_width`, while pure formatting lives in
`gdr.formatting.format_table` so unit tests can pin 80/100/120/160 columns.
When the natural table is too wide, only text columns explicitly marked
`elastic` by the adapter shrink, in adapter-provided priority order, and
overlong cells truncate with `..` while preserving a leading waiter count. If
even the minimum elastic widths overflow, the natural table is printed
unchanged and the terminal may wrap it. Numeric, state, and address cells are
never truncated or dropped.

### Critical vs non-critical field placement

Kernel fields are classified by diagnostic value, and that classification
fixes where each field may appear:

- **Critical fields** (waiter counts, `Addr`, `ExpiresIn`, `OrigPrio`,
  SMP `CPU`/`Bind`) are mandatory in list output and survive any width
  adaptation; their meaning must never be silently truncated away.
- **Non-critical single-value fields** (`Policy`, `Free`, `Used`) stay in the
  compact list when the 120-column budget allows, without adding rows.
- **Non-critical detail fields** (internal pointers, per-waiter conditions,
  consistency checks, thread `error`/`remaining_tick`) appear only in the
  singular `rtt <object> <name>` detail, or via native `$gdr_object()` /
  GDB expressions.

This mirrors the field taxonomy described earlier in this document: the list
column set is stable, and deep or verbose state is reached through the detail
command or raw `gdb.Value` inspection rather than by widening the default
table.

### Memory sizes are bytes; the docs declare the unit, not the headers

Every memory size GDR prints is a byte count. Key/value pairs state the unit in
the value (`<n> B`, from `format_optional_bytes`). Table headers stay plain
names (`Stack`, `Used`, `HighWater`, `Size`, `BlockSize`, `Bytes`, `Space`,
`Capacity`, `Trigger`): once the convention is documented, a unit suffix in the
header only repeats it. Each byte column carries a `doc` that says bytes, and
the `rtt`/`frt` help text lists the unit. The occupancy table's
`Bytes (header included)` is the one header that qualifies its meaning, since
its spans include block headers. Counts (blocks, threads, objects, a mempool's
Total/Free/Used) stay unitless. The panel's stack bar applies wherever a
`Stack` column sits beside `Used` and `HighWater`, so renaming those columns
also updates `vscode/src/webview/render.ts`. A heap table's title comes from the
adapter (`HeapReport.table_title`), so one label never names two different
tables.

RT-Thread small_mem accounting uses block spans, header included: each used
block contributes its full span to `used_bytes`, and `total_bytes` is the
kernel's `mem_size_aligned`, which excludes the end sentinel's header. On a
consistent chain `used_bytes + sum(hole_sizes) + header * free_blocks ==
total_bytes + header`, which is why `HeapAccounting` compares the walk with the
kernel counters.

## Runtime data flows

Initialization selects the adapter and builds its layout before registration:

```text
gdr init -> RTOS version policy -> config probes/layout -> adapter
         -> printers/functions/api registration
```

Machine-readable clients use the same collectors as the text commands:

```text
gdr api <verb> / $gdr_json -> collector -> active adapter
  -> ObjectTable/ObjectDetail/SystemSummary/HeapReport
  -> envelope JSON (inline, or chunked when it exceeds one evaluate result)
```

Aggregate commands use the adapter-owned presentation path:

```text
rtt/freertos command -> generic coordinator -> active adapter
  -> RTOS navigation/layout -> adapter intermediate model
  -> ObjectTable/ObjectDetail/SystemSummary
  -> shared formatting -> GDB output
```

Native inspection stays separate:

```text
$gdr_task/$gdr_tasks/$gdr_object -> active adapter -> pure RTOS navigation
  -> raw gdb.Value or target-native pointer array
GDB p/bt/info -> registered printer -> active layout metadata -> GDB display
```

## Closed-loop verification

GDR maybe degrade silently after update: the script runs but output is wrong.
To guard against this, QEMU smoke tests boot RTOS-specific firmware that
creates known objects and assert, where an adapter implementation exists:

- pretty-printers registered and fold correctly,
- convenience functions return non-null `gdb.Value` with expected fields,
- aggregate commands list the expected objects,
- the machine-readable envelope round trip: `gdr api` output parses, the
  `$gdr_json` frames reassemble to the same envelope, and a rejected re-init
  leaves the live session usable.

The FreeRTOS `mps2-an385` fixture asserts ready-marker delivery, retained
DWARF for kernel structures, the 32-bit ABI, persistent GDB, scheduler-list
navigation, current-task marking, system counters, and pretty-printer fold
for typedef-spelled kernel objects (Task/List/Queue). The queue family adds
ground-truth assertions against the objects the fixture creates: the queue
lengths and item counts, the blocked sender/receiver names in `SendWait`/
`RecvWait`, the semaphore count/max, the mutex `Held`/`Owner`/`Recursive`
cells (including a two-level recursive take), the FIFO `Item[0]` storage address,
the absence of any owner field in a semaphore detail, the `Set` column
appearing only on the queue-set variant, and the `?` inference marker on the
trace-off variant.

### Test infrastructure

Tests use a **persistent GDB session** driven by `pexpect`:

1. A session-scoped `QemuSession` starts the selected QEMU profile with a
   dynamically allocated `-gdb tcp::<port>` endpoint (free-running, no `-S`)
   and waits for the profile's ready marker. Every session owns distinct serial
   and QEMU-output logs, which are included in boot timeout and early-exit
   diagnostics.
2. A session-scoped `GdbSession` spawns one GDB process via `pexpect`,
   connects to QEMU, and runs `source gdr.py` **once**. All tests in the
   suite reuse this single GDB connection, keeping convenience
   functions and pretty-printers registered across tests.
3. Each test calls `gdb_session.run(...)` to execute a GDB command and capture
   output. ANSI escape sequences and PTY artifacts are stripped automatically.

This approach (borrowed from `pytest-embedded-jtag`'s `Gdb` class) is
preferred over spawning a fresh GDB batch process per test: it is faster
and avoids registration-state loss between tests.

`tests/support/loader.py` is the single reader of closed-loop environment
variables (`GDR_RTOS` / `GDR_VERSION` / `GDR_QEMU_TARGET` /
`GDR_FIXTURE_VARIANT` plus cache and optional ELF overrides). Matrix scripts
and integration tests both call it; they do not parse `os.environ` themselves.
`tests/integration/conftest.py` is the pytest assembly layer: it selects
profiles and constructs session fixtures. Reusable QEMU/GDB process lifecycle,
dynamic ports, logs and timeout diagnostics live in
`tests/support/qemu_harness.py`. The FreeRTOS snapshot lane uses
`tests/support/freertos_elf_harness.py` (`file` only, no QEMU).

### Target profiles

`GDR_QEMU_TARGET` selects the profile while keeping all GDR assertions shared:

| Target | QEMU startup | GDB symbols | Notes |
| -------- | -------------- | ------------- | ------- |
| `cortex-a9` | `qemu-system-arm -M vexpress-a9 -kernel rtthread.elf` | `rtthread.elf` | No SD device is required for the fixture boot path. |
| `rv64` | `qemu-system-riscv64 -M virt -cpu rv64 -m 256M -bios rtthread.bin` | `rtthread.elf` | M-Mode boot, no SD image, `set architecture riscv:rv64`. |
| `mps2-an385` | `qemu-system-arm -M mps2-an385 -kernel freertos.elf -semihosting-config enable=on,target=native` | `freertos.elf` | FreeRTOS-Kernel tag builds (10.3.1 config matrix / 10.4.x / 10.5.x / 11.1.x) on Cortex-M3. |
| `mps2-an521` | `qemu-system-arm -M mps2-an521 -kernel freertos.elf -semihosting-config enable=on,target=native` | `freertos.elf` | Dual-core Cortex-M33 (SSE-200) SMP lane, kernel `11.3.1`, `smp` variant. The machine is fixed at two cores, so `-smp` is redundant. GDB sees the second core as a separate *inferior*, not a second thread, so per-core register reads would need `target extended-remote`; every SMP assertion instead derives from shared memory (`pxCurrentTCBs[]`). |

The ELF and firmware image may be separate: RV64 deliberately boots a raw BIN
while GDB requires the DWARF ELF. The shared suite asserts each profile's
pointer width, including `sizeof(void *) == 8` for RV64 and 4 for the FreeRTOS
Cortex-M fixture.

`tests/support/rtthread_fixture_profiles.py` separately owns fixture-level expectations that
vary by target or RT-Thread version: object enum values, the current-thread
expression, and canonical fixture object names. It intentionally does not
import production layout metadata, so a regression in GDR's layout mapping
cannot update the expected values at the same time. Cortex-A9 fixture patches
set `RT_NAME_MAX` to 16, preserving the canonical `test_mutex` and
`test_timer` names used by every shared test; the RV64 BSPs already use 20.

The RV64 matrix covers RT-Thread v4.0.4, v4.0.5, v4.1.0, and v4.1.1. The BSP
is `bsp/qemu-riscv-virt64` through v4.1.0 and is renamed to
`bsp/qemu-virt64-riscv` in v4.1.1; each path has a separate platform-specific
patch set.

Layout changes must update the corresponding assertion, keeping the
helper and the kernel struct in lockstep.

The Cortex-A9 matrix covers RT-Thread 3.1.0 through 3.1.5 as well as the 4.x
representatives. It runs the complete suite at 3.1.0, 3.1.3, and 3.1.5, which
cover the pre-`Null` object enum, the enum/`rt_semaphore` transition, and the
final 3.1 layout. The remaining 3.1 tags retain build coverage for their exact
Cortex-A9 fixtures. Upstream has no QEMU RV64 BSP for 3.1.x, so that range is
Cortex-A9 only.

### Known constraints

Known limits of the debugging model and the fixture evidence behind each
decoder branch.  A constraint is a *current* limitation (what GDR cannot do
or what a lane cannot falsify), not a development rule — development rules
live in `AGENTS.md` and the per-lane READMEs.

#### General

**Kernel debug info drives every probe.** Both adapters read versions,
config features and struct shapes from DWARF and `-g3` macro tables, never
from version strings or a parsed `.config`.  A build stripped of debug info
(or a halt in a CU the macro table does not cover) degrades specific probes
to safe defaults with a warning, never a guess.

**SMP per-core state comes from shared memory, not per-core registers.**
QEMU models a second core as a separate GDB *inferior*, so per-core register
reads would need `target extended-remote`; every SMP assertion instead
derives from shared structure (`pxCurrentTCBs[]` for FreeRTOS, `rt_cpu_index`/
halted registers for RT-Thread).

#### RT-Thread

**3.1.x has no QEMU RV64 BSP.** Upstream's RV64 BSP starts at 4.0.4
(`bsp/qemu-riscv-virt64`, renamed `bsp/qemu-virt64-riscv` in 4.1.1), so the
3.1.x range is verified on Cortex-A9 only; the rv64 lane covers 4.0.4 through
4.1.1.

**Fixture patch sets fix `RT_NAME_MAX`.** Cortex-A9 fixture patches set
`RT_NAME_MAX` to 16 to preserve the canonical `test_mutex` / `test_timer`
names every shared test asserts on (the RV64 BSPs already use 20).  The
object enum value for `Null` predates 3.1.3 and is asserted only on the
3.1.x tags whose layout is pinned.

**Heap-manager shape is probed, not asserted.** `small_mem` / `slab` /
`memheap` change the block-header structure entirely, so the heap walks are
keyed off the probed `heap_type`; missing counters render `N/A`, never a
fabricated 0.

#### FreeRTOS

**Live coverage is 32-bit Cortex-M plus one 64-bit RISC-V lane.**
The 32-bit lanes are `mps2-an385` Cortex-M3,
`mps2-an521` dual-core Cortex-M33 and the Cortex-M33 static snapshot; a
`qemu-virt-rv64` lane (QEMU `-machine virt`, `portable/GCC/RISC-V`
rv64imac) covers 64-bit pointers and 64-bit ticks (the RISC-V port hardcodes
`TickType_t` to the architecture width; the lane's config also declares
`configTICK_TYPE_WIDTH_IN_BITS` 64 so the kernel's event-group control
bits sit in the top byte, matching the tick-width-derived masks).  Pointer
and field widths are always taken from
DWARF (`read_path` reads each union arm at its target type, and the reserved
MPU-pool handle is compared at the target pointer width), so no literal width
constant exists in the adapter.

**Version detection depends on `-g3` macro debug info and CU scope.**
`detect_target_version()` reads the `tskKERNEL_VERSION_*` macros via
identifier eval and `info macro`. Both consult the current compilation unit's
`.debug_macro` first; `info macro -a` plus the fixture-exported
`gdr_freertos_version_num` cover a halt in HAL / startup / application CUs.
A remaining miss degrades to a warning ("target FreeRTOS version is not
exported") and skips the mismatch check rather than guessing.

**Live fixtures are a variant matrix**, not a single configuration.
`ci/freertos/fixture/config/<variant>/` plus a shared `main.c` produce
`base` (the historical 10.3.1 combination), `full`,
`static-only`, `static-dynamic`, `trace-off`, `heap-1`/`2`/`3`/`5`,
`heap-protector` (≥V11), `heap-5-protector` (heap_5 + the protector, one
region), `registry-0`, `pend-callback` (`INCLUDE_xTimerPendFunctionCall`),
`streams` (write/read/delete actions on the stream/message/batching
buffers), `mpu` (single-core `configENABLE_MPU`, wrappers v2), `smp`
(dual-core 11.3.1) and `tick16` (`configUSE_16_BIT_TICKS 1`, a 2-byte
`TickType_t` on either ARM lane's kernel). Cache layout is
`<cache>/<target>/<version>/<variant>/freertos.elf`. Independent expected
capabilities live in `tests/support/freertos_fixture_profiles.py` and must not
be derived from `freertos/layout.py`.

The historical `base` combination remains: single-core / Cortex-M3 / heap_4
/ trace_facility=on / FreeRTOS 10.3.1 / `configENABLE_BACKWARD_COMPATIBILITY=1`
(member name `pvContainer`) / no `pxEndOfStack` / scalar `ucNotifyState` /
no runtime statistics / `configMAX_PRIORITIES=6` / stack grows down. Default
builds still use `pvContainer`; without `pxEndOfStack`, `Stack`/`Used`/`HighWater`
are unavailable because the saved SP does not retain stack capacity.

**Not covered by any current fixture (documented, unit-tested only):**

- `stack_grows_up`: **not supported**. GDR decodes FreeRTOS stacks as
  grow-down only (high water mark scans the untouched fill from the low end).
  The sole upstream `portSTACK_GROWTH +1` port is SDCC/Cygnal 8051, which has
  no GCC toolchain and no QEMU machine, so no grow-up target can exist; the
  dead field/branch was removed rather than kept as an untestable probe.

Several decoder branches now have live fixture evidence that used to be
unit-test-only: the MPU-pool channel (`mpu` variant), event-group control
bits at 16/64-bit tick widths (`tick16` / the RISC-V lane), the timer
pended-callback arm (`pend-callback`), stream-buffer ring wrap and the
deleted-buffer signature (`streams`), and `ListIntegrityBytes` on the static
snapshot.  Each variant's exact scope is documented in
`ci/freertos/README.md`; the snapshot variant carries the diagnostic negatives
a healthy kernel cannot produce.

**Static snapshot variant** (`build-fixture-kernel.sh --variant snapshot`,
cell `mps2-an521/11.1.0`, sources in `ci/freertos/fixture/config/snapshot/`)
covers states a healthy kernel cannot produce
(corrupt lists, SMP `xTaskRunState` of `0`/`1`/`-1`/`-2` without a live
dual-core port, a timer on the overflow list, and corrupt-heap cells). Snapshot
data is non-zero-initialized into `.data`; file-only GDB synthesises zeros for
`.bss`, so a BSS-resident structure would read back as a successful decode of
empty lists (see `ci/freertos/README.md`). Zero-valued standalone globals are
forced into `.data` with `__attribute__((section(".data")))` for the same
reason. The heap *mismatch* cell (free-list vs linear vs counter disagree) and
the *allocated-bit* cell (free-list member with the size_t MSB) cannot share
one heap symbol set — a corrupt walk swallows the mismatch verdict — so the
allocated-bit cell lives in a second, heap-only ELF (`snapshot_heap.c`).

**No `Entry` column.** The FreeRTOS TCB (`tskTaskControlBlock`) does not store
the task entry function pointer after task creation; it exists only transiently
on the initial stack frame and is overwritten on first context switch. This is
a fundamental difference from RT-Thread's `rt_thread.entry`, which persists in
the TCB. The `frt help` output documents this limitation.

**High-water mark semantics.** `HighWater` matches RT-Thread: historical maximum
stack bytes used, derived as `max(Used, recorded usable span - untouched fill)`.
FreeRTOS's native API instead returns remaining words; GDR converts and subtracts
at the model boundary, then includes the current saved-SP footprint because
reserved context slots may still contain `0xa5`. `pxEndOfStack` is inclusive of one `StackType_t`;
`Stack` and `Used` use that same upper bound, excluding alignment-discarded
allocation padding. Missing bounds, partial reads, or ambiguous unfilled/exhausted
stacks yield no value, never a fabricated total. Initial context reservations
count before scheduler startup, ensuring `Used <= HighWater <= Stack` whenever
all are available. A halted snapshot cannot recover historical reservations
that left no write trace or distinguish a write equal to the fill byte.

**Queue `type` field is gated by `trace_facility`.** The `ucQueueType` member
only exists under `configUSE_TRACE_FACILITY == 1`. Layout summary fields that
reference config-conditional struct members must be gated by the corresponding
`FreeRtosConfig` flag; unconditional inclusion produces `N/A` on builds where
the member is absent.

**Heap blocks carry no owner field, so per-task heap usage is not
attributable.** A `BlockLink_t` is exactly `{pxNextFreeBlock, xBlockSize}`
(heap_4.c) with no owner member; `frt heap` therefore never offers a thread-occupancy
breakdown, and `frt help` documents this instead of faking a column. `frt heap`
renders the `Algorithm/TotalSize/FreeSize/MinEver/Allocated/Freed/Protector/Blocks/
Holes` properties, using `N/A` for unavailable facts. `Allocated`/`Freed` are
cumulative successful operation counts, not bytes or live block counts.
`Check[HeapCrossCheck]` appears only for disagreements or incomplete walks;
`frt checks heap` includes successful verdicts.

**heap_5 without `configENABLE_HEAP_PROTECTOR` has no linear-walk bounds.**
The kernel exports no region bases without the protector (`pucHeapLowAddress`/
`pucHeapHighAddress` only exist then, heap_5.c), the intermediate region-markers
can be unlinked by forward coalescing, and the region table is a function-local
array — so `frt heap` skips the linear walk, renders `Blocks` from the free-list
walk only and reports `Check[HeapCrossCheck]: skipped: heap_5 region bases unknown`.
With the protector a single-region heap can be walked from its base to `pxEnd`;
the `heap-5-protector` variant (heap_5 + `configENABLE_HEAP_PROTECTOR`, one
region) is the live fixture for that branch: the linear walk runs, the cross-check
passes and the region extremes become the true `TotalSize` (a multi-region
extent would span gaps, so the total is only set when the walk reaches `pxEnd`
-- the single-region witness).

**The heap_4/heap_2 linear walk starts at `align_up(&ucHeap)`, never at the
free-list head.** `pvPortMalloc` carves every allocation from the *front* of the
first suitable free block and puts the remainder back in the list (heap_4.c:261,
278-288), so after the first allocation the free-list head sits above the
allocated blocks at the heap base; heap_2 additionally sorts its free list by
size, so its head has no relation to the physical base at all. Walking from the
head therefore silently drops every block below it while the cross-check — which
compares only free bytes and free-block addresses, all at or above the head —
still reports `ok`. GDR walks from the kernel's own `pucAlignedHeap`
(`align_up(&ucHeap, portBYTE_ALIGNMENT)`); when the `ucHeap` symbol cannot be
resolved the walk is skipped (`Holes` is `N/A` and `Check[HeapCrossCheck]` explains why) rather
than truncated from the head. Live evidence on `mps2-an385/10.3.1/base`: the
aligned base is 13792 bytes below `xStart.pxNextFreeBlock`, and the walk from the
base covers 57 blocks (56 allocated, 1 free) up to `pxEnd`, summing to exactly
`xFreeBytesRemaining`.

**An uninitialised heap has no derivable used bytes.** Before `prvHeapInit` runs,
`xFreeBytesRemaining` still holds its static initializer (0 for heap_4), so
`total - free` would report the whole heap as used; `frt system` omits the
`Heap used` line entirely in that state and keeps only the statically knowable
`Heap total`. heap_2's initialisation flag is not always readable either: at
`-Og` the fixtures fold `xHeapHasBeenInitialised` into a non-debug local symbol,
so the state falls back to `xEnd.xBlockSize` (written by `prvHeapInit`, zero
before it).

**Live heap coverage includes one 64-bit lane.** The `qemu-virt-rv64` lane has
`sizeof(size_t) == 8`, so the 64-bit allocation-mask arithmetic
(`_allocated_bitmask` with the size_t MSB) and the `Heap used + Heap free ==
Heap total` arithmetic are live-verified there; `_pointer_bits` still falls
back to 32 only when no type information exists at all. The cross-check
`mismatch` and walk `corrupt` verdicts have live evidence from the static
snapshot variant's crafted cells (free-list skips a linear-free block; a
free-list member carrying the size_t MSB), since a healthy kernel cannot
produce a corrupt heap.

**Object discovery is a six-channel provenance model, not a registry walk.**
FreeRTOS keeps no global object registry for most kinds (only the optional
`xQueueRegistry` and the MPU wrappers v2 pool), so GDR discovers objects from
six channels, each of which attaches its origin to the object:

| channel | source | covers |
| --- | --- | --- |
| MPU pool | `xKernelObjectPool` (file-static, wrappers v2 only) | all kinds |
| registry | `xQueueRegistry`, whole array scanned (holes included) | queue/semaphore/mutex |
| active | `pxCurrentTimerList` / `pxOverflowTimerList` | timers (active only) |
| symbol | one `info variables` scan per session, cached; `Static*_t` buffers use the symbol address, `*Handle_t` uses the pointer value (translated through the kernel object pool on MPU builds, where the handle is an opaque slot index) | all kinds |
| waiter | reverse `container_of` from a blocked TCB's `xEventListItem.pxContainer`, confirmed by list membership and struct plausibility | queue/semaphore/mutex/event group |
| user | explicit `0x`/decimal address or symbol name | all kinds |

`discover(kind)` merges the channels in that priority order, deduplicating by
address: the first channel to find an address keeps its name and stays the
source of record, and every later channel that finds the same address is
recorded as an extra source. The `Src` cell therefore shows the full evidence
(`registry+symbol+waiter`), because corroboration by several channels is
stronger evidence than a single heuristic hit — which matters exactly when a
name or kind looks wrong. The `frt objects` summary still counts each object
under its source of record only, so the per-source counts keep summing to the
kind's total; it prints that `source=count` breakdown plus the enumeration
limitation, so a count is never mistaken for a complete inventory:
unregistered dynamic objects without a global handle (and stopped or expired
timers) are simply not reachable from any channel.

Automatic views show only named failures and incomplete reads/walks, retaining
`fail:`/`skipped:` prefixes so merged diagnostic columns preserve severity.
`frt checks [scope] [name]` is the explicit exhaustive surface (passes and
structural skips included), with object addresses and observed pointer ranges,
fields or block locations. Checks never drive the target; an interrupted kernel
update can be transient. `frt system` uses `SystemSummary.extra_pairs` without
introducing RTOS semantics into the core. NextUnblockTime is skipped before
scheduler startup and while suspended; an earlier cached deadline is legitimate
after event unblocking, while a later cache reports the delayed item and wake tick.
Heap mismatches name differing free blocks and `xFreeBytesRemaining`; corrupt
walks retain their exact stop reason. No healthy-view aggregate counts claim that
an unperformed check passed.

A singular detail command refuses a kind it can prove wrong: resolving a name
or address stamps the *requested* kind on whatever it found, so a wrong
kind must be refused before rendering. The resolved object is refined
through the `Queue_t`
discriminator and, when the actual kind is known and different, the command
replies with a redirect (`'gdr_mutex' is a mutex, not a semaphore; try
`freertos mutex gdr_mutex``). An *inferred* kind never justifies a refusal —
without `configUSE_TRACE_FACILITY` the discriminator cannot tell a binary from
a counting semaphore, so the detail renders with the usual `?` marker.

Two deliberate heuristics: the registry and MPU-pool queue slots only prove "some
`Queue_t`", so the discovery layer refines them through the `Queue_t`
discriminator — with `configUSE_TRACE_FACILITY` the kind is definitive, without
it only the family is known and `inferred_kind` stays set (rendered with a `?`).
The waiter channel validates hosts by field plausibility (`uxLength`/`uxMessagesWaiting`/
`uxItemSize`, non-null `pcHead` for item queues, and the
`xListEnd.xItemValue == portMAX_DELAY` sentinel on both waiting lists), which
can in principle still misread plausible neighbouring memory — every other
channel outranks it.

The `mpu-pool` channel is live on the `mpu` variant (see the "Closed by the
fixture matrix" list); the empty/reserved-slot skip, kind mapping and
QUEUE-slot `inferred_kind` semantics were unit-tested before any bootable
fixture existed.  On an MPU build the application-visible handle is an opaque
pool index (wrappers v2 `CONVERT_TO_EXTERNAL_INDEX` = slot + 1), never a
pointer, so the symbol and user channels translate such a value through
`xKernelObjectPool[value - 1].xInternalObjectHandle` (and skip a value that
is neither a valid index nor a pointer inside a mapped section) instead of
reading `0x3` as an address. The `waiter` channel is pinned by a deliberate
fixture object:
`gdr_waiter_only_eg_task` (ci/freertos/fixture/main.c) blocks forever on an
event group whose handle lives only on its own stack, so that group is
discoverable *only* through the waiter channel -- the live no-ghost assertion
now permits exactly one host (that anonymous event group) that no earlier
channel finds, and would fail if the container_of probe fabricated objects out
of plausible neighbouring memory beyond it.
