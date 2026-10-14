#!/bin/sh
# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Runs in the proxy before nginx starts (the image's /docker-entrypoint.d), as nginx's user. Writes what
# nginx.conf includes, under /tmp/kumo-service:
#   keys.map      the accepted X-API-Key values, from the api_key and previous_api_key secrets
#   real-ip.conf  the link proxies trusted for the client address, from KUMO_SERVICE_REAL_IP_FROM
# Exits non-zero, so nginx never starts, without a usable key. Never prints a key.
set -eu

dir=/tmp/kumo-service
umask 077
mkdir -p "$dir"

keys=$dir/keys.map
: >"$keys"
for name in api_key previous_api_key; do
  file=/run/secrets/$name
  [ -s "$file" ] || continue # Compose writes no file for an empty value
  key=$(tr -d '\r\n' <"$file")
  case $key in
    *[!A-Za-z0-9_-]*)
      echo "kumo-service: the $name secret may hold only A-Z, a-z, 0-9, _ and - (openssl rand -hex 32)" >&2
      exit 1
      ;;
  esac
  if [ "${#key}" -lt 32 ]; then
    echo "kumo-service: the $name secret is shorter than 32 characters (openssl rand -hex 32)" >&2
    exit 1
  fi
  printf '~^%s$ 1;\n' "$key" >>"$keys" # a regex: matched whole and case-sensitively
done
if [ ! -s "$keys" ]; then
  echo "kumo-service: no API key: set KUMO_SERVICE_API_KEY in .env (docs/kumo-service.md)" >&2
  exit 1
fi

real_ip=$dir/real-ip.conf
: >"$real_ip"
for address in $(printf '%s' "${KUMO_SERVICE_REAL_IP_FROM:-}" | tr ',' ' '); do
  case $address in
    *[!0-9A-Fa-f.:/]*)
      echo "kumo-service: KUMO_SERVICE_REAL_IP_FROM takes addresses and CIDR ranges, not $address" >&2
      exit 1
      ;;
  esac
  printf 'set_real_ip_from %s;\n' "$address" >>"$real_ip"
done
if [ -s "$real_ip" ]; then
  printf 'real_ip_header X-Forwarded-For;\nreal_ip_recursive on;\n' >>"$real_ip"
fi
