FROM docker.m.daocloud.io/library/node:22.21.1-bookworm-slim AS web-build
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm config set registry https://registry.npmmirror.com && npm ci
COPY frontend/index.html frontend/tsconfig.json frontend/vite.config.ts ./
COPY frontend/src ./src
RUN npm run build

FROM golang:1.24-alpine AS build
ENV GOPROXY=https://goproxy.cn,direct
ENV GOSUMDB=sum.golang.google.cn
WORKDIR /src
COPY go.mod ./
RUN go mod download
COPY go.sum ./
COPY cmd ./cmd
COPY internal ./internal
COPY examples/historical/A ./examples/historical/A
RUN gofmt -w ./cmd ./internal && go test ./... && CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/prototype ./cmd/prototype

FROM alpine:3.21
RUN apk add --no-cache ca-certificates tzdata wget && addgroup -S -g 10001 app && adduser -S -u 10001 -G app app && mkdir -p /identity /public_keys && chown -R 10001:10001 /identity /public_keys
COPY --from=build /out/prototype /usr/local/bin/prototype
COPY --from=web-build /web/dist /app/web
USER app
EXPOSE 8080 8081 8082
ENTRYPOINT ["/usr/local/bin/prototype"]

