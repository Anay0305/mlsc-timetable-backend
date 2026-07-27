#!/usr/bin/env sh
set -eu

# Run this from the backend repository root. Certbot preserves the webroot
# used during initial issuance, so no domain or email is repeated here.
docker compose run --rm certbot renew
docker compose exec -T nginx nginx -s reload
