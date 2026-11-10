#!/usr/bin/env bash
# make-certs.sh — creates the sandbox CA and the cred-gw certificate on the host (once; existing files are kept).
#
#   bash .devcontainer/cred-gw/make-certs.sh <host> [host ...]     e.g. jira.example.com git.example.com
#
# The hosts are the upstream Jira/GitLab names the devcontainer uses; cred-gw answers for them.
#
# Layout under ~/.config/ai-envelope/sandbox-ca/ (outside the workspace; the devcontainer must not read the keys):
#   public/ca.crt          trusted by the devcontainer image (build context 'sandbox-ca')
#   private/ca.key         CA key, never mounted anywhere
#   cred-gw/tls.crt, .key  served by cred-gw, mounted only into cred-gw
set -euo pipefail

dir="${HOME}/.config/ai-envelope/sandbox-ca"
if [ $# -eq 0 ]; then echo "usage: $0 <host> [host ...]   (e.g. jira.example.com git.example.com)" >&2; exit 1; fi
hosts=("$@")
mkdir -p "$dir/public" "$dir/private" "$dir/cred-gw"
chmod 700 "$dir/private" "$dir/cred-gw"

if [ ! -f "$dir/private/ca.key" ]; then
  openssl req -x509 -newkey rsa:3072 -nodes -days 3650 -subj "/CN=ai-envelope sandbox CA" \
    -addext "basicConstraints=critical,CA:TRUE,pathlen:0" -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -keyout "$dir/private/ca.key" -out "$dir/public/ca.crt"
  chmod 600 "$dir/private/ca.key"
  echo "[certs] CA created: $dir/public/ca.crt"
fi

if [ ! -f "$dir/cred-gw/tls.key" ]; then
  san="$(printf 'DNS:%s,' "${hosts[@]}")"
  openssl req -newkey rsa:3072 -nodes -subj "/CN=${hosts[0]}" -keyout "$dir/cred-gw/tls.key" -out "$dir/cred-gw/tls.csr"
  openssl x509 -req -in "$dir/cred-gw/tls.csr" -CA "$dir/public/ca.crt" -CAkey "$dir/private/ca.key" \
    -CAcreateserial -days 825 -out "$dir/cred-gw/tls.crt" \
    -extfile <(printf 'subjectAltName=%s\nextendedKeyUsage=serverAuth\nbasicConstraints=CA:FALSE\n' "${san%,}")
  rm -f "$dir/cred-gw/tls.csr" "$dir/public/ca.srl"
  chmod 600 "$dir/cred-gw/tls.key"
  echo "[certs] cred-gw certificate created for: ${hosts[*]}"
fi
