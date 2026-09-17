# Nextcloud image and deployment notes

This repository builds an Apache-based Nextcloud **34.0.4** image for both
`linux/amd64` (x86-64) and `linux/arm64` (for example, Raspberry Pi 5). The
GitHub Actions publishes multi-platform images at
`ghcr.io/codefallacy/nextcloud:32.0.15`, `:33.0.9`, and `:34.0.4`, plus
`latest` and a build-number tag for 34.0.4. Docker selects the correct
architecture from each manifest. The workflow runs on pushes to `production`
or can be started manually. The old unversioned `arm64`/`aarch64` aliases are
not updated: pointing a live 32.x installation at 34.x would skip a required
major-version upgrade. Use versioned tags on both CPUs.

As of September 16, 2026, 34.0.4 is the newest stable Apache image published
on Docker Hub. Nextcloud 35 (Hub 26 Summer) was announced the same day, but
its stable Docker image is not yet published. Keep the app and cron containers
on the same version; never use `nextcloud:latest` for cron alongside a pinned
app, since it can advance independently to another major version.

## What is in the image

The Dockerfile installs Java (`default-jre-headless`), FFmpeg, Node.js/npm,
PDFtk (`pdftk-java`), and Poppler (`pdfinfo` and `pdfsig`) from the base
image's Debian repositories.
Those packages are built for the selected CPU architecture. The workflow builds
both architectures and executes the Dockerfile's tool checks on each one.

The old `init-libresign.sh` hook was removed. It ran
`occ libresign:install --java` after installation, which downloads a separate
Java runtime into persistent Nextcloud data. A runtime copied from an x86 host
can then fail on ARM even though the Docker image itself is ARM-native. On a
machine migrated between architectures, inspect LibreSign's configured Java,
PDFtk, and JSignPdf paths and run its own configuration check before trusting
the admin-page status:

```sh
docker compose exec -u www-data nextcloud-app php occ libresign:configure:check
docker compose exec -u www-data nextcloud-app php occ config:app:get libresign java_path
docker compose exec -u www-data nextcloud-app php occ config:app:get libresign pdftk_path
docker compose exec nextcloud-app java -version
docker compose exec nextcloud-app pdftk --version
```

If LibreSign points at copied, wrong-architecture Java or PDFtk binaries, set
their paths to the runtimes installed in this image:

```sh
docker compose exec -u www-data nextcloud-app php occ config:app:set libresign java_path --value=/usr/bin/java
docker compose exec -u www-data nextcloud-app php occ config:app:set libresign pdftk_path --value=/usr/bin/pdftk
```

LibreSign is a Nextcloud app, not bundled into this image. Its JSignPdf JAR is
Java bytecode; let LibreSign install/manage it separately after enabling the
app. Its own PDFtk binary/download and file-hash checks are separate from the
system packages in this image. Do not copy LibreSign's downloaded native
executables between x86 and ARM; reinstall them for the destination CPU and
run `libresign:configure:check`. Manually setting system binary paths may
still show LibreSign hash warnings even if a binary runs correctly.

On the Pi, LibreSign 14.1.0 cleared its JSignPdf and PDFtk hash warnings after
reinstalling those assets for ARM64:

```sh
docker compose exec -u www-data nextcloud-app php occ libresign:install --jsignpdf --pdftk --architecture=aarch64
docker compose exec -u www-data nextcloud-app php occ libresign:configure:check
```

The app and cron containers set `LANG` and `LC_ALL` to `C.UTF-8`; this also
cleared LibreSign's Java encoding warning. After upgrading Nextcloud, run
`occ maintenance:repair --include-expensive` if the admin overview reports
mimetype or duplicate-system-tag migrations.

## Deploy or upgrade from the existing Pi installation

1. Back up the Nextcloud data/config volume and MariaDB before an upgrade.
   Stop or quiesce writes while taking a consistent database backup. Do not
   copy a live MariaDB data directory as an upgrade/backup method.
