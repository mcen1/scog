FROM docker.io/redhat/ubi10@sha256:d9a6082302bb48a977754911118db0b7d2f0cac900c64f4897651c2dc09ca0a1

RUN dnf install -y git python3 python3-pip && \
    dnf clean all && \
    useradd notroot -u 10001 && \
    mkdir -p /notroot /artifacts && \
    python3 -m venv /notroot/.venv && \
    chown -R notroot:notroot /notroot /artifacts

ENV PATH="/notroot/.venv/bin:${PATH}"

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --upgrade pip==26.1.2 && \
    pip install -r /app/requirements.txt

COPY --chmod=0755 entrypoint.sh /entrypoint.sh
COPY src/ /app/src/
COPY wsgi.py index.html /app/

WORKDIR /app
USER notroot

VOLUME /artifacts

# Declarative configuration; see entrypoint.sh and src/config.py.
ENV ARTIFACT_DIR=/artifacts
ENV PORT=5000
ENV UI_ENABLED=true
ENV UPSTREAM_URL=https://galaxy.ansible.com

EXPOSE 5000
ENTRYPOINT ["/entrypoint.sh"]
