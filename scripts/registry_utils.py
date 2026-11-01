"""Shared registry transport for mirroring and retention (Python standard library)."""

from http.client import IncompleteRead
import json
import logging
import os
import re
import subprocess
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

LOG = logging.getLogger("registry")
RETAIN_IMAGES = 20
ATTEMPTS = 4


class AccessError(RuntimeError):
    pass


def version_key(value):
    if not re.fullmatch(r"\d+(?:\.\d+){2,4}", value):
        raise ValueError(f"Not a stable release: {value}")
    return tuple(map(int, value.split(".")))


def pause(attempt, retry_after=None):
    delay = 2 ** (attempt + 1)
    if retry_after and retry_after.isdigit():
        delay = max(delay, min(60, int(retry_after)))
    LOG.warning("Retrying in %ss (next attempt %s/%s)", delay, attempt + 2, ATTEMPTS)
    time.sleep(delay)


def request_json(url, token=None, method="GET", missing_ok=False):
    headers = {"Accept": "application/vnd.github+json"} if token else {}
    if token:
        headers.update(Authorization=f"Bearer {token}", **{"X-GitHub-Api-Version": "2022-11-28"})
    for attempt in range(ATTEMPTS):
        try:
            LOG.info("%s %s (attempt %s/%s)", method, url, attempt + 1, ATTEMPTS)
            with urlopen(Request(url, headers=headers, method=method), timeout=30) as response:
                return None if response.status == 204 else json.load(response)
        except (OSError, IncompleteRead, json.JSONDecodeError, UnicodeDecodeError) as error:
            retry_after = None
            if isinstance(error, HTTPError):
                if error.code == 404 and missing_ok:
                    return None
                if error.code in (401, 403):
                    raise AccessError(f"Access denied: {method} {url} (HTTP {error.code})") from error
                if error.code not in (408, 429) and error.code < 500:
                    raise RuntimeError(f"{method} {url}: HTTP {error.code}") from error
                retry_after = error.headers.get("Retry-After") if error.headers else None
            LOG.warning("Request failed: %s", error)
            if attempt == ATTEMPTS - 1:
                raise RuntimeError(f"Request failed after {ATTEMPTS} attempts: {method} {url}") from error
            pause(attempt, retry_after)


def inspect(reference, missing_ok=False, raw=True):
    command = ["docker", "buildx", "imagetools", "inspect", reference]
    command += ["--raw"] if raw else ["--format", "{{json .Manifest}}"]
    for attempt in range(ATTEMPTS):
        try:
            LOG.info("Inspect %s (attempt %s/%s)", reference, attempt + 1, ATTEMPTS)
            result = subprocess.run(command, capture_output=True, text=True, timeout=90)
            if result.returncode == 0:
                return json.loads(result.stdout)
            error = result.stderr.lower()
            if re.search(r"\b(?:401|403)\b|unauthorized|forbidden", error):
                raise AccessError(f"Registry access denied: {reference}: {result.stderr.strip()}")
            if missing_ok and not re.search(r"\b429\b", error) and (
                "manifest unknown" in error or "name unknown" in error
                or re.search(re.escape(reference.lower()) + r": not found\s*$", error)
            ):
                return None
            LOG.warning("Registry lookup failed: %s", result.stderr.strip())
        except (subprocess.TimeoutExpired, json.JSONDecodeError) as error:
            LOG.warning("Registry lookup failed: %s", error)
        if attempt < ATTEMPTS - 1:
            pause(attempt)
    raise RuntimeError(f"Registry lookup failed after {ATTEMPTS} attempts: {reference}")


def has_platforms(manifest):
    found = {(m.get("platform", {}).get("os"), m.get("platform", {}).get("architecture"))
             for m in manifest.get("manifests", [])}
    return {("linux", "amd64"), ("linux", "arm64")} <= found


def require_current_revision():
    branch = os.environ["DEFAULT_BRANCH"]
    if os.environ["GITHUB_REF"] != f"refs/heads/{branch}":
        raise RuntimeError("Only the default branch may publish or delete images")
    for attempt in range(ATTEMPTS):
        try:
            result = subprocess.run(
                ["git", "ls-remote", "--exit-code", "origin", f"refs/heads/{branch}"],
                check=True, capture_output=True, text=True, timeout=60,
            )
            break
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            if attempt == ATTEMPTS - 1:
                raise RuntimeError("Could not verify publishing revision")
            pause(attempt)
    if result.stdout.split()[0] != os.environ["GITHUB_SHA"]:
        raise RuntimeError("Stale run: default branch has advanced; no shared tags will be changed")


def package_url(package):
    from urllib.parse import quote
    owner = os.environ["GITHUB_REPOSITORY_OWNER"]
    owner_type = os.environ["GITHUB_REPOSITORY_OWNER_TYPE"]
    if owner_type not in ("User", "Organization"):
        raise ValueError("Unknown GitHub package owner type")
    scope = "orgs" if owner_type == "Organization" else "users"
    return (f"{os.environ.get('GITHUB_API_URL', 'https://api.github.com')}/{scope}/"
            f"{quote(owner, safe='')}/packages/container/{quote(package, safe='')}")
