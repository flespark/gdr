# GDR CI base image — generic toolchain for RTOS × hardware combos.
#
# Used by both local `docker run` and CNB `.cnb.yml` `docker.build`. Provides
# generic tools reusable across RTOS adapters and target boards:
#   - qemu-system-arm  (QEMU full-system emulation; vexpress-a9 today, mps2/other later)
#   - gdb-multiarch    (GDB with Python scripting, ARM target support)
#   - arm-none-eabi-*  (GCC cross-toolchain for ARM Cortex-A/M bare-metal builds)
#   - python3 + uv     (Python deps + pytest/pexpect runner)
#   - scons            (RT-Thread BSP build tool; kept here because it's
#                       apt-installable and harmless for other RTOS paths)
#
# Design principle: ONE base Dockerfile covers all RTOS × hardware combos
# until a real dependency conflict appears (e.g. one RTOS needs python2,
# another needs python3, or incompatible GCC majors). Premature splits
# multiply maintenance burden without value. See `TODO.md` "future evolution"
# note for the splitting trigger.
#
# Base choice: debian:trixie has recent enough gdb (>= 14) and qemu (>= 8)
# to support modern ARM debug info and -gdb tcp::1234 server semantics.

FROM debian:trixie-slim

#----------------------------------------------------------------------
# Toolchain: QEMU, GDB (multiarch so it can talk to ARM targets), and the
# GCC arm-none-eabi cross-compiler used by the RT-Thread qemu-vexpress-a9
# BSP.  We install `python3` and `pip` here too so `uv` can bootstrap.
#----------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
        qemu-system-arm \
        gdb-multiarch \
        gcc-arm-none-eabi \
        libnewlib-arm-none-eabi \
        libstdc++-arm-none-eabi-newlib \
        binutils-arm-none-eabi \
        python3 \
        python3-pip \
        python3-venv \
        git \
        curl \
        ca-certificates \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

#----------------------------------------------------------------------
# uv: fast Python package installer used for `uv sync --group dev`.
# Pinned to a recent stable; revisit on dependency change.
#----------------------------------------------------------------------
RUN curl -LsSf https://astral.sh/uv/0.5.11/install.sh | sh \
    && ln -sf /root/.local/bin/uv /usr/local/bin/uv

#----------------------------------------------------------------------
# scons: RT-Thread build tool. Install via pip in an isolated venv to
# avoid apt's older SCons and to keep this layer reproducible.
#----------------------------------------------------------------------
RUN python3 -m venv /opt/scons-venv \
    && /opt/scons-venv/bin/pip install --no-cache-dir "scons>=4.6,<5" \
    && ln -sf /opt/scons-venv/bin/scons /usr/local/bin/scons

# Default working directory; CNB mounts the repo at /root/workspace by
# convention, but we leave WORKDIR generic so local `docker run -v` works.
WORKDIR /workspace

# Sanity: print versions on container start so failures are diagnosable.
CMD ["bash", "-lc", "echo qemu-system-arm:; qemu-system-arm --version | head -1; \
                     echo gdb-multiarch:;   gdb-multiarch --version | head -1; \
                     echo arm-none-eabi-gcc:; arm-none-eabi-gcc --version | head -1; \
                     echo python3:;          python3 --version; \
                     echo uv:;               uv --version; \
                     echo scons:;            scons --version | head -3"]