from __future__ import annotations

import hashlib
import json
import re
import zipfile
from pathlib import Path
from typing import Any


DEFAULT_POLICY_CATALOG_PATH = (
    Path(__file__).with_name("policy") / "policy-sources-v1.json"
)


def load_policy_catalog(path: Path | str | None = None) -> dict[str, Any]:
    """Load the immutable policy-source catalog contract."""

    catalog_path = Path(path) if path is not None else DEFAULT_POLICY_CATALOG_PATH
    with catalog_path.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def source_ids_for_rule(
    catalog: dict[str, Any],
    rule_id: str,
) -> tuple[str, ...]:
    """Return the ordered policy sources declared for a guardrail rule."""

    rule_source_map = catalog.get("rule_source_map")
    if not isinstance(rule_source_map, dict) or rule_id not in rule_source_map:
        raise KeyError(rule_id)
    source_ids = rule_source_map[rule_id]
    if not isinstance(source_ids, list):
        raise ValueError(f"{rule_id}: source mapping must be an array")
    return tuple(str(source_id) for source_id in source_ids)


_REQUIRED_DOCUMENT_FIELDS = frozenset(
    {
        "source_id",
        "source_family_id",
        "title",
        "document_type",
        "usage_scope",
        "jurisdiction",
        "published_at",
        "snapshot_at",
        "source_path",
        "source_url",
        "document_hash",
        "basis_type",
        "rule_ids",
        "supersedes_source_id",
    }
)


def _document_bytes(document: dict[str, Any], package_root: Path) -> bytes:
    source_path = package_root / str(document["source_path"])
    archive_member = document.get("archive_member")
    if archive_member:
        with zipfile.ZipFile(source_path) as archive:
            return archive.read(str(archive_member))
    return source_path.read_bytes()


def validate_policy_catalog(
    catalog: dict[str, Any],
    *,
    package_root: Path | str | None = None,
) -> list[str]:
    """Return contract violations without mutating the catalog."""

    errors: list[str] = []
    if catalog.get("schema_version") != "eron-policy-source-catalog-v1":
        errors.append("schema_version must be eron-policy-source-catalog-v1")

    documents = catalog.get("documents")
    if not isinstance(documents, list):
        return [*errors, "documents must be an array"]

    seen_source_ids: set[str] = set()
    root = Path(package_root) if package_root is not None else None
    for index, document in enumerate(documents):
        if not isinstance(document, dict):
            errors.append(f"documents[{index}] must be an object")
            continue
        source_id = str(document.get("source_id") or f"documents[{index}]")
        missing = sorted(_REQUIRED_DOCUMENT_FIELDS - document.keys())
        if missing:
            errors.append(f"{source_id}: missing fields: {', '.join(missing)}")
            continue
        if source_id in seen_source_ids:
            errors.append(f"{source_id}: duplicate source_id")
        seen_source_ids.add(source_id)

        expected_hash = str(document["document_hash"])
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_hash):
            errors.append(f"{source_id}: invalid document_hash")
        if root is not None:
            try:
                actual_hash = "sha256:" + hashlib.sha256(
                    _document_bytes(document, root)
                ).hexdigest()
            except (FileNotFoundError, KeyError, OSError, zipfile.BadZipFile) as exc:
                errors.append(f"{source_id}: source unavailable: {exc}")
            else:
                if actual_hash != expected_hash:
                    errors.append(f"{source_id}: document_hash mismatch")

    rule_source_map = catalog.get("rule_source_map")
    if not isinstance(rule_source_map, dict):
        errors.append("rule_source_map must be an object")
        return errors

    expected_rule_ids = {f"G{number:02d}" for number in range(1, 21)}
    actual_rule_ids = set(rule_source_map)
    for missing_rule_id in sorted(expected_rule_ids - actual_rule_ids):
        errors.append(f"{missing_rule_id}: missing rule source mapping")
    for unknown_rule_id in sorted(actual_rule_ids - expected_rule_ids):
        errors.append(f"{unknown_rule_id}: unknown guardrail rule")

    documents_by_id = {
        str(item["source_id"]): item
        for item in documents
        if isinstance(item, dict) and "source_id" in item
    }
    active_source_versions = catalog.get("active_source_versions")
    active_source_ids: set[str] = set()
    if not isinstance(active_source_versions, dict):
        errors.append("active_source_versions must be an object")
    else:
        source_families = {
            str(item.get("source_family_id"))
            for item in documents_by_id.values()
            if item.get("source_family_id")
        }
        for family_id in sorted(source_families - set(active_source_versions)):
            errors.append(f"{family_id}: missing active source version")
        for family_id, active_source_id in active_source_versions.items():
            active_source_id = str(active_source_id)
            active_document = documents_by_id.get(active_source_id)
            if active_document is None:
                errors.append(f"{family_id}: unknown active source_id {active_source_id}")
                continue
            if active_document.get("source_family_id") != family_id:
                errors.append(
                    f"{active_source_id}: active version belongs to a different family"
                )
                continue
            active_source_ids.add(active_source_id)

    for source_id, document in documents_by_id.items():
        supersedes_source_id = document.get("supersedes_source_id")
        if supersedes_source_id is None:
            continue
        predecessor = documents_by_id.get(str(supersedes_source_id))
        if predecessor is None:
            errors.append(f"{source_id}: unknown supersedes_source_id")
        elif source_id == supersedes_source_id:
            errors.append(f"{source_id}: cannot supersede itself")
        elif predecessor.get("source_family_id") != document.get("source_family_id"):
            errors.append(f"{source_id}: predecessor belongs to a different family")

    for rule_id, source_ids in rule_source_map.items():
        if not isinstance(source_ids, list) or not source_ids:
            errors.append(f"{rule_id}: source mapping must be a non-empty array")
            continue
        for source_id in source_ids:
            document = documents_by_id.get(str(source_id))
            if document is None:
                errors.append(f"{rule_id}: unknown source_id {source_id}")
            elif rule_id not in document.get("rule_ids", []):
                errors.append(f"{rule_id}: {source_id} does not declare the rule")
            elif active_source_ids and source_id not in active_source_ids:
                errors.append(f"{rule_id}: {source_id} is not the active source version")

    for source_id, document in documents_by_id.items():
        if active_source_ids and source_id not in active_source_ids:
            continue
        for rule_id in document.get("rule_ids", []):
            if source_id not in rule_source_map.get(rule_id, []):
                errors.append(f"{source_id}: {rule_id} missing reverse mapping")

    return errors