2. For a new deployment, copy `.env.example` to `.env` and set absolute host
   paths and strong passwords. For the existing Pi, **keep its current `.env`**
   and compare/merge this Compose file with the live file and its
   `docker-compose.override.yml` before replacing anything. Preserve the
   Nextcloud and MariaDB volume paths, existing `npm-network`, Collabora
   service, and host ClamAV socket. This Compose file follows the Pi's tested
   `mariadb:11.5.2`; do not change the database version independently during
   the Nextcloud upgrade.
3. Wait for the `production` workflow to publish all three image tags. The Pi
   was last checked on Nextcloud 32.0.11; upgrade **32.0.11 → 32.0.15 →
   33.0.9 → 34.0.4**, verifying each step before the next. Nextcloud does not
   support skipping a major version. The included Compose overlays keep app
   and cron on matching versions during the two bridge stages.

   First, stop cron and move to the final 32.x patch:

   ```sh
   docker compose stop cron
   docker compose -f docker-compose.yml -f docker-compose.upgrade-32.yml pull nextcloud-app cron
   docker compose -f docker-compose.yml -f docker-compose.upgrade-32.yml up -d --no-deps --no-build nextcloud-app
   docker compose -f docker-compose.yml -f docker-compose.upgrade-32.yml exec -u www-data nextcloud-app php occ status
   ```

   Confirm version 32.0.15, `maintenance: false`, and
   `needsDbUpgrade: false`. Check `occ app:list` and update or resolve any
   incompatible third-party apps before continuing. Do not continue if
   startup or app-upgrade logs show errors. Then upgrade to 33.0.9:

   ```sh
   docker compose -f docker-compose.yml -f docker-compose.upgrade-33.yml pull nextcloud-app cron
   docker compose -f docker-compose.yml -f docker-compose.upgrade-33.yml up -d --no-deps --no-build nextcloud-app
   docker compose -f docker-compose.yml -f docker-compose.upgrade-33.yml exec -u www-data nextcloud-app php occ status
   ```

   Confirm the same healthy status and check apps again at 33.0.9. Finally
   move to 34.0.4 and restart cron on the matching image:

   ```sh
   docker compose pull nextcloud-app cron
   docker compose up -d --no-deps --no-build nextcloud-app
   docker compose exec -u www-data nextcloud-app php occ status
   docker compose up -d --no-deps --no-build cron
   docker compose exec -u www-data nextcloud-app php occ db:add-missing-indices
   ```

4. Check `occ status` for 34.0.4 and `needsDbUpgrade: false`, inspect app
   logs, and test a login, file upload, document edit, and scheduled
   background job. Check the compatibility of installed Nextcloud apps
   (especially LibreSign, Office, Memories, and Antivirus) before each major
   step. If a database or app migration fails, restore a consistent backup rather than
   trying to downgrade the data directory. These image tags are not available
   in GHCR until this repository's workflow publishes them.

### Raspberry Pi compatibility checks

The Pi was checked as `aarch64` (`arm64` in Docker) and running Nextcloud
32.0.11. The official Nextcloud 32.0.15, 33.0.9, and 34.0.4 Apache base
images all publish `linux/arm64` variants. The Dockerfile installs Debian's
native ARM64 Java, FFmpeg, Node/npm, and Poppler packages; PDFtk is a Java
package shared by both architectures. GitHub Actions builds each bridge and
the target image for both `linux/amd64` and `linux/arm64`. After the workflow
finishes, verify the GHCR tags include ARM64 before pulling on the Pi:

```sh
docker buildx imagetools inspect ghcr.io/codefallacy/nextcloud:32.0.15
docker buildx imagetools inspect ghcr.io/codefallacy/nextcloud:33.0.9
docker buildx imagetools inspect ghcr.io/codefallacy/nextcloud:34.0.4
```

Do not build these large images on the 4 GB Pi while it is serving users;
pull the ARM64 variants from GHCR after CI succeeds. LibreSign 12.4.8 on the
Pi is a Nextcloud 32 release; the app store lists newer releases for 33 and
34. Nextcloud Office and Memories likewise have newer 34-compatible releases.
Antivirus for Files 6.4.1 already lists 34 compatibility. Check all enabled
third-party apps at each stage and allow for app migrations/disabled apps;
image architecture alone does not guarantee app compatibility. The host
ClamAV socket and separate Collabora container remain in place; neither is
rebuilt by the Nextcloud image workflow.

