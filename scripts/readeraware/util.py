from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

WORD_RE = re.compile(r"\b[A-Za-z0-9]+(?:['’\-][A-Za-z0-9]+)*\b")
KEY_RE = re.compile(r"\b(?:sk|ark)-[A-Za-z0-9_-]{16,}\b")
AUTH_RE = re.compile(r"(?i)((?:authorization\s*[:=]\s*(?:bearer\s+)?|x-api-key\s*[:=]\s*)[\"']?)([^\s\"',}]+)")


class ValidationError(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def text_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def json_write(path: Path, value: Any) -> None:
    text_write(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def redact(text: str) -> str:
    text = KEY_RE.sub("[REDACTED_KEY]", text)
    return AUTH_RE.sub(lambda m: m.group(1) + "[REDACTED_AUTH]", text)


def check_no_credentials(text: str, location: str) -> None:
    if redact(text) != text:
        raise ValidationError(f"Potential credential in {location}; clean the text before running. Its value is not logged.")


def validate_schema(value: Any, schema: dict, at: str = "$", root: dict | None = None) -> None:
    """Validate the deliberately small JSON Schema subset used in this project."""
    root = schema if root is None else root
    if "$ref" in schema:
        ref = schema["$ref"]
        if not ref.startswith("#/"):
            raise ValidationError(f"{at}: external schema references are unsupported")
        target = root
        for segment in ref[2:].split("/"):
            target = target[segment.replace("~1", "/").replace("~0", "~")]
        validate_schema(value, target, at, root)
        return
    kind = schema.get("type")
    match kind:
        case "object":
            if not isinstance(value, dict):
                raise ValidationError(f"{at}: expected object")
            properties = schema.get("properties", {})
            missing = set(schema.get("required", [])) - value.keys()
            if missing:
                raise ValidationError(f"{at}: missing required fields {sorted(missing)}")
            if schema.get("additionalProperties") is False and value.keys() - properties.keys():
                raise ValidationError(f"{at}: unexpected fields")
            for key, child in properties.items():
                if key in value:
                    validate_schema(value[key], child, f"{at}.{key}", root)
        case "array":
            if not isinstance(value, list):
                raise ValidationError(f"{at}: expected array")
            if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", float("inf")):
                raise ValidationError(f"{at}: invalid item count")
            for i, item in enumerate(value):
                validate_schema(item, schema.get("items", {}), f"{at}[{i}]", root)
        case "string":
            if not isinstance(value, str):
                raise ValidationError(f"{at}: expected string")
            if len(value.strip()) < schema.get("minLength", 0):
                raise ValidationError(f"{at}: string is empty or too short")
            if "pattern" in schema and re.search(schema["pattern"], value) is None:
                raise ValidationError(f"{at}: invalid string pattern")
        case "integer":
            if type(value) is not int:
                raise ValidationError(f"{at}: expected integer")
            if value < schema.get("minimum", -float("inf")) or value > schema.get("maximum", float("inf")):
                raise ValidationError(f"{at}: number out of range")
        case "number":
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValidationError(f"{at}: expected finite number")
            if value < schema.get("minimum", -float("inf")) or value > schema.get("maximum", float("inf")):
                raise ValidationError(f"{at}: number out of range")
        case "boolean":
            if type(value) is not bool:
                raise ValidationError(f"{at}: expected boolean")
        case None:
            pass
        case _:
            raise ValidationError(f"Unsupported project schema type at {at}: {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValidationError(f"{at}: invalid enumeration")

