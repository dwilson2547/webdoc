FROM node:22-slim AS ui
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY ui/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /srv
COPY api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY api/webdoc/ ./webdoc/
COPY --from=ui /ui/dist/ ./static/
RUN useradd --uid 10001 --no-create-home webdoc
USER 10001
ENV STATIC_DIR=/srv/static
EXPOSE 8080
# Traefik in front sets X-Forwarded-*; trust it from any in-cluster peer.
CMD ["uvicorn", "--factory", "webdoc.main:create_app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*", "--timeout-graceful-shutdown", "5"]
