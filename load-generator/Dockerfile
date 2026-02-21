################################################################################
# wrk2 Load Generator for Chaos Benchmark
# Multi-stage build: compile wrk2 from DSB source, bake in custom Lua scripts
################################################################################

FROM ubuntu:22.04 AS builder

WORKDIR /workspace

RUN apt-get update -y && \
    apt-get install -y libssl-dev libz-dev luarocks make gcc && \
    luarocks install luasocket

# Copy wrk2 source from DeathStarBench submodule
COPY wrk2/src/ src/
COPY wrk2/deps/ deps/
COPY wrk2/Makefile Makefile

RUN make -j$(nproc) && make install

FROM ubuntu:22.04

RUN apt-get update -y && \
    apt-get install -y --no-install-recommends libssl3 && \
    rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/bin/wrk /usr/local/bin/wrk
COPY --from=builder /usr/local/lib/lua /usr/local/lib/lua
COPY --from=builder /usr/local/lib/luarocks /usr/local/lib/luarocks
COPY --from=builder /usr/local/share/lua /usr/local/share/lua

# Bake in our custom Lua workload script
COPY mixed-workload.lua /scripts/mixed-workload.lua

ENTRYPOINT ["/usr/local/bin/wrk"]
