# src/search/service.py (stub — full implementation in Task 13)


def extract_metadata_text(metadata: dict) -> str:
    """Recursively extract all scalar values from metadata into a single text string."""
    values: list[str] = []

    def _walk(obj: object) -> None:
        if isinstance(obj, dict):
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)
        else:
            values.append(str(obj))

    _walk(metadata)
    return " ".join(values)
