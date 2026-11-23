ARG PYTHON_IMAGE
FROM ${PYTHON_IMAGE} AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /workspace/backend
COPY backend/requirements.lock ./requirements.lock
RUN pip install --no-cache-dir --require-hashes -r requirements.lock && useradd --uid 10001 --create-home app
COPY backend/app ./app
COPY backend/alembic.ini ./alembic.ini
COPY database /workspace/database
RUN mkdir -p /workspace/artifacts && chown app:app /workspace/artifacts
USER app
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]

FROM runtime AS test
USER root
COPY backend/pyproject.toml ./pyproject.toml
COPY backend/requirements-dev.lock ./requirements-dev.lock
RUN pip install --no-cache-dir --require-hashes -r requirements-dev.lock
COPY backend/tests ./tests
RUN printf 'disposable-compose-test\n' > .disposable-test-image
COPY contracts /workspace/contracts
COPY scripts /workspace/scripts
USER app
CMD ["pytest", "-q", "-p", "no:cacheprovider"]
