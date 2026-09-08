from types import SimpleNamespace

from agent_foundry.storage_notices import storage_notice


def test_storage_identity_comes_from_registry_and_ignores_record_content():
    manifest = SimpleNamespace(requires_db=True, name="record-store")
    result = {"storage": {"action": "created", "record_type": "기록",
                          "program_name": "spoofed", "record_id": "private"}}
    notice = storage_notice(manifest, "id", result)
    assert notice == {"program_id": "id", "program_name": "record-store",
                      "action": "created", "record_type": "기록"}


def test_no_inferred_writes_and_only_top_level_outcomes():
    manifest = SimpleNamespace(requires_db=True, name="comparison")
    assert storage_notice(manifest, "id", {"result": {"storage": {
        "action": "created", "record_type": "비교"}}}) is None
    for result in ({}, {"storage": {"action": "failed", "record_type": "기록"}},
                   {"storage": {"action": "created", "record_type": "x" * 81}}):
        assert storage_notice(manifest, "id", result) is None
    manifest.requires_db = False
    assert storage_notice(manifest, "id", {"storage": {
        "action": "created", "record_type": "기록"}}) is None


def test_read_and_reuse_remain_distinct_from_new_data():
    manifest = SimpleNamespace(requires_db=True, name="comparison")
    for action in ("read", "reused", "updated"):
        assert storage_notice(manifest, "id", {"storage": {
            "action": action, "record_type": "비교"}})["action"] == action
