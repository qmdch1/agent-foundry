def storage_notice(manifest, program_id, result):
    """Attribute explicit tool storage outcomes to trusted Registry identity."""
    if not manifest.requires_db or not isinstance(result, dict):
        return None
    storage = result.get("storage")
    if not isinstance(storage, dict) or storage.get("action") not in (
        "created", "updated", "reused", "read"
    ):
        return None
    record_type = storage.get("record_type")
    if not isinstance(record_type, str) or not 1 <= len(record_type) <= 80:
        return None
    return {
        "program_id": str(program_id),
        "program_name": manifest.name,
        "action": storage["action"],
        "record_type": record_type,
    }
