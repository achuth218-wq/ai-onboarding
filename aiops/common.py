import hashlib
import json
import re
from pathlib import Path

import yaml


class Error(Exception):
    pass


class StrictLoader(yaml.SafeLoader):
    pass


def mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise Error(f"Duplicate YAML key: {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)


def read_yaml(path):
    return yaml.load(Path(path).read_text(), Loader=StrictLoader)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def required(condition, message):
    if not condition:
        raise Error(message)


def version(value):
    match = re.fullmatch(r"(\d+)\.(\d+)(?:\.(\d+))?", str(value))
    required(match, f"Expected numeric AI Gateway version, got {value!r}")
    return tuple(int(x or 0) for x in match.groups())


class Tagged:
    def __init__(self, tag, value):
        self.tag, self.value = tag, value


class Dumper(yaml.SafeDumper):
    pass


def represent_tag(dumper, item):
    if isinstance(item.value, dict):
        return dumper.represent_mapping(item.tag, item.value, flow_style=True)
    return dumper.represent_scalar(item.tag, str(item.value))


Dumper.add_representer(Tagged, represent_tag)


def plain(value):
    """JSON Schema checks the resolved type; kongctl resolves actual YAML tags."""
    if isinstance(value, Tagged):
        return "schema-validation-reference"
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [plain(v) for v in value]
    return value