## Redis and antivirus

The Compose file includes Redis. The Nextcloud app uses it via `REDIS_HOST`
and `REDIS_HOST_PASSWORD`; verify `memcache.locking` and
`memcache.distributed` are `\OC\Memcache\Redis` with `occ config:system:get`.
APCu remains suitable for `memcache.local`. The Redis volume is mounted at
the official image's `/data` directory; Redis is a cache/lock service, not a
substitute for the MariaDB backup.

The Pi uses the **host's** `clamav-daemon` and `clamav-freshclam`, not a second
ClamAV container. Both the app and cron containers mount `/run/clamav` as a
directory so a recreated `clamd.ctl` socket stays visible after daemon
restarts. The cron mount is essential: the Antivirus background scanner runs
there, and without it the log repeatedly reports that the socket does not
exist even while scans from the app container work. In Nextcloud, enable
`files_antivirus` and select **Daemon (Socket)** with
`/var/run/clamav/clamd.ctl` (`/var/run` links to `/run` in the container).
The Compose host must actually run ClamAV and expose that socket. Check:

```sh
systemctl is-active clamav-daemon clamav-freshclam
docker compose exec -u www-data nextcloud-app php occ config:app:get files_antivirus av_mode
docker compose exec -u www-data nextcloud-app php occ files_antivirus:status
docker compose exec cron test -S /var/run/clamav/clamd.ctl
docker compose exec -u www-data cron php occ files_antivirus:test
```

The Pi's cron-container test detected standard EICAR but not the modified
EICAR variant; treat that as a separate scanning-policy issue, not a socket
connectivity failure.

The Pi's host ClamAV currently limits `MaxFileSize` and `StreamMaxLength` to
100 MB, whereas Nextcloud permits larger uploads. Review those limits and
the Nextcloud antivirus policy before treating all uploads as fully scanned.
ClamAV consumes substantial RAM; on a 4 GB Pi with Collabora, avoid running a
second daemon. On an x86 host without host ClamAV, either install the host
service or use a separate ClamAV container with a persistent signature volume
and configure Nextcloud's daemon TCP mode; do not publish ClamAV's unauthenticated
TCP port to the internet. Official ClamAV Debian image tags include both
`amd64` and `arm64`; the generic Alpine `stable_base` tag is not ARM64.

## Collabora / Nextcloud Office

Collabora is **not** part of the Nextcloud image. The Compose file runs the
official `collabora/code:26.04.3.2.1` container separately on `npm-network`,
shared with the reverse proxy and Nextcloud. That tag supports `amd64` and
`arm64`; do not force
`platform: linux/arm64` in a Compose file intended for both architectures.
The Pi deployment uses `shm_size: 256m`, a reduced spare-process count, and
the settings below (replace the domains for another installation):

```yaml
collabora-nc:
  image: collabora/code:26.04.3.2.1
  restart: unless-stopped
  shm_size: 256m
  environment:
    aliasgroup1: https://nc.luisunlimited.com:443
    server_name: collabora123.luisunlimited.com
    extra_params: --o:ssl.enable=false --o:ssl.termination=true --o:num_prespawn_children=1
  expose:
    - "9980"
  networks:
    - npm-network
```

The Pi already has the external `npm-network`; a new host must create it and
attach Nginx Proxy Manager, or adapt the network name to its proxy. Proxy
`https://collabora123.luisunlimited.com` to `http://collabora-nc:9980` with
WebSocket support. Set Nextcloud Office's Collabora URL to that public HTTPS
name; both the browser and Nextcloud must reach it, and Collabora must reach
Nextcloud's public HTTPS name for WOPI. Forward only HTTPS/HTTP to the reverse
proxy—do not expose container port 9980 directly. Check
`https://collabora123.luisunlimited.com/hosting/discovery` for HTTP 200 and
then edit a document from an off-LAN client. Collabora is memory-intensive;
watch Pi RAM and swap under real editing load.
