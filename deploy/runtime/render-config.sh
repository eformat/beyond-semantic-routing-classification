#!/bin/sh
# render-config.sh - materialise the Praxis config, substituting ${MAAS_API_KEY}.
#
# Praxis does NOT expand environment variables in its config file (verified in
# v0.7.5: the only env lookup in the core config is the OTLP endpoint
# fallback). So a credential cannot be templated by Praxis itself, and it must
# not be templated into the ConfigMap by us either - a ConfigMap is not a
# Secret.
#
# This script runs as an initContainer, reads the ConfigMap template,
# substitutes any literal ${MAAS_API_KEY} token from the Secret-provided
# environment variable, and writes the result to a memory-backed emptyDir
# shared with the proxy container.
#
# The shipped config (deploy/config/praxis-config.yaml) carries NO placeholder
# at all: the credential_injection filter reads MAAS_API_KEY straight from the
# environment and needs no templating. In that case this is a plain copy and
# still succeeds, which keeps the proven initContainer render pattern in place
# for any future templated variant.

set -eu

SRC="${CONFIG_TEMPLATE:-/config-template/praxis.yaml}"
DST="${CONFIG_OUT:-/etc/praxis/config.yaml}"
TOKEN='${MAAS_API_KEY}'

if [ ! -r "$SRC" ]; then
  echo "render-config: FATAL: template not readable: $SRC" >&2
  exit 1
fi

# -F (fixed string) matters: the bare name MAAS_API_KEY also appears as
# `env_var: MAAS_API_KEY` in the shipped config, which needs no substitution
# at all. Only the literal ${MAAS_API_KEY} token is a placeholder.
if ! grep -qF "$TOKEN" "$SRC"; then
  # No templating needed. Copy through untouched.
  cp "$SRC" "$DST"
  echo "render-config: no placeholder in template; copied verbatim to $DST"
  exit 0
fi

: "${MAAS_API_KEY:?render-config: FATAL: MAAS_API_KEY is unset but the template requires it}"

# Substitute with awk's ENVIRON so the credential never appears in argv (argv
# is world-readable via /proc inside the pod). Deliberately uses index()/
# substr() rather than gsub(): gsub's replacement string treats "&" and "\" as
# metacharacters, so a key containing either would be silently corrupted. This
# loop is literal and byte-exact for any key value.
awk -v tok="$TOKEN" '
  BEGIN { key = ENVIRON["MAAS_API_KEY"]; n = length(tok) }
  {
    line = $0
    out  = ""
    while ((p = index(line, tok)) > 0) {
      out  = out substr(line, 1, p - 1) key
      line = substr(line, p + n)
    }
    print out line
  }
' "$SRC" > "$DST"

# The rendered file now holds a credential: restrict it as far as the
# (arbitrary, OpenShift-assigned) UID allows.
chmod 0600 "$DST" 2>/dev/null || true

if grep -qF "$TOKEN" "$DST"; then
  echo "render-config: FATAL: placeholder survived substitution in $DST" >&2
  exit 1
fi

echo "render-config: rendered $SRC -> $DST ($(wc -l < "$DST") lines, credential substituted)"
