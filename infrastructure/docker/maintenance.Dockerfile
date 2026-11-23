# Both base images are fixed by infrastructure/images.lock.env. PostgreSQL tools
# and their complete dynamic dependency set come from that same PG17 image; no
# mutable package repository or extra system dependency is installed here.
ARG PYTHON_IMAGE
ARG POSTGRES_IMAGE
FROM ${POSTGRES_IMAGE} AS postgres-client
USER root
RUN set -eu; mkdir -p /export/bin /export/lib; \
    cp /usr/lib/postgresql/17/bin/pg_dump /usr/lib/postgresql/17/bin/pg_restore /export/bin/; \
    ldd /export/bin/pg_dump /export/bin/pg_restore | awk '/=> \// { print $3 } /^[[:space:]]*\// && $1 !~ /:$/ { print $1 }' | sort -u | \
    while read dependency; do cp -L "$dependency" /export/lib/; done; \
    loader="$(ldd /export/bin/pg_dump | awk '/ld-linux/ { print $1; exit }')"; \
    cp -L "$loader" /export/lib/; ln -s "lib/$(basename "$loader")" /export/loader

FROM ${PYTHON_IMAGE} AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /workspace/backend
COPY backend/requirements.lock ./requirements.lock
RUN pip install --no-cache-dir --require-hashes -r requirements.lock && useradd --uid 10001 --create-home app
COPY --from=postgres-client /export /opt/postgresql
# Preserve the pinned distribution's PostgreSQL and runtime-library notices.
COPY --from=postgres-client /usr/share/doc /opt/postgresql/share/doc
RUN printf '#!/bin/sh\nexec /opt/postgresql/loader --library-path /opt/postgresql/lib /opt/postgresql/bin/pg_dump "$@"\n' > /usr/local/bin/pg_dump \
    && printf '#!/bin/sh\nexec /opt/postgresql/loader --library-path /opt/postgresql/lib /opt/postgresql/bin/pg_restore "$@"\n' > /usr/local/bin/pg_restore \
    && chmod 755 /usr/local/bin/pg_dump /usr/local/bin/pg_restore \
    && pg_dump --version && pg_restore --version
COPY backend/app ./app
RUN mkdir -p /workspace/backups /workspace/updates /workspace/maintenance-status && chown -R app:app /workspace/backups /workspace/updates /workspace/maintenance-status
USER app
CMD ["python", "-m", "app.maintenance.worker"]
