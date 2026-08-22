# DBA Ops Assistant — Ubuntu runtime image.
#
# The image mirrors the repository layout under /app/tools/db_ops so that the
# daemon's REPO_ROOT/TOOL_ROOT path math and the app_commands "working_dir"
# values ("tools/db_ops") resolve exactly as they do on a developer checkout.
# Secrets are read (encrypted) from the mounted data/ dir; the passphrase is
# supplied at runtime via --key and is never baked into the image.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    ACCEPT_EULA=Y \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:/opt/mssql-tools18/bin:$PATH \
    LANG=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8

# System dependencies:
#   - unixodbc + msodbcsql17/18 + mssql-tools18 : SQL Server access via pyodbc / sqlcmd
#     (driver 17 is kept for legacy SQL Server 2008 R2 hosts that driver 18's
#      stricter TLS/cert defaults reject)
#   - powershell (pwsh)                      : Windows-target remote operations from Linux
#   - openssh-client, rsync, smbclient       : Linux-target copy/exec and SMB copy
# Ubuntu apt mirror. Defaults to an HTTPS regional mirror: the default
# archive/security.ubuntu.com endpoints over HTTP are intercepted/corrupted on
# some networks (403s and index/package hash-sum mismatches from a transparent
# proxy). HTTPS bypasses that; GPG signatures still verify package integrity, so
# TLS peer verification is disabled to avoid a ca-certificates bootstrap problem.
# Override for other environments:
#   docker build --build-arg UBUNTU_MIRROR=http://archive.ubuntu.com/ubuntu ...
ARG UBUNTU_MIRROR=https://mirror.sg.gs/ubuntu

RUN set -eux; \
    # Skip TLS peer/host verification for apt only (integrity guaranteed by GPG). \
    APT_OPTS="-o Acquire::Retries=5 -o Acquire::https::Verify-Peer=false -o Acquire::https::Verify-Host=false"; \
    # Point apt at the chosen mirror and drop the unused noble-backports suite. \
    sed -i "s|http://archive.ubuntu.com/ubuntu|${UBUNTU_MIRROR}|g; s|http://security.ubuntu.com/ubuntu|${UBUNTU_MIRROR}|g; s/ noble-backports//g" /etc/apt/sources.list.d/ubuntu.sources 2>/dev/null || true; \
    sed -i "s|http://archive.ubuntu.com/ubuntu|${UBUNTU_MIRROR}|g; s|http://security.ubuntu.com/ubuntu|${UBUNTU_MIRROR}|g; /noble-backports/d" /etc/apt/sources.list 2>/dev/null || true; \
    apt-get $APT_OPTS update; \
    apt-get $APT_OPTS install -y --no-install-recommends \
        ca-certificates curl gnupg apt-transport-https locales \
        python3 python3-venv \
        unixodbc unixodbc-dev \
        openssh-client rsync smbclient \
        docker.io docker-compose-v2; \
    # docker client + `docker compose` plugin: the SRE create-db-docker command drives
    # the HOST docker daemon (via the mounted /var/run/docker.sock) to provision lab DB
    # containers. Only the client is used; the bundled dockerd is never started here. \
    locale-gen en_US.UTF-8; \
    curl -sSL -O https://packages.microsoft.com/config/ubuntu/24.04/packages-microsoft-prod.deb; \
    dpkg -i packages-microsoft-prod.deb; \
    rm packages-microsoft-prod.deb; \
    apt-get $APT_OPTS update; \
    apt-get $APT_OPTS install -y --no-install-recommends msodbcsql17 msodbcsql18 mssql-tools18 powershell; \
    # Allow legacy TLS 1.0 so the ODBC drivers can reach old SQL Server 2008 R2 \
    # hosts. OpenSSL 3 blocks TLS < 1.2 by default; those servers only offer \
    # TLS 1.0, so without this every connect fails with \
    # "SSL routines::unsupported protocol". (Windows/SChannel allows it, which is \
    # why the same targets work from the PC but not the Linux container.) \
    sed -i '/^\[openssl_init\]/a ssl_conf = ssl_sect' /etc/ssl/openssl.cnf; \
    printf '\n[ssl_sect]\nsystem_default = system_default_sect\n\n[system_default_sect]\nMinProtocol = TLSv1.0\nCipherString = DEFAULT@SECLEVEL=0\n' >> /etc/ssl/openssl.cnf; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*

# Isolated virtualenv keeps Python deps off the PEP 668 system interpreter.
RUN python3 -m venv "$VIRTUAL_ENV"

WORKDIR /app/tools/db_ops

# Install Python deps first so this layer caches across source changes.
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# Application source. config/data/sql/secrets are supplied at runtime via mounts
# (see docker-compose.yml) and are intentionally NOT baked into the image.
COPY db_ops ./db_ops

# Install it, rather than relying on the source being in the working directory.
#
# Copying alone is enough for the daemon, which runs from this WORKDIR — and it is why the image
# had no `db-ops` command and `python -m db_ops...` failed from anywhere else. Both are what the
# documentation tells a reader to type, so an image that cannot do them is an image the docs are
# wrong about. `--no-deps` because requirements.txt above already resolved them, and installing
# again would let a transitive pin drift between the two layers.
COPY pyproject.toml README.md ./
RUN pip install --no-deps .

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Run as a normal user. The daemon reads databases over the network and writes logs; it needs no
# privilege in the container, and an image that ships as root gives every operator of it more than
# they asked for.
#
# The uid is **pinned to 10001** rather than left to the distribution's next-free number, because
# the thing that matters is the number a host has to `chown` its bind mounts to — and that number
# has to be stable across rebuilds or the instruction in the docs stops being true.
#
# `data/`, `logs/` and `runtime/` arrive as mounts owned by the host user. Where they are not
# writable the entrypoint says so and names the fix; where an operator would rather not chown
# anything, `--user 0:0` (or `user: "0:0"` in compose) restores the old behaviour in one line.
ARG APP_UID=10001
RUN groupadd --gid "${APP_UID}" dbabrain \
    && useradd --uid "${APP_UID}" --gid "${APP_UID}" --create-home --shell /bin/bash dbabrain \
    && chown -R dbabrain:dbabrain /app "$VIRTUAL_ENV"
USER dbabrain

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["daemon"]
