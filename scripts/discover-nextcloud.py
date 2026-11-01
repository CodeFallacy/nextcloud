#!/usr/bin/env python3
"""Discover stable Apache images and compare their build inputs with GHCR."""

import hashlib
from http.client import IncompleteRead
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.error import HTTPError
from urllib.request import urlopen

BASE_KEY = "io.codefallacy.nextcloud.base-digest"
RECIPE_KEY = "io.codefallacy.nextcloud.recipe"
PLATFORMS = {("linux", "amd64"), ("linux", "arm64")}
ATTEMPTS = 4
LOG = logging.getLogger("nextcloud-discovery")


class RegistryAccessError(RuntimeError):
    """Authentication failures invalidate the complete discovery run."""


def retry_delay(attempt, retry_after=None):
    delay = min(30, 2 ** (attempt + 1))
    if retry_after and retry_after.isdigit():
        delay = max(delay, min(60, int(retry_after)))
    LOG.warning("Retrying in %s seconds (next attempt %s/%s)", delay, attempt + 2, ATTEMPTS)
    time.sleep(delay)


def fetch_page(url):
    for attempt in range(ATTEMPTS):
        try:
            LOG.info("Docker Hub request, attempt %s/%s: %s", attempt + 1, ATTEMPTS, url)
            with urlopen(url, timeout=30) as response:
                return json.load(response)
        except (OSError, IncompleteRead, json.JSONDecodeError, UnicodeDecodeError) as error:
            retry_after = None
            if isinstance(error, HTTPError):
                if error.code not in (408, 429) and error.code < 500:
                    raise RuntimeError(f"Docker Hub rejected the request: HTTP {error.code}") from error
                retry_after = error.headers.get("Retry-After") if error.headers else None
            LOG.warning("Docker Hub request failed: %s", error)
            if attempt == ATTEMPTS - 1:
                raise RuntimeError(f"Cannot reach Docker Hub after {ATTEMPTS} attempts: {error}") from error
            retry_delay(attempt, retry_after)


def version_key(version):
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError(f"Invalid stable version: {version}")
    return tuple(map(int, version.split(".")))


def published_versions():
    """Resolve only the release currently behind official Apache latest."""
    latest = inspect("docker.io/library/nextcloud:apache", raw=False)
    digest = latest.get("digest", "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest) or not has_platforms(latest):
        raise RuntimeError("Official Nextcloud Apache latest is incomplete; retry next run")
    url = "https://hub.docker.com/v2/repositories/library/nextcloud/tags?page_size=100&name=apache"
    matches = set()
    while url:
        if not url.startswith("https://hub.docker.com/v2/repositories/library/nextcloud/tags"):
            raise ValueError("Unexpected Docker Hub pagination URL")
        page = fetch_page(url)
        for tag in page["results"]:
            match = re.fullmatch(r"(\d+\.\d+\.\d+)-apache", tag["name"])
            if match and tag.get("digest") == digest:
                matches.add(match[1])
        url = page["next"]
    if not matches:
        raise RuntimeError("No stable version tag matches official Nextcloud Apache latest")
    return [max(matches, key=version_key)]


def inspect(reference, missing_ok=False, raw=True):
    command = ["docker", "buildx", "imagetools", "inspect", reference]
    command += ["--raw"] if raw else ["--format", "{{json .Manifest}}"]
    for attempt in range(ATTEMPTS):
        LOG.info("Inspecting %s (attempt %s/%s)", reference, attempt + 1, ATTEMPTS)
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=90)
        except subprocess.TimeoutExpired:
            LOG.warning("Registry lookup timed out after 90 seconds: %s", reference)
            if attempt == ATTEMPTS - 1:
                raise RuntimeError(f"Registry lookup timed out after {ATTEMPTS} attempts: {reference}")
            retry_delay(attempt)
            continue
        if result.returncode == 0:
            return json.loads(result.stdout)
        error = result.stderr.lower()
        # Never interpret auth, rate-limit, or network failures as missing images.
        auth_error = "unauthorized" in error or "forbidden" in error or bool(
            re.search(r"\b(?:401|403)\b", error)
        )
        missing = not auth_error and not re.search(r"\b429\b", error) and (
            "manifest unknown" in error or "name unknown" in error
            or bool(re.search(re.escape(reference.lower()) + r": not found\s*$", error))
        )
        if missing_ok and missing:
            return None
        LOG.warning("Registry lookup failed for %s: %s", reference, result.stderr.strip()[:1000])
        if auth_error:
            raise RegistryAccessError(f"Registry denied access to {reference}: {result.stderr.strip()}")
        if attempt < ATTEMPTS - 1:
            retry_delay(attempt)
    raise RuntimeError(f"Cannot inspect {reference}: {result.stderr.strip()}")


