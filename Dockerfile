# ECOA Tools Development Environment
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
WORKDIR /workspace

# -------------------------------------------------------------------
# Switch to TUNA mirrors
# -------------------------------------------------------------------
RUN sed -i 's|archive.ubuntu.com|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list && \
    sed -i 's|security.ubuntu.com|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list

# -------------------------------------------------------------------
# Install system deps + Python 3.10 (official)
# -------------------------------------------------------------------
RUN apt-get update && apt-get install -y \
    python3 \
    python3-venv \
    python3-dev \
    build-essential \
    gcc \
    g++ \
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
    git \
    ca-certificates \
    curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# -------------------------------------------------------------------
# Copy project
# -------------------------------------------------------------------
COPY . /workspace/

# -------------------------------------------------------------------
# Create venv (python3 == 3.10)
# -------------------------------------------------------------------
RUN python3 -m venv /workspace/.venv

ENV VIRTUAL_ENV=/workspace/.venv
ENV PATH="/workspace/.venv/bin:$PATH"

# -------------------------------------------------------------------
# pip mirror (TUNA)
# -------------------------------------------------------------------
RUN mkdir -p /root/.pip && \
    printf "[global]\nindex-url = https://pypi.tuna.tsinghua.edu.cn/simple\n" > /root/.pip/pip.conf

# -------------------------------------------------------------------
# Install Python deps
# -------------------------------------------------------------------
RUN python -m pip install --upgrade "pip>=23" && \
    python -m pip install --upgrade "setuptools<82" wheel && \
    python -m pip install -r requirements.txt

# -------------------------------------------------------------------
# Editable installs
# -------------------------------------------------------------------
RUN for d in \
    ecoa-toolset \
    ecoa-exvt \
    ecoa-csmgvt \
    ecoa-mscigt \
    ecoa-asctg \
    ecoa-ldp; do \
        if [ -d "/workspace/as6-tools/$d" ]; then \
            pip install -e "/workspace/as6-tools/$d"; \
        fi; \
    done

EXPOSE 5000
CMD ["python", "main.py"]