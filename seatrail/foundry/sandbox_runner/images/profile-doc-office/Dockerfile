# Pinned office-documents sandbox: headless LibreOffice, poppler, python-docx/openpyxl/pptx, PyMuPDF.
# apt packages come from the Debian bookworm snapshot of the base image; pip packages are pinned.
FROM python:3.12.8-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends \
      libreoffice-writer-nogui libreoffice-calc-nogui \
      poppler-utils \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir python-docx==1.1.2 openpyxl==3.1.5 python-pptx==1.0.2 pymupdf==1.24.14 \
      scikit-image==0.24.0 pytest==8.3.4
COPY tools.py tool_server.py /opt/seatrail/
RUN useradd -m -u 10001 agent && mkdir -p /workspace && chown agent /workspace
USER agent
WORKDIR /workspace
CMD ["python", "/opt/seatrail/tool_server.py", "--mode", "agentic_native"]
