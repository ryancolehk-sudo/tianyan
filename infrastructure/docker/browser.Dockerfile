ARG PLAYWRIGHT_IMAGE
FROM ${PLAYWRIGHT_IMAGE}
WORKDIR /workspace/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/playwright.config.ts ./
COPY frontend/src ./src
COPY frontend/tests ./tests
COPY scripts/capture_workbench_values.cjs /workspace/scripts/capture_workbench_values.cjs
RUN npx vite build --config tests/fixtures.vite.config.ts
CMD ["npx", "playwright", "test"]
