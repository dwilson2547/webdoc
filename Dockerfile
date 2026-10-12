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
# The search embedding model (design §8) ships in the image: the root
# filesystem is read-only at run time, so nothing is downloaded then.
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('sentence-transformers/all-MiniLM-L6-v2', cache_dir='/srv/models')" \
 && chmod -R a+rX /srv/models
COPY api/webdoc/ ./webdoc/
COPY --from=ui /ui/dist/ ./static/
RUN useradd --uid 10001 --no-create-home webdoc
USER 10001
ENV STATIC_DIR=/srv/static MODEL_DIR=/srv/models HF_HUB_OFFLINE=1
EXPOSE 8080
# Traefik in front sets X-Forwarded-*; trust it from any in-cluster peer.
CMD ["uvicorn", "--factory", "webdoc.main:create_app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*", "--timeout-graceful-shutdown", "5"]
