# =============================================================================
# Next.js frontend — multi-stage.
#   target: dev   -> hot-reload dev server
#   target: prod  -> standalone build, non-root
# =============================================================================

FROM node:22-alpine AS base
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1

# Dependency manifests only, so the install layer is reused whenever just the
# application source changed.
COPY package.json package-lock.json* ./


# --- dev ---------------------------------------------------------------------
FROM base AS dev
# `npm ci` needs a lockfile; fall back to `npm install` on a first run when one
# has not been generated yet.
RUN npm ci 2>/dev/null || npm install
COPY . .
EXPOSE 3000
CMD ["npm", "run", "dev"]


# --- build -------------------------------------------------------------------
FROM base AS build
RUN npm ci 2>/dev/null || npm install
COPY . .
RUN npm run build


# --- prod --------------------------------------------------------------------
FROM node:22-alpine AS prod
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1

RUN addgroup -g 10001 -S nodejs && adduser -S -u 10001 -G nodejs nextjs

COPY --from=build /app/public ./public
COPY --from=build --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=build --chown=nextjs:nodejs /app/.next/static ./.next/static

USER nextjs
EXPOSE 3000
CMD ["node", "server.js"]
