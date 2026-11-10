# Module cred-gw, devcontainer side. Stacked on the previous image of the module
# chain (BASE_IMAGE is passed by initialize.sh). The gateway itself is the
# 'cred-gw' service (compose.yml, image built from gateway/).
#
# Here: trust the sandbox CA, so TLS to the gateway verifies. The gateway answers
# for the upstream host names with a certificate from that CA (make-certs.sh).
# 'sandbox-ca' is a named build context (build-contexts): only the public CA
# certificate, never the key.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}

USER root
COPY --from=sandbox-ca ca.crt /usr/local/share/ca-certificates/ai-envelope-sandbox-ca.crt
RUN update-ca-certificates
# Clients that bring their own CA bundle: point them at the system bundle
# (all public CAs + the sandbox CA)
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    NODE_EXTRA_CA_CERTS=/usr/local/share/ca-certificates/ai-envelope-sandbox-ca.crt
USER dev