def has_platforms(manifest):
    platforms = {
        (item.get("platform", {}).get("os"), item.get("platform", {}).get("architecture"))
        for item in manifest.get("manifests", [])
    }
    return PLATFORMS <= platforms


def needs_build(existing, digest, recipe, force=False):
    annotations = (existing or {}).get("annotations", {})
    return force or existing is None or not has_platforms(existing) or (
        annotations.get(BASE_KEY) != digest or annotations.get(RECIPE_KEY) != recipe
    )


def recipe_hash():
    digest = hashlib.sha256()
    for name in ("Dockerfile", "scripts/discover-nextcloud.py", "scripts/publish-nextcloud.py",
                 ".github/workflows/build-nextcloud.yml"):
        digest.update(name.encode() + b"\0" + Path(name).read_bytes() + b"\0")
    return digest.hexdigest()


def discover(versions, image, recipe, force=False, limit=24):
    pending, rows = [], []
    latest, latest_digest = "", ""
    for version in versions:
        LOG.info("Checking Nextcloud %s", version)
        reference = f"docker.io/library/nextcloud:{version}-apache"
        try:
            descriptor = inspect(reference, missing_ok=True, raw=False)
            if descriptor is None:
                rows.append((version, "Upstream tag not yet available; retry next run"))
                continue
            digest = descriptor["digest"]
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                raise ValueError(f"Invalid upstream digest for {version}")
            if not has_platforms(descriptor):
                rows.append((version, "Waiting for AMD64 and ARM64; retry next run"))
                continue
            existing = None if force else inspect(f"{image}:{version}", missing_ok=True)
        except RegistryAccessError:
            raise
        except (RuntimeError, ValueError, OSError) as error:
            LOG.error("Nextcloud %s lookup failed; continuing other versions: %s", version, error)
            rows.append((version, "ERROR: registry lookup failed; see logs; retry next run"))
            continue
        if version == versions[0]:
            latest, latest_digest = version, digest
        if needs_build(existing, digest, recipe, force):
            if len(pending) < limit:
                pending.append({"version": version, "digest": digest})
                rows.append((version, "Build queued"))
            else:
                rows.append((version, "Deferred to next run (batch limit)"))
        else:
            rows.append((version, "Unchanged"))
    return pending, rows, latest, latest_digest


def main():
    image = "ghcr.io/" + os.environ["GITHUB_REPOSITORY"].lower()
    recipe = recipe_hash()
    # Follow only current latest; never backfill deleted historical releases.
    versions = published_versions()
    LOG.info("Discovered %s stable Apache versions; destination: %s", len(versions), image)
    pending, rows, latest, latest_digest = discover(
        versions, image, recipe, os.environ.get("FORCE_REBUILD", "false") == "true"
    )
    for version, status in rows:
        LOG.info("Nextcloud %s: %s", version, status)
    outputs = {
        "image": image,
        "recipe": recipe,
        "matrix": json.dumps({"include": pending}, separators=(",", ":")),
        "has_changes": str(bool(pending)).lower(),
        "latest": latest,
        "latest_digest": latest_digest,
        "lookup_failures": str(sum(status.startswith("ERROR:") for _, status in rows)),
    }
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        for key, value in outputs.items():
            output.write(f"{key}={value}\n")
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
        summary.write("## Nextcloud discovery\n\n| Version | Result |\n| --- | --- |\n")
        for version, status in rows:
            summary.write(f"| {version} | {status} |\n")
    print(f"Discovered {len(versions)} versions; queued {len(pending)} builds for {image}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        main()
    except Exception:
        LOG.exception("Nextcloud discovery failed")
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            with open(summary_path, "a") as summary:
                summary.write("## Nextcloud discovery failed\n\n"
                              "No build matrix was published. See the discovery step logs "
                              "for the error and retry history.\n")
        raise SystemExit(1)
