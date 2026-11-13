"""Audit explicitly reviewed Git sources without importing or building their code."""

import hashlib
import io
import json
import re
import tarfile
import tomllib
import urllib.request
from pathlib import PurePosixPath
from urllib.parse import quote

from packaging.requirements import Requirement


def fetch(url, payload=None):
    """Bounded public metadata requests; no application/GitHub/wallet credentials."""
    if not url.startswith(
        (
            "https://api.github.com/repos/",
            "https://codeload.github.com/",
            "https://api.osv.dev/v1/query",
        )
    ):
        raise ValueError("Unreviewed source-audit origin")
    request = urllib.request.Request(  # noqa: S310 - validated fixed HTTPS audit origins above.
        url,
        data=json.dumps(payload).encode() if payload else None,
        headers={"Content-Type": "application/json", "User-Agent": "polymarket-source-audit-v1"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - fixed HTTPS origins
        if response.geturl() != url:
            raise ValueError("Unexpected source-audit redirect")
        raw = response.read(8_000_001)
    if len(raw) > 8_000_000:
        raise ValueError("Source-audit response exceeded its bound")
    return raw


def source_entry(package, manifest):
    if set(manifest) != {"schema_version", "sources"} or manifest["schema_version"] != 1:
        raise ValueError("Invalid reviewed-source manifest")
    matches = [
        entry
        for entry in manifest["sources"]
        if entry["name"] == package["name"] and entry["version"] == package["version"]
    ]
    if len(matches) != 1:
        raise ValueError("Unreviewed or duplicate source dependency")
    entry = matches[0]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", entry["repository"]) or any(
        part in {".", ".."} for part in entry["repository"].split("/")
    ):
        raise ValueError("Invalid source repository")
    if (
        not re.fullmatch(r"[0-9a-f]{40}", entry["commit"])
        or not re.fullmatch(r"[0-9a-f]{40}", entry["tree"])
        or not re.fullmatch(r"[0-9a-f]{64}", entry["content_sha256"])
    ):
        raise ValueError("Immutable source/tree/content identity required")
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", entry["tag"]):
        raise ValueError("Reviewed release tag required")
    evidence = PurePosixPath(entry["review_evidence"])
    if (
        evidence.is_absolute()
        or ".." in evidence.parts
        or evidence.parts[:2] != ("security", "source-reviews")
    ):
        raise ValueError("Source review must be repository-owned security evidence")
    expected = f"https://github.com/{entry['repository']}.git?rev={entry['tag']}#{entry['commit']}"
    if package["source"] != {"git": expected}:
        raise ValueError("Source origin, tag or commit differs from reviewed identity")
    return entry


def _git_object(kind, body):
    return hashlib.sha1(  # noqa: S324 - Git protocol identity; independently bound to SHA-256 content.
        kind.encode() + b" " + str(len(body)).encode() + b"\0" + body
    ).hexdigest()


def tree_identity(files):
    """Recompute Git's tree identity independently from the downloaded public archive."""
    root = {}
    for path, (body, mode) in files.items():
        current = root
        parts = PurePosixPath(path).parts
        for part in parts[:-1]:
            current = current.setdefault(part, {})
        current[parts[-1]] = (body, mode)

    def digest(node):
        body = bytearray()
        for name, item in sorted(
            node.items(), key=lambda pair: pair[0] + ("/" if isinstance(pair[1], dict) else "")
        ):
            if isinstance(item, dict):
                mode, sha = "40000", digest(item)
            else:
                data, executable = item
                mode, sha = ("100755" if executable else "100644"), _git_object("blob", data)
            body.extend(mode.encode() + b" " + name.encode() + b"\0" + bytes.fromhex(sha))
        return _git_object("tree", bytes(body))

    return digest(root)


def verify_archive(raw, entry):
    files = {}
    total = 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        for index, member in enumerate(archive):
            if index >= 1000:
                raise ValueError("Source archive file count exceeded its bound")
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or member.issym() or member.islnk():
                raise ValueError("Unsafe source archive member")
            if member.isdir():
                continue
            if not member.isfile() or len(path.parts) < 2:
                raise ValueError("Unsupported source archive member")
            name = str(PurePosixPath(*path.parts[1:]))
            total += member.size
            if name in files or total > 8_000_000:
                raise ValueError("Duplicate or oversized source archive")
            files[name] = (archive.extractfile(member).read(), bool(member.mode & 0o111))
    records = [
        {"path": path, "sha256": hashlib.sha256(body).hexdigest()}
        for path, (body, _) in sorted(files.items())
    ]
    content = hashlib.sha256(
        json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if content != entry["content_sha256"] or tree_identity(files) != entry["tree"]:
        raise ValueError("Source bytes or Git tree differ from reviewed content")
    project = tomllib.loads(files["pyproject.toml"][0].decode())
    if (
        (project["project"]["name"], project["project"]["version"])
        != (entry["name"], entry["version"])
        or not files["LICENSE"][0].startswith(b"MIT License")
        or entry["license"] != "MIT"
    ):
        raise ValueError("Source package identity/license differs from reviewed release")
    return project, records


def verify_build(root, entry, project, config):
    path = PurePosixPath(entry["build_project"])
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Invalid source build inventory path")
    lock = tomllib.loads((root / path / "uv.lock").read_text())
    constraints = []
    versions = {}
    for package in lock["package"]:
        if "registry" not in package["source"]:
            continue
        versions[package["name"]] = package["version"]
        wheel = next(a for a in package["wheels"] if a["url"].endswith("py3-none-any.whl"))
        constraints.append(f"{package['name']} @ {wheel['url']}#{wheel['hash'].replace(':', '=')}")
    policy = config.get("tool", {}).get("uv", {})
    if sorted(constraints) != entry["build_constraints"] or (
        policy.get("build-constraint-dependencies") != entry["build_constraints"]
        or policy.get("require-build-hashes") is not True
    ):
        raise ValueError("Source build inputs must match the audited hashed build inventory")
    if project["build-system"]["build-backend"] != "hatchling.build":
        raise ValueError("Unreviewed source build backend")
    for value in project["build-system"]["requires"]:
        requirement = Requirement(value)
        if (
            requirement.url
            or requirement.name not in versions
            or not requirement.specifier.contains(versions[requirement.name])
        ):
            raise ValueError("Source build requirement absent from frozen audited build inventory")


def audit_source(package, manifest, root, config, cache):
    entry = source_entry(package, manifest)
    key = entry["repository"], entry["commit"]
    if key not in cache:
        base = f"https://api.github.com/repos/{entry['repository']}"
        tag = json.loads(fetch(f"{base}/git/ref/tags/{quote(entry['tag'], safe='')}"))
        commit = json.loads(fetch(f"{base}/git/commits/{entry['commit']}"))
        if tag.get("object") != {
            "type": "commit",
            "sha": entry["commit"],
            "url": f"{base}/git/commits/{entry['commit']}",
        } or (
            commit.get("sha") != entry["commit"]
            or commit.get("tree", {}).get("sha") != entry["tree"]
        ):
            raise ValueError("Reviewed source release tag/commit/tree changed")
        archive = fetch(
            f"https://codeload.github.com/{entry['repository']}/tar.gz/{entry['commit']}"
        )
        project, records = verify_archive(archive, entry)
        osv = json.loads(fetch("https://api.osv.dev/v1/query", {"commit": entry["commit"]}))
        if (
            not isinstance(osv, dict)
            or set(osv) - {"vulns"}
            or not isinstance(osv.get("vulns", []), list)
        ):
            raise ValueError("Invalid OSV commit scan")
        cache[key] = project, records, osv
    project, records, osv = cache[key]
    verify_build(root, entry, project, config)
    evidence = root / entry["review_evidence"]
    review = json.loads(evidence.read_text())
    if review.get("commit") != entry["commit"] or review.get("files") != records:
        raise ValueError("Final source review does not bind the actual source files")
    return {
        "package": entry["name"],
        "version": entry["version"],
        "source": package["source"],
        "tree": entry["tree"],
        "content_sha256": entry["content_sha256"],
        "license": entry["license"],
        "registry_advisory_scan": "not_a_registry_package",
        "commit_advisory_scan": osv,
        "source_review": entry["review_evidence"],
        "build_inventory": entry["build_project"],
    }
