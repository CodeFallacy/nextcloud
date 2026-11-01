ARG NEXTCLOUD_VERSION=34.0.4
ARG NEXTCLOUD_BASE=nextcloud:${NEXTCLOUD_VERSION}-apache
FROM ${NEXTCLOUD_BASE}
ARG TARGETARCH

# Debian resolves native amd64/arm64 packages for the selected base image.
# Do not download LibreSign's Java runtime at container startup: persisted app
# data can retain a runtime built for a different CPU after a host migration.
RUN set -eux; \
    test "$(dpkg --print-architecture)" = "$TARGETARCH"; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        default-jre-headless \
        ffmpeg \
        nano \
        nodejs \
        npm \
        pdftk-java \
        poppler-utils; \
    rm -rf /var/lib/apt/lists/*; \
    php --version; \
    java -version; \
    ffmpeg -version; \
    pdftk --version; \
    pdfinfo -v; \
    pdfsig -v
