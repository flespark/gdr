# Integration test tooling

`python3 ci/test.py` is the public entry point for local and hosted RTOS
integration tests. It requires Python 3.10+; `--help` and `list` require no
installed dependencies and do not initialize the cache. Run examples from
the repository root; an absolute path to `ci/test.py` works from elsewhere.

## Quick start

```bash
python3 ci/test.py cache init
python3 ci/test.py setup
python3 ci/test.py list smoke
python3 ci/test.py doctor freertos mps2-an385 11.1.0 full
python3 ci/test.py run freertos mps2-an385 11.1.0 full -- -k backtrace
```

On first interactive use, cache initialization offers either the default
directory or a link to another location. To choose explicitly, before
`ci/.cache` exists:

```bash
python3 ci/test.py cache init --directory /path/to/gdr-cache
python3 ci/test.py cache path
```

An existing cache is never moved, replaced, or deleted automatically. A
broken link must be repaired explicitly. Neither cache-root environment
variables nor developer-home paths configure this runner.

## Commands

| Command | Contract |
| --- | --- |
| `cache init [--directory DIR]` | Create the cache or link an absent cache to a directory. |
| `cache path` | Print the logical and resolved cache paths without creating them. |
| `setup` | Register installed tools with platform-specific links; no tool downloads. |
| `list [GROUP]` | List supported RTOS/target/version/variant coordinates. |
| `doctor RTOS TARGET VERSION [VARIANT]` | Check compiler, QEMU, GDB Python/ISA support, and RT-Thread build dependencies. |
| `build RTOS TARGET VERSION [VARIANT]` | Prepare a release checkout and build or reuse one firmware cell. |
| `run RTOS TARGET VERSION [VARIANT] -- [PYTEST_ARGS]` | Preflight, build if needed, and execute selected integration tests. |
| `matrix GROUP -- [PYTEST_ARGS]` | Run a named matrix through the same build/run functions. |
| `container init\|build\|run` | Initialize an isolated Podman environment, build its image, or invoke the CLI inside it. |

Each command has `--help`. `build`, `run`, and `matrix` accept `--rebuild`;
`run` and `matrix` support the GDB 12/Python 3.10 gate with `--gdb12`.
Use `--` to separate CLI options from pytest arguments:

```bash
python3 ci/test.py run rtthread cortex-a9 4.0.5 -- tests/integration/rtthread/test_backtrace.py -x
python3 ci/test.py run bare qemu-virt-rv64 none eh-frame
python3 ci/test.py run freertos mps2-an521 11.1.0 snapshot
python3 ci/test.py matrix freertos-ports -- -k backtrace
```

An explicit pytest path replaces the default suite selection. Otherwise,
live lanes include the RTOS suite and shared panel-table tests. The
`snapshot` and `eh-frame` cells select their dedicated tests. A successful
pytest exit with no executed tests is an error, except for collection/help.
Individual capability-specific skips remain valid. Direct pytest runs can
skip absent tools/artifacts, but never build or fetch firmware.

Matrix groups are defined in `testing/matrix.py`: RT-Thread (`rtthread-arm-ports`,
`rtthread-a9`, `rtthread-rv32`, `rtthread-rv64`, `rtthread-riscv`, `rtthread-ports`,
`rtthread`), FreeRTOS (`freertos-snapshot`, `freertos-config`, `freertos-arm-version`,
`freertos-arm-m0`, `freertos-arm-m4`, `freertos-arm-m33`, `freertos-arm-smp-mpu`,
`freertos-rv32`, `freertos-rv64`, `freertos-version`, `freertos-ports`, `freertos`),
`bare`, `gdb12`, `gdb-latest`, `smoke`, and `all`. The A9 matrix builds
3.1.1, 3.1.2, and 3.1.4 without running tests; explicit `run` or forwarded
pytest arguments execute them. `list all` also lists supported releases
outside the default matrix.

## Cache and provenance

All integration-owned writable state uses the ignored `ci/.cache` root
(or its symlink destination):

```text
ci/.cache/
  tools/<host>/             registered tool links and managed Python
  sources/<rtos>/<tag>/     clean, tagged upstream checkouts
  build/<host>/<lane>/      private build directories and build.log
  fixtures/<lane>/          published ELF/BIN/metadata and manifest.json
  env/<host>/<os-python>/   locked development virtual environments
  logs/<host>/<lane>/       pytest.log, results.xml, and test temporaries
  locks/                   firmware, source, tool and environment locks
  downloads/               uv downloads
  containers/              dedicated Podman configuration, images and VM state
  tmp/                     tool caches and temporary files
```

