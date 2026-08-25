# ReconProof web image.
#
# NEXT_PUBLIC_API_BASE is inlined into the client bundle at build time —
# every browser-side fetch in this app calls the API directly (see
# src/lib/api.ts), so it must resolve from the *reviewer's browser*, not from
# inside the Docker network. The default matches the host port docker-compose
# publishes the API on; override the build arg if you publish it elsewhere.
FROM node:20-slim AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM node:20-slim AS build
WORKDIR /app
ARG NEXT_PUBLIC_API_BASE=http://localhost:8817
ENV NEXT_PUBLIC_API_BASE=$NEXT_PUBLIC_API_BASE
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

FROM node:20-slim AS run
WORKDIR /app
ENV NODE_ENV=production
COPY --from=build /app/public ./public
COPY --from=build /app/.next ./.next
COPY --from=build /app/node_modules ./node_modules
COPY --from=build /app/package.json ./package.json

EXPOSE 43917

CMD ["npx", "next", "start", "--port", "43917", "--hostname", "0.0.0.0"]
