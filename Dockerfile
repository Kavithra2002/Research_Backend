# Express API + Python extraction scripts (Render / any Docker host)
FROM node:20-bookworm-slim AS builder

RUN apt-get update \
  && apt-get install -y --no-install-recommends python3 python3-pip \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY scripts/requirements.txt ./scripts/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r scripts/requirements.txt

COPY package.json package-lock.json ./
RUN npm ci

COPY tsconfig.json ./
COPY app ./app
COPY scripts ./scripts

RUN npm run build

# --- production image ---
FROM node:20-bookworm-slim AS runner

RUN apt-get update \
  && apt-get install -y --no-install-recommends python3 python3-pip tini \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY scripts/requirements.txt ./scripts/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r scripts/requirements.txt

COPY package.json package-lock.json ./
RUN npm ci --omit=dev

COPY --from=builder /app/dist ./dist
COPY --from=builder /app/scripts ./scripts

ENV NODE_ENV=production
ENV PORT=4000
ENV SCRIPT_ROOT=/app
ENV SCRIPT_DIR=/app/scripts
ENV PYTHON_BIN=python3

EXPOSE 4000

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["node", "dist/server.js"]
