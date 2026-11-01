#!/usr/bin/env python3
"""Verify a build candidate, then promote its immutable digest to a shared tag."""

import importlib.util
from datetime import datetime
import logging
import os
from pathlib import Path
import re
import subprocess
import time

spec = importlib.util.spec_from_file_location(
    "discovery", Path(__file__).with_name("discover-nextcloud.py")
)
discovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(discovery)
LOG = logging.getLogger("nextcloud-publish")
VERSION_KEY = "org.opencontainers.image.version"
CREATED_KEY = "org.opencontainers.image.created"


def require_current_revision():
    branch = os.environ["DEFAULT_BRANCH"]
    if os.environ["GITHUB_REF"] != f"refs/heads/{branch}":
        raise RuntimeError("Only the repository default branch may publish shared tags")
    for attempt in range(discovery.ATTEMPTS):
        try:
            result = subprocess.run(
                ["git", "ls-remote", "--exit-code", "origin", f"refs/heads/{branch}"],
                capture_output=True, text=True, timeout=60, check=True,
            )
            break
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            if attempt == discovery.ATTEMPTS - 1:
                raise RuntimeError("Cannot verify the current publishing branch revision")
            LOG.warning("Publishing branch lookup failed (attempt %s)", attempt + 1)
            discovery.retry_delay(attempt)
    if result.stdout.split()[0] != os.environ["GITHUB_SHA"]:
        raise RuntimeError("This run is stale: the default branch has advanced; shared tags were not changed")


def require_fresh_candidate(image, target, version, base_digest, created_at):
    """Saved matrices and timestamps cannot establish upstream freshness."""
    upstream = discovery.inspect(f"docker.io/library/nextcloud:{version}-apache", raw=False)
    if upstream.get("digest") != base_digest or not discovery.has_platforms(upstream):
        raise RuntimeError(
            "Candidate upstream image has been superseded or is incomplete; "
            "run fresh version discovery instead of replaying this build"
        )
    if target == "latest":
        official_latest = discovery.inspect("docker.io/library/nextcloud:apache", raw=False)
        if official_latest.get("digest") != base_digest or not discovery.has_platforms(official_latest):
            raise RuntimeError("Official Nextcloud latest changed; run fresh discovery")
    current = discovery.inspect(f"{image}:{target}", missing_ok=True)
    annotations = (current or {}).get("annotations", {})
    current_version = annotations.get(VERSION_KEY)
    if current_version and discovery.version_key(current_version) > discovery.version_key(version):
        raise RuntimeError(f"Refusing to downgrade {target} from {current_version} to {version}")
    current_created = annotations.get(CREATED_KEY)
    if (current_version == version and annotations.get(discovery.BASE_KEY) == base_digest
            and current_created and datetime.strptime(current_created, "%Y-%m-%dT%H:%M:%SZ") > created_at):
        raise RuntimeError(f"Refusing to replace {target} with an older build of the same version")


def promote(image, candidate, tag, version, base_digest, recipe):
    # Resolve the candidate once, then verify and promote that immutable index.
    descriptor = discovery.inspect(f"{image}:{candidate}", raw=False)
    digest = descriptor["digest"]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("Invalid candidate image digest")
    manifest = discovery.inspect(f"{image}@{digest}")
    if discovery.needs_build(manifest, base_digest, recipe):
        raise RuntimeError("Candidate does not match the expected base, recipe, or architectures")
    if manifest.get("annotations", {}).get(VERSION_KEY) != version:
        raise RuntimeError("Candidate version does not match the newest selected release")
    created = manifest.get("annotations", {}).get(CREATED_KEY)
    if not created:
        raise RuntimeError("Candidate is missing its build timestamp")
    created_at = datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ")
    if tag not in ("latest", version):
        raise ValueError("Shared tag must be latest or the candidate's exact version")

    # Reject a latest downgrade before touching either alias. Each actual write
    # also rechecks the upstream digest and destination, including on retries.
    targets = [version, "latest"] if tag == "latest" else [version]
    if tag == "latest":
        require_fresh_candidate(image, "latest", version, base_digest, created_at)
    for target in targets:
        for attempt in range(3):
            # Recheck after potentially slow lookups and before every write.
            require_current_revision()
            require_fresh_candidate(image, target, version, base_digest, created_at)
            LOG.info("Promoting %s@%s to %s (attempt %s/3)", image, digest, target, attempt + 1)
            try:
                result = subprocess.run([
                    "docker", "buildx", "imagetools", "create", "--tag", f"{image}:{target}",
                    "--annotation", f"index:{VERSION_KEY}={version}",
                    "--annotation", f"index:{CREATED_KEY}={created}",
                    "--annotation", f"index:{discovery.BASE_KEY}={base_digest}",
                    "--annotation", f"index:{discovery.RECIPE_KEY}={recipe}",
                    f"{image}@{digest}",
                ], timeout=120)
                if result.returncode == 0:
                    break
            except subprocess.TimeoutExpired:
                LOG.warning("Promotion request timed out after 120 seconds")
            if attempt < 2:
                LOG.warning("Promotion failed; retrying in %s seconds", (attempt + 1) * 15)
                time.sleep((attempt + 1) * 15)
        else:
            raise RuntimeError(f"Failed to publish {image}:{target} after 3 attempts")
    return digest


def main():
    digest = promote(*(os.environ[name] for name in (
        "IMAGE", "CANDIDATE", "PUBLISH_TAG", "VERSION", "BASE_DIGEST", "RECIPE"
    )))
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
        summary.write(f"Published {os.environ['IMAGE']}:{os.environ['PUBLISH_TAG']} "
                      f"from verified Nextcloud {os.environ['VERSION']} ({digest}).\n")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        main()
    except Exception:
        LOG.exception("Image promotion failed; no unverified candidate will be promoted")
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
            summary.write(f"Failed to promote {os.environ.get('PUBLISH_TAG', 'image')}; see logs.\n")
        raise SystemExit(1)
