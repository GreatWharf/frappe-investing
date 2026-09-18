# syntax=docker/dockerfile:1.7
#
# =============================================================================
# Frappe Investing -- application image
# =============================================================================
#
# Layers the frappe_investing app, installed EDITABLE, onto the official
# ERPNext image published by the Frappe team on Docker Hub (docker.io). The
# project is CI-tested against Frappe/ERPNext v15 and v16; the image base
# tracks the v16 line. Never point ERPNEXT_VERSION at a quay.io (or any
# other) mirror.
#
# Build context: this repository's own git checkout. There is no
# `bench get-app <git-url>` network fetch during the build -- the code that
# lands in apps/frappe_investing is exactly what was handed to the build as
# its context.
#
# Runtime site install/migrate/asset-repair is done separately, once per
# environment, by docker/site-init.sh. This image never creates, drops or
# migrates any site by itself, and never touches site credentials.
# =============================================================================

ARG ERPNEXT_VERSION=version-16
FROM docker.io/frappe/erpnext:${ERPNEXT_VERSION}

ARG ERPNEXT_VERSION
LABEL org.opencontainers.image.title="frappe-investing" \
      org.opencontainers.image.description="Frappe Investing app layered on the official ERPNext image" \
      org.opencontainers.image.base.name="docker.io/frappe/erpnext:${ERPNEXT_VERSION}" \
      org.frappe-investing.site-init="docker/site-init.sh"

# The base image already provides Python, Node/Yarn and pre-built
# frappe/erpnext assets, running as a non-root `frappe` user with an
# initialized bench at /home/frappe/frappe-bench. This build only adds one
# app on top.
USER frappe
WORKDIR /home/frappe/frappe-bench

# -----------------------------------------------------------------------------
# 1. Bring in the app source -- runtime files only. No tests/, docs/,
#    .github/, node_modules/ or VCS metadata are copied here.
# -----------------------------------------------------------------------------
COPY --chown=frappe:frappe pyproject.toml README.md LICENSE /opt/frappe_investing_src/
COPY --chown=frappe:frappe frappe_investing/ /opt/frappe_investing_src/frappe_investing/

# -----------------------------------------------------------------------------
# 2. Register the app with this bench as an EDITABLE install rooted at
#    apps/frappe_investing. `bench get-app --soft-link` assumes a git
#    checkout and fails against a plain copied tree, so registration is
#    direct: link the source into apps/, editable-install into the bench
#    virtualenv, record in sites/apps.txt.
# -----------------------------------------------------------------------------
RUN ln -s /opt/frappe_investing_src apps/frappe_investing \
    && ./env/bin/pip install --quiet --editable apps/frappe_investing \
    && printf 'frappe\nerpnext\nfrappe_investing\n' > sites/apps.txt

# -----------------------------------------------------------------------------
# 3. Verify the runtime dependencies declared in pyproject.toml (requests,
#    cryptography) landed in the bench virtualenv and import cleanly with no
#    site context, so a broken dependency fails the image build instead of
#    the first deployment.
# -----------------------------------------------------------------------------
RUN ./env/bin/python -c "\
import frappe_investing, requests, cryptography; \
assert frappe_investing.__version__; \
print('frappe_investing', frappe_investing.__version__, '/ requests', requests.__version__)"

# -----------------------------------------------------------------------------
# 4. Asset build. `bench build` needs a target app but no site. The sites
#    volume mounted in real deployments hides sites/assets at runtime;
#    docker/site-init.sh re-creates the link on start.
# -----------------------------------------------------------------------------
RUN bench build --app frappe_investing \
    && test -r apps/frappe_investing/frappe_investing/public/js/investing.js

# Site creation, `install-app` and `migrate` are intentionally NOT done here.
# Run docker/site-init.sh once per environment, with an explicit SITE_NAME,
# after the sites volume and the database/Redis services are ready.
COPY --chown=frappe:frappe docker/site-init.sh /home/frappe/frappe-bench/docker/site-init.sh
RUN chmod 0755 /home/frappe/frappe-bench/docker/site-init.sh
