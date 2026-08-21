# GDR CI base image — generic toolchain for RTOS × hardware combos.
#
# Used by both local `docker run` and CNB `.cnb.yml` `docker.build`. Provides
# generic tools reusable across RTOS adapters and target boards:
#   - qemu-system-arm + qemu-system-misc (Cortex-A9 and RISC-V RV64 emulation)
#   - gdb-multiarch    (GDB with Python scripting and multi-ISA support)
#   - xPack GNU Arm + RISC-V GCC/newlib (bare-metal cross-compilers)
#   - python3 + uv     (Python deps + pytest/pexpect runner)
#   - SCons 4/Python 3 (RT-Thread 4.x BSP build tool)
#   - SCons 3/Python 2 (isolated RT-Thread 3.1.x BSP build tool)
#
# Design principle: ONE base Dockerfile covers all RTOS × hardware combos
# until a dependency cannot coexist in isolated paths. RT-Thread 3.1.x needs
# Python 2, but its interpreter and SCons stay under /opt/python2 and never
# become the container default, so a separate image is unnecessary.
#
# Base choice: debian:trixie has recent enough gdb (>= 14) and qemu (>= 8)
# to support modern ARM debug info and -gdb tcp::1234 server semantics.

# Python 2.7 is EOL and is used only to run RT-Thread 3.1.x's Python-2-only
# build scripts. Keep it out of the final image's default Python environment.
FROM python:2.7.18-slim-buster AS legacy-scons

RUN pip install --no-cache-dir "scons==3.1.2"


FROM debian:trixie-slim

COPY --from=legacy-scons /usr/local /opt/python2
COPY --from=legacy-scons /usr/lib/x86_64-linux-gnu/libcrypto.so.1.1 /opt/python2/lib/
COPY --from=legacy-scons /usr/lib/x86_64-linux-gnu/libssl.so.1.1 /opt/python2/lib/

#----------------------------------------------------------------------
# Runtime tools. Host GCC and pip are unnecessary: RT-Thread uses the xPack
# cross-compilers and SCons is installed in isolated environments.
#----------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
        qemu-system-arm \
        qemu-system-misc \
        gdb-multiarch \
        python3 \
        python3-venv \
        git \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -LsSf https://astral.sh/uv/0.5.11/install.sh | sh \
    && ln -sf /root/.local/bin/uv /usr/local/bin/uv \
    && python3 -m venv /opt/scons-venv \
    && /opt/scons-venv/bin/pip install --no-cache-dir "scons>=4.6,<5" \
    && ln -sf /opt/scons-venv/bin/scons /usr/local/bin/scons

#----------------------------------------------------------------------
# xPack GNU GCC/newlib toolchains. Archive hashes are from the corresponding
# GitHub release assets and keep toolchain updates deliberate and reviewable.
#----------------------------------------------------------------------
ARG XPACK_ARM_VERSION=15.2.1-1.1
ARG XPACK_ARM_SHA256=da6a49ad4003944b823c6c93702a8787c922ab34bd7e918ec0eaf6933a9b1ff6
ARG XPACK_RISCV_VERSION=15.2.0-1
ARG XPACK_RISCV_SHA256=aaaa8060c914851a3e5ee1ba82cc3d6f80972f90638a05c6e823a37557a33758
# Reason: RT-Thread's RV64 BSP hard-codes the legacy riscv64-unknown-elf prefix.
RUN arm_archive="xpack-arm-none-eabi-gcc-${XPACK_ARM_VERSION}-linux-x64.tar.gz" \
    && riscv_archive="xpack-riscv-none-elf-gcc-${XPACK_RISCV_VERSION}-linux-x64.tar.gz" \
    && curl --fail --location --retry 3 --output "/tmp/$arm_archive" \
        "https://github.com/xpack-dev-tools/arm-none-eabi-gcc-xpack/releases/download/v${XPACK_ARM_VERSION}/$arm_archive" \
    && echo "${XPACK_ARM_SHA256}  /tmp/$arm_archive" | sha256sum --check - \
    && curl --fail --location --retry 3 --output "/tmp/$riscv_archive" \
        "https://github.com/xpack-dev-tools/riscv-none-elf-gcc-xpack/releases/download/v${XPACK_RISCV_VERSION}/$riscv_archive" \
    && echo "${XPACK_RISCV_SHA256}  /tmp/$riscv_archive" | sha256sum --check - \
    && tar --extract --gzip --file "/tmp/$arm_archive" --directory /opt \
    && tar --extract --gzip --file "/tmp/$riscv_archive" --directory /opt \
    && rm "/tmp/$arm_archive" "/tmp/$riscv_archive" \
    && for tool in /opt/xpack-riscv-none-elf-gcc-${XPACK_RISCV_VERSION}/bin/riscv-none-elf-*; do \
        ln --symbolic --force "${tool##*/}" \
            "/opt/xpack-riscv-none-elf-gcc-${XPACK_RISCV_VERSION}/bin/riscv64-unknown-elf-${tool##*/riscv-none-elf-}"; \
    done \
    && arm_gcc="/opt/xpack-arm-none-eabi-gcc-${XPACK_ARM_VERSION}/bin/arm-none-eabi-gcc" \
    && riscv_gcc="/opt/xpack-riscv-none-elf-gcc-${XPACK_RISCV_VERSION}/bin/riscv64-unknown-elf-gcc" \
    && "$arm_gcc" --version | head -1 \
    && "$riscv_gcc" --version | head -1 \
    && printf '#include <stdio.h>\n' | "$arm_gcc" -E -x c - >/dev/null \
    && printf '#include <stdio.h>\n' | "$riscv_gcc" -E -x c - >/dev/null

ENV XPACK_ARM_TOOLCHAIN_PATH=/opt/xpack-arm-none-eabi-gcc-15.2.1-1.1/bin \
    XPACK_RISCV_TOOLCHAIN_PATH=/opt/xpack-riscv-none-elf-gcc-15.2.0-1/bin \
    PYTHON2_BIN=/opt/python2/bin/python \
    PYTHON2_SCONS=/opt/python2/bin/scons \
    PATH=/opt/xpack-arm-none-eabi-gcc-15.2.1-1.1/bin:/opt/xpack-riscv-none-elf-gcc-15.2.0-1/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# Fail the image build if the isolated Python 2 runtime cannot load the
# matching OpenSSL 1.1 extension modules copied from the Buster stage.
# Reason: keep /opt/python2/lib off the image-wide LD_LIBRARY_PATH; only this
# smoke check and build-rtt.sh's 3.1.x scons path need it.
RUN LD_LIBRARY_PATH=/opt/python2/lib \
    /opt/python2/bin/python -c 'import hashlib; assert hashlib.md5("gdr").hexdigest() == "fff7c7603b50cc39962ecf42277c0d0d"'

# Default working directory; CNB mounts the repo at /root/workspace by
# convention, but we leave WORKDIR generic so local `docker run -v` works.
WORKDIR /workspace