`<lane>` means `<rtos>/<target>/<version>[/<variant>]`. `setup` links tools
already on `PATH`; ARM and RISC-V compiler distributions remain intact.
Native compiler installations are not copied. `doctor` rejects missing or
incompatible tools before downloading sources. RT-Thread 3.1.x requires
Python 2/SCons 3; the shared container supplies that isolated dependency.

A firmware manifest fingerprints fixture/build inputs and compiler
identity. Reuse requires both a matching fingerprint and every non-empty
artifact. `--rebuild` bypasses reuse. Failed builds retain their logs but
cannot publish a successful manifest. Modified source checkouts are rejected,
not reset or cleaned. Same-lane operations fail on concurrent access; shared
source and environment operations serialize. Logs persist until explicitly
removed by the maintainer.

## Containers

Install Podman, then initialize its dedicated environment on macOS:

```bash
python3 ci/test.py container init
python3 ci/test.py container stop
python3 ci/test.py container build
python3 ci/test.py container run -- run freertos mps2-an385 11.1.0 full -- -k backtrace
python3 ci/test.py container run --no-build -- matrix smoke
python3 ci/test.py container run --profile gdb12 -- matrix gdb12
python3 ci/test.py container run --profile gdb-latest -- matrix gdb-latest
python3 ci/checks/gdb_api.py --gdb gdb-multiarch
python3 ci/checks/build_latest_gdb.py --prefix /opt/gdb-latest
```

Image builds download pinned toolchains and can be large. `--no-build`
requires an existing image. The default QEMU image follows the host CPU;
`--platform linux/amd64` or `linux/arm64` selects explicitly. The `gdb12` and
`gdb-latest` compatibility baselines are amd64-only. Repository and external-cache mounts retain their
absolute paths, including symlink targets. Container internals such as
`/opt` belong to the cache-owned image rather than the host filesystem.

On macOS, `container build` and `container run` start `gdr-ci` when it is
stopped and restart it when its recorded state is stale; `--no-start` opts out.
`container init` remains the only command that creates the machine. A machine
whose VM process is wedged is not force-removed; restart it or the host, then
retry. `container stop` stops a running `gdr-ci`; an absent or already stopped
machine is a no-op. On Linux, CI uses native Podman and `container stop` is a
no-op.

## Layering and new lanes

| Path | Responsibility |
| --- | --- |
| `test.py` | Bootstrap imports and exit status. |
| `testing/cli.py` | Public argument parsing and dispatch. |
| `testing/cache.py` | Cache/link safety and host identity. |
| `testing/matrix.py` | Explicit coordinates, artifacts, and matrix policy. |
| `testing/runner.py` | Tools, locks, sources, fingerprints, builds, and pytest transport. |
| `testing/containers.py` | Podman configuration, image builds, and mounts. |
| `fixture/freertos/build.sh`, `fixture/freertos/lib.sh` | FreeRTOS-specific compilation and artifact installation. |
| `fixture/rtthread/build.sh` | RT-Thread-specific BSP cloning, patches, and compilation. |
| `checks/`, `containers/`, `release/` | Tool gates, image definitions, and runtime release packaging. |

Builders are internal interfaces; they receive explicit paths and do not
orchestrate pytest. Extension packaging and schema scripts live under
`vscode/scripts/`. Build details live in the [FreeRTOS](fixture/freertos/README.md)
and [RT-Thread](fixture/rtthread/README.md) lane READMEs; coverage lives in
[testing](../docs/testing.md).

For unwind work, add deterministic board/config/assembly inputs under the
owning RTOS and declare the lane coordinates and artifacts. CFI-expression
semantics have an isolated `.eh_frame` lane. Exception and scheduler work
requires separate architecture and RTOS-port acceptance; see
[verification requirements](../docs/testing.md#exception-unwind-acceptance).
A signal CIE alone does not prove hardware IRQ or cross-stack support.
Add independent positive and negative expectations under
`tests/integration/<rtos>/`, then include the lane in an appropriate matrix.
New capabilities need a falsifiable fixture before runtime support is opened.
