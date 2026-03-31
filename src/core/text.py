def extract_metadata_text(metadata: dict) -> str:
    """Recursively extract all scalar values from metadata into a single text string.

    Leaf values only — keys are queryable via the ES flattened metadata field.
    """
    values: list[str] = []

    def _walk(obj: object) -> None:
        if isinstance(obj, dict):
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)
        elif obj is None or isinstance(obj, bool):
            return
        else:
            values.append(str(obj))

    _walk(metadata)
    return " ".join(values)
