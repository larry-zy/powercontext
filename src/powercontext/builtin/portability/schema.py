"""Version-one wire fields, independent of relational table metadata.

Changing persistence columns must not silently change an existing archive format.
Each entry lists the identity fields followed by the authoritative payload fields.
"""

RECORD_FIELDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "source_journal_head": (("scope_id",), ("position",)),
    "source": (("scope_id", "source_type", "source_id"), ("payload", "journal_position")),
    "artifact_revision": (("scope_id", "family", "artifact_id", "revision"), ("content",)),
    "artifact_lineage_source": (
        ("scope_id", "family", "artifact_id", "revision", "ordinal"),
        ("source_type", "source_id"),
    ),
    "artifact_lineage_artifact": (
        ("scope_id", "family", "artifact_id", "revision", "ordinal"),
        ("upstream_family", "upstream_artifact_id", "upstream_revision"),
    ),
    "artifact_head": (("scope_id", "family", "artifact_id"), ("revision",)),
    "memory_entry_version": (
        ("scope_id", "memory_artifact_id", "entry_version_id"),
        (
            "family",
            "entry_id",
            "version",
            "previous_version_id",
            "kind",
            "text",
            "source_refs",
            "artifact_refs",
            "entry_content_hash",
            "created_in_revision",
        ),
    ),
    "memory_entry_head": (
        ("scope_id", "memory_artifact_id", "entry_id"),
        ("family", "head_revision", "entry_version_id", "entry_content_hash"),
    ),
    "candidate_version": (
        ("scope_id", "candidate_id", "version"),
        (
            "family",
            "proposal",
            "source_refs",
            "artifact_refs",
            "target_family",
            "target_artifact_id",
            "target_revision",
            "reason",
        ),
    ),
    "candidate_head": (
        ("scope_id", "candidate_id"),
        ("family", "version", "status", "result_family", "result_artifact_id", "result_revision", "decision_reason"),
    ),
}

BINARY_FIELDS = frozenset({"payload", "content", "proposal", "source_refs", "artifact_refs"})
INTEGER_FIELDS = frozenset({
    "position",
    "journal_position",
    "revision",
    "ordinal",
    "upstream_revision",
    "version",
    "created_in_revision",
    "head_revision",
    "target_revision",
    "result_revision",
})
NULLABLE_FIELDS = frozenset({
    "previous_version_id",
    "target_family",
    "target_artifact_id",
    "target_revision",
    "result_family",
    "result_artifact_id",
    "result_revision",
    "decision_reason",
    "reason",
})
