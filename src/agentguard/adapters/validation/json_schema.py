"""Draft 2020-12 validation with local references only; never fetches URLs."""
import json
from copy import deepcopy

from ...domain.exceptions import ConfigurationError


def validate_schema_config(schema):
    if schema is None:
        return
    nodes = 0
    def walk(value, depth=0):
        nonlocal nodes
        nodes += 1
        if nodes > 5000 or depth > 32:
            raise ConfigurationError("quality.json_schema exceeds structure limits")
        if type(value) is dict:
            for key, child in value.items():
                if type(key) is not str:
                    raise ConfigurationError("quality.json_schema keys must be strings")
                if key in {"$ref", "$dynamicRef"} and (type(child) is not str or not child.startswith("#")):
                    raise ConfigurationError("quality.json_schema only supports local references")
                if key == "$id":
                    raise ConfigurationError("quality.json_schema does not support $id")
                walk(child, depth + 1)
        elif type(value) is list:
            for child in value:
                walk(child, depth + 1)
        elif value is not None and type(value) not in {str, bool, int, float}:
            raise ConfigurationError("quality.json_schema must contain JSON values")
    if type(schema) not in {dict, bool}:
        raise ConfigurationError("quality.json_schema must be an object or boolean")
    walk(schema)
    try:
        raw = json.dumps(schema, allow_nan=False)
        if len(raw.encode()) > 65536:
            raise ValueError()
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise ConfigurationError("quality.json_schema exceeds JSON limits") from None


def build_validator(schema):
    validate_schema_config(schema)
    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        raise ConfigurationError("JSON Schema requires agentguard[quality]") from None
    try:
        Draft202012Validator.check_schema(schema)
        return Draft202012Validator(deepcopy(schema))
    except Exception:
        raise ConfigurationError("quality.json_schema is not a valid Draft 2020-12 schema") from None
