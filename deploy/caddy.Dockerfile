# Caddy + DuckDNS DNS-01 plugin.
# Stock caddy:2-alpine doesn't include third-party plugins, so we build a
# custom binary using xcaddy and copy it into the slim runtime image.

FROM caddy:2-builder-alpine AS builder
RUN xcaddy build --with github.com/caddy-dns/duckdns

FROM caddy:2-alpine
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
