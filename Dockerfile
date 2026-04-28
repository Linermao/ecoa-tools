# ECOA Tools Development Environment
FROM docker.1ms.run/ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
WORKDIR /app

# -------------------------------------------------------------------
# Switch to TUNA mirrors, install system deps, create venv, install Python deps
# Combined into fewer layers to reduce image size
# -------------------------------------------------------------------
RUN sed -i 's|archive.ubuntu.com|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list && \
    sed -i 's|security.ubuntu.com|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list && \
    apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-venv \
    python3-dev \
    build-essential \
    gcc \
    g++ \
    gdb \
    gdbserver \
    cmake \
    make \
    bison \
    flex \
    libapr1-dev \
    libaprutil1-dev \
    libcunit1-dev \
    liblog4cplus-dev \
    libxml2-dev \
    libxslt1-dev \
    pkg-config \
    ca-certificates \
    curl \
    docker.io \
    docker-compose-v2 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# -------------------------------------------------------------------
# Copy project
# -------------------------------------------------------------------
COPY . /app/

# -------------------------------------------------------------------
# Create venv + install Python deps + editable installs (combined layer)
# -------------------------------------------------------------------
RUN python3 -m venv /app/.venv && \
    mkdir -p /root/.pip && \
    printf "[global]\nindex-url = https://pypi.tuna.tsinghua.edu.cn/simple\n" > /root/.pip/pip.conf

ENV VIRTUAL_ENV=/app/.venv
ENV PATH="/app/.venv/bin:$PATH"

RUN python -m pip install --no-cache-dir --upgrade "pip>=23" && \
    python -m pip install --no-cache-dir --upgrade "setuptools<82" wheel && \
    python -m pip install --no-cache-dir -r requirements.txt && \
    for d in \
    ecoa-toolset \
    ecoa-exvt \
    ecoa-csmgvt \
    ecoa-mscigt \
    ecoa-asctg \
    ecoa-ldp; do \
        if [ -d "/app/as6-tools/$d" ]; then \
            pip install --no-cache-dir -e "/app/as6-tools/$d"; \
        fi; \
    done && \
    rm -rf /root/.cache/pip /tmp/*

EXPOSE 5000
CMD ["python", "main.py"]
