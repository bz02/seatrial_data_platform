# Pinned CAD sandbox: build123d + CadQuery on a fixed OCP kernel, matplotlib renderer.
# render_view and validate_cad_model write <file>.metrics.json, which the host-side grader reads.
# apt packages come from the Debian bookworm snapshot of the base image; pip packages are pinned.
FROM python:3.12.8-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglu1-mesa \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir cadquery-ocp==7.7.2 cadquery==2.4.0 build123d==0.8.0 matplotlib==3.9.2 \
      numpy==1.26.4 pytest==8.3.4
COPY tools.py tool_server.py cad_tools.py /opt/seatrail/
RUN ln -s /opt/seatrail/cad_tools.py /usr/local/bin/render_view \
 && ln -s /opt/seatrail/cad_tools.py /usr/local/bin/validate_cad_model \
 && chmod +x /opt/seatrail/cad_tools.py
RUN useradd -m -u 10001 agent && mkdir -p /workspace && chown agent /workspace
USER agent
WORKDIR /workspace
CMD ["python", "/opt/seatrail/tool_server.py", "--mode", "auto"]
