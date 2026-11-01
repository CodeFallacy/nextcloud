#!/usr/bin/env python3
"""Keep twenty newest image snapshots and their multiarch dependencies."""

import logging
import os
import re

import registry_utils as registry

LOG = logging.getLogger("image-retention")


def tags(version):
    return version["metadata"]["container"]["tags"]


def list_versions(url, token):
    versions, page = [], 1
    while True:
        batch = registry.request_json(f"{url}/versions?per_page=100&page={page}", token)
        if not isinstance(batch, list):
            raise RuntimeError("Unexpected GitHub package versions response; refusing cleanup")
        versions.extend(batch)
        if len(batch) < 100:
            return versions
        page += 1


def children(manifest):
    result = {item["digest"] for item in manifest.get("manifests", [])}
    if manifest.get("subject"):
        result.add(manifest["subject"]["digest"])
    return result


def cleanup_plan(versions, manifests, keep=registry.RETAIN_IMAGES):
    if keep < 1:
        raise ValueError("Retention must keep at least one release")
    by_digest = {version["name"]: version for version in versions}
    if len(by_digest) != len(versions):
        raise RuntimeError("Duplicate package versions; refusing cleanup")
    latest = [v for v in versions if "latest" in tags(v)]
    if len(latest) != 1:
        raise RuntimeError("Expected exactly one latest image; refusing cleanup")
    latest = latest[0]
    referenced = {child for manifest in manifests.values() for child in children(manifest)}
    roots = [v for v in versions if v["name"] not in referenced and
             ("manifests" in manifests[v["name"]] or tags(v))]
    if latest["name"] not in {v["name"] for v in roots}:
        raise RuntimeError("Latest is not a top-level image; refusing cleanup")
    # Count complete image snapshots, not their architecture/attestation parts.
    # Version/build/digest tags pointing to one index count as one image.
    retained = {latest["name"]}
    for version in sorted(roots, key=lambda v: (v["created_at"], v["id"]), reverse=True):
        if len(retained) >= keep:
            break
        retained.add(version["name"])
    protected = set()

    def protect(digest):
        if digest in protected:
            return
        protected.add(digest)
        if digest not in manifests:
            raise RuntimeError(f"Missing retained manifest {digest}; refusing cleanup")
        for child in children(manifests[digest]):
            protect(child)

    for digest in retained:
        protect(digest)
    # Roots first, followed by unreferenced architecture/attestation manifests.
    delete = [v for v in versions if v["name"] not in protected]
    delete.sort(key=lambda v: bool(children(manifests[v["name"]])), reverse=True)
    return retained, protected, delete


def cleanup(image, package, token):
    registry.require_current_revision()
    url = registry.package_url(package)
    # Called only after a successful publication. A missing/inaccessible package
    # is an error, not permission to silently skip the retention requirement.
    registry.request_json(url, token)
    versions = list_versions(url, token)
    if not versions:
        raise RuntimeError("Published package has no visible versions; refusing cleanup")
    # Take a complete manifest snapshot before planning any deletion. Failure to
    # read even one manifest stops cleanup rather than guessing dependencies.
    manifests = {}
    for version in versions:
        digest = version["name"]
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValueError(f"Invalid package digest {digest}")
        manifest = registry.inspect(f"{image}@{digest}")
        if manifest.get("schemaVersion") != 2:
            raise ValueError(f"Unsupported manifest {digest}; refusing cleanup")
        manifests[digest] = manifest
    # The Packages API can omit platform manifests that are nevertheless
    # referenced by an index. Resolve those references from the registry too.
    pending = {child for manifest in manifests.values() for child in children(manifest)} - manifests.keys()
    while pending:
        digest = pending.pop()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValueError("Invalid dependent manifest digest; refusing cleanup")
        manifest = registry.inspect(f"{image}@{digest}")
        if manifest.get("schemaVersion") != 2:
            raise ValueError("Unsupported dependent manifest; refusing cleanup")
        manifests[digest] = manifest
        pending.update(children(manifest) - manifests.keys())
    retained, protected, delete = cleanup_plan(versions, manifests)
    latest_digest = next(v["name"] for v in versions if "latest" in tags(v))
    if registry.inspect(f"{image}:latest", raw=False)["digest"] != latest_digest:
        raise RuntimeError("Latest changed during cleanup planning; refusing deletion")
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
        summary.write(f"## {package} retention\n\nKeeping {len(retained)} image snapshots "
                      f"and their {len(protected) - len(retained)} dependent manifests; "
                      f"removing {len(delete)} obsolete package versions.\n")
    for version in delete:
        registry.require_current_revision()
        if registry.inspect(f"{image}:latest", raw=False)["digest"] != latest_digest:
            raise RuntimeError("Latest changed during cleanup; stopping deletion")
        current = registry.request_json(f"{url}/versions/{version['id']}", token, missing_ok=True)
        if current is None:
            continue
        if current["name"] != version["name"] or set(tags(current)) != set(tags(version)):
            raise RuntimeError("Package tags changed during cleanup; stopping deletion")
        LOG.info("Deleting obsolete %s digest=%s id=%s tags=%s", package, version["name"], version["id"], tags(version))
        registry.request_json(f"{url}/versions/{version['id']}", token, method="DELETE", missing_ok=True)
    return len(delete)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        package = os.environ["GHCR_PACKAGE"]
        if package not in ("nextcloud", "collabora"):
            raise ValueError("Cleanup is restricted to this repository's Nextcloud and Collabora packages")
        cleanup("ghcr.io/" + os.environ["GITHUB_REPOSITORY_OWNER"].lower() + "/" + package,
                package, os.environ["GH_TOKEN"])
    except Exception:
        LOG.exception("Image retention failed; see logs for any completed deletions")
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
            summary.write("\nImage retention failed; see logs for its plan and any completed deletions.\n")
        raise SystemExit(1)
