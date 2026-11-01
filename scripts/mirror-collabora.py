#!/usr/bin/env python3
"""Mirror official Collabora releases unchanged, independently of Nextcloud."""

import logging
import os
from pathlib import Path
import re
import subprocess

import registry_utils as registry

LOG = logging.getLogger("collabora-mirror")
SOURCE = "docker.io/collabora/code"


def published_versions():
    url = "https://hub.docker.com/v2/repositories/collabora/code/tags?page_size=100"
    latest_digest = validate(registry.inspect(f"{SOURCE}:latest", raw=False))
    versions = set()
    while url:
        if not url.startswith("https://hub.docker.com/v2/repositories/collabora/code/tags"):
            raise ValueError("Unexpected Collabora pagination URL")
        page = registry.request_json(url)
        for tag in page["results"]:
            if re.fullmatch(r"\d+(?:\.\d+){2,4}", tag["name"]) and tag.get("digest") == latest_digest:
                versions.add(tag["name"])
        url = page["next"]
    if not versions:
        raise RuntimeError("No stable Collabora version tag matches official latest")
    return [max(versions, key=registry.version_key)]


def validate(manifest):
    digest = manifest.get("digest", "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest) or not registry.has_platforms(manifest):
        raise RuntimeError("Collabora image is missing a valid digest or AMD64/ARM64")
    return digest


def copy_tag(image, version, digest, tag):
    authfile = str(Path(os.environ.get("DOCKER_CONFIG", str(Path.home() / ".docker"))) / "config.json")
    for attempt in range(registry.ATTEMPTS):
        registry.require_current_revision()
        if validate(registry.inspect(f"{SOURCE}:{version}", raw=False)) != digest:
            raise RuntimeError(f"Collabora {version} changed upstream; start a new discovery run")
        if tag == "latest" and validate(registry.inspect(f"{SOURCE}:latest", raw=False)) != digest:
            raise RuntimeError("Official Collabora latest changed; refusing stale promotion")
        LOG.info("Copy %s to %s:%s (attempt %s/%s)", digest, image, tag, attempt + 1, registry.ATTEMPTS)
        # --all copies every platform and --preserve-digests forbids silent conversion.
        origin = f"{SOURCE}@{digest}" if tag.startswith("sha256-") else f"{image}@{digest}"
        try:
            subprocess.run([
                "skopeo", "copy", "--all", "--preserve-digests",
                "--src-authfile", authfile, "--dest-authfile", authfile,
                f"docker://{origin}", f"docker://{image}:{tag}",
            ], timeout=900, check=True)
            if validate(registry.inspect(f"{image}:{tag}", raw=False)) != digest:
                raise RuntimeError(f"Copied {tag} does not match the official image digest")
            return
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            LOG.warning("Collabora copy failed: %s", error)
            if attempt == registry.ATTEMPTS - 1:
                raise RuntimeError(f"Copy of Collabora {version} failed after retries") from error
            registry.pause(attempt)


def mirror(image, versions, package_exists):
    latest_digest = validate(registry.inspect(f"{SOURCE}:latest", raw=False))
    rows, failures, latest_matched = [], 0, False
    for version in versions:
        try:
            upstream = registry.inspect(f"{SOURCE}:{version}", raw=False)
            if not registry.has_platforms(upstream):
                rows.append((version, "Waiting for AMD64 and ARM64; retry next run", ""))
                continue
            digest = validate(upstream)
            immutable_tag = "sha256-" + digest.split(":")[1]
            existing = registry.inspect(f"{image}:{immutable_tag}", missing_ok=True, raw=False) if package_exists else None
            if existing is None or validate(existing) != digest:
                copy_tag(image, version, digest, immutable_tag)
                package_exists = True
            current = registry.inspect(f"{image}:{version}", missing_ok=True, raw=False)
            if current is None or validate(current) != digest:
                copy_tag(image, version, digest, version)
            if digest == latest_digest:
                current_latest = registry.inspect(f"{image}:latest", missing_ok=True, raw=False)
                if current_latest is None or validate(current_latest) != digest:
                    copy_tag(image, version, digest, "latest")
                latest_matched = True
            rows.append((version, "Mirrored/verified", digest))
        except registry.AccessError:
            raise
        except (RuntimeError, ValueError) as error:
            LOG.exception("Collabora %s failed; continuing other releases", version)
            rows.append((version, f"FAILED: {error}", ""))
            failures += 1
    if not latest_matched:
        rows.append(("latest", "FAILED: no successfully mirrored release matched official latest", latest_digest))
        failures += 1
    return rows, failures


def main():
    image = "ghcr.io/" + os.environ["GITHUB_REPOSITORY_OWNER"].lower() + "/collabora"
    exists = registry.request_json(registry.package_url("collabora"), os.environ["GH_TOKEN"], missing_ok=True)
    rows, failures = mirror(image, published_versions(), exists is not None)
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
        summary.write("## Collabora mirror\n\n| Release | Result | Upstream digest |\n| --- | --- | --- |\n")
        for version, status, digest in rows:
            summary.write(f"| {version} | {status} | {digest} |\n")
    if failures:
        raise RuntimeError(f"{failures} Collabora release(s) failed; see logs and summary")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        main()
    except Exception:
        LOG.exception("Collabora mirroring failed")
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
            summary.write("\nCollabora mirror failed; see step logs for errors and retry history.\n")
        raise SystemExit(1)
