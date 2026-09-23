FROM oven/bun:1 AS deps
WORKDIR /app
COPY package.json bun.lock* ./
RUN bun install --frozen-lockfile || bun install

FROM oven/bun:1 AS build
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# NEXT_PUBLIC_* values are read from frontend/.env.production at this point
# and inlined into the client bundle. To override per-environment, drop a
# frontend/.env.production.local (gitignored) into the build context, or pass
# the NEXT_PUBLIC_API_URL build arg (docker-compose.dev.yml does, because the
# dev stack has no nginx to serve the relative /api/v1).
ARG NEXT_PUBLIC_API_URL=""
ENV NEXT_TELEMETRY_DISABLED=1
RUN if [ -n "$NEXT_PUBLIC_API_URL" ]; then \
      echo "NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL" >> .env.production.local; \
    fi \
 && bun run build

FROM oven/bun:1 AS run
WORKDIR /app
ENV NODE_ENV=production
COPY --from=build /app/node_modules ./node_modules
COPY --from=build /app/.next ./.next
COPY --from=build /app/public ./public
COPY --from=build /app/package.json ./package.json
COPY --from=build /app/next.config.ts ./next.config.ts
EXPOSE 3000
CMD ["bun", "run", "start"]
