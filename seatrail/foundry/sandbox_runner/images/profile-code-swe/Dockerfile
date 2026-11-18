# Pinned SWE sandbox. Rebuild only by bumping versions here; tasks reference the image by digest.
# apt packages come from the Debian bookworm snapshot of the base image; pip packages are pinned.
FROM python:3.12.8-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends \
      git ripgrep jq \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir pytest==8.3.4
COPY tools.py tool_server.py /opt/seatrail/
RUN useradd -m -u 10001 agent && mkdir -p /workspace && chown agent /workspace
USER agent
WORKDIR /workspace
# Default: MCP stdio tool server. The runner usually starts `sleep infinity` and uses docker exec.
CMD ["python", "/opt/seatrail/tool_server.py", "--mode", "agentic_native"]
