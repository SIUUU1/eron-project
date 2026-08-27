# TanStack Start 는 SSR 앱이다. 정적 산출물이 아니라 Node 서버가 나온다.
# 기본 빌드 타깃이 Cloudflare Workers 이므로 NITRO_PRESET 으로 바꾼다.
# vite.config.ts 는 Lovable 관리 파일이라 수정하지 않는다 (D8 확정).

FROM node:22-alpine AS build
WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .
ENV NITRO_PRESET=node-server
RUN npm run build


FROM node:22-alpine AS runtime
WORKDIR /app

ENV NODE_ENV=production \
    PORT=3000

COPY --from=build /app/.output ./.output

EXPOSE 3000
CMD ["node", ".output/server/index.mjs"]
