"""Minimal JSON Schema (2020-12 subset) validator for the BTC schemas.

The shared fma virtualenv has no ``jsonschema`` and none is added (the XAU
runtime must not change). Only the keywords the BTC schemas use are
implemented; any other keyword raises ``SchemaError`` instead of being
silently ignored. ``format`` is asserted for ``date-time`` (offset required),
``date`` and ``uri`` (absolute http/https).

Cross-checked against jsonschema (Draft 2020-12 with FormatChecker) in a
temporary virtualenv; see docs/btc/README.md.
"""
from __future__ import annotations

from datetime import date, datetime
import json
import math
import re
from urllib.parse import urlsplit

ANNOTATIONS = {'$schema', '$id', '$comment', 'title', 'description', '$defs', 'examples', 'default'}
ASSERTIONS = {'type', 'enum', 'const', 'properties', 'required', 'additionalProperties', 'items', 'minItems',
              'maxItems', 'uniqueItems', 'minLength', 'maxLength', 'pattern', 'minimum', 'maximum', 'anyOf',
              'oneOf', 'allOf', 'if', 'then', 'else', 'format', '$ref', 'not'}
FORMATS = {'date-time', 'date', 'uri'}
_DATE_TIME = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$')


class SchemaError(ValueError):
    """The schema itself uses something this validator does not implement."""


def _is_type(value, kind: str) -> bool:
    if kind == 'null':
        return value is None
    if kind == 'boolean':
        return isinstance(value, bool)
    if kind == 'string':
        return isinstance(value, str)
    if kind == 'object':
        return isinstance(value, dict)
    if kind == 'array':
        return isinstance(value, list)
    if kind == 'number':
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    if kind == 'integer':
        return (isinstance(value, int) and not isinstance(value, bool)) or (
            isinstance(value, float) and math.isfinite(value) and value.is_integer())
    raise SchemaError(f'unsupported type: {kind}')


def _canonical(value) -> str:
    def norm(v):
        if isinstance(v, float) and v.is_integer():
            return int(v)
        if isinstance(v, list):
            return [norm(x) for x in v]
        if isinstance(v, dict):
            return {k: norm(x) for k, x in v.items()}
        return v
    return json.dumps(norm(value), sort_keys=True, ensure_ascii=False)


def _equal(a, b) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    return _canonical(a) == _canonical(b)


def _format_ok(value, fmt: str) -> bool:
    if not isinstance(value, str):
        return True
    if fmt == 'date-time':
        if not _DATE_TIME.match(value):
            return False
        try:
            datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return False
        return True
    if fmt == 'date':
        try:
            return re.fullmatch(r'\d{4}-\d{2}-\d{2}', value) is not None and bool(date.fromisoformat(value))
        except ValueError:
            return False
    if fmt == 'uri':
        parts = urlsplit(value)
        return parts.scheme in ('http', 'https') and bool(parts.netloc)
    raise SchemaError(f'unsupported format: {fmt}')


class Validator:
    def __init__(self, schema: dict):
        self.root = schema
        self.check_schema(schema)

    def check_schema(self, schema, path='#'):
        if isinstance(schema, bool):
            return
        if not isinstance(schema, dict):
            raise SchemaError(f'{path}: schema must be an object')
        unknown = set(schema) - ANNOTATIONS - ASSERTIONS
        if unknown:
            raise SchemaError(f'{path}: unsupported keywords {sorted(unknown)}')
        if 'format' in schema and schema['format'] not in FORMATS:
            raise SchemaError(f'{path}: unsupported format {schema["format"]}')
        for key in ('items', 'additionalProperties', 'if', 'then', 'else', 'not'):
            if key in schema and not isinstance(schema[key], bool):
                self.check_schema(schema[key], f'{path}/{key}')
        for key in ('properties', '$defs'):
            for name, sub in (schema.get(key) or {}).items():
                self.check_schema(sub, f'{path}/{key}/{name}')
        for key in ('anyOf', 'oneOf', 'allOf'):
            for index, sub in enumerate(schema.get(key) or []):
                self.check_schema(sub, f'{path}/{key}/{index}')
        if '$ref' in schema:
            self._resolve(schema['$ref'])

    def _resolve(self, ref: str):
        if not ref.startswith('#/'):
            raise SchemaError(f'only local references are supported: {ref}')
        node = self.root
        for part in ref[2:].split('/'):
            part = part.replace('~1', '/').replace('~0', '~')
            if not isinstance(node, dict) or part not in node:
                raise SchemaError(f'unresolvable reference: {ref}')
            node = node[part]
        return node

    def errors(self, instance) -> list[str]:
        out: list[str] = []
        self._validate(instance, self.root, '$', out)
        return out

    def _validate(self, value, schema, path, out):
        if schema is True:
            return
        if schema is False:
            out.append(f'{path}: not allowed')
            return
        if '$ref' in schema:
            self._validate(value, self._resolve(schema['$ref']), path, out)
        if 'type' in schema:
            kinds = schema['type'] if isinstance(schema['type'], list) else [schema['type']]
            if not any(_is_type(value, k) for k in kinds):
                out.append(f'{path}: expected type {"/".join(kinds)}')
                return
        if 'enum' in schema and not any(_equal(value, x) for x in schema['enum']):
            out.append(f'{path}: value not in enum')
        if 'const' in schema and not _equal(value, schema['const']):
            out.append(f'{path}: value differs from const')
        if 'format' in schema and not _format_ok(value, schema['format']):
            out.append(f'{path}: invalid {schema["format"]}')
        if isinstance(value, str):
            if 'minLength' in schema and len(value) < schema['minLength']:
                out.append(f'{path}: shorter than {schema["minLength"]}')
            if 'maxLength' in schema and len(value) > schema['maxLength']:
                out.append(f'{path}: longer than {schema["maxLength"]}')
            if 'pattern' in schema and not re.search(schema['pattern'], value):
                out.append(f'{path}: does not match pattern')
        if _is_type(value, 'number'):
            if 'minimum' in schema and value < schema['minimum']:
                out.append(f'{path}: below minimum')
            if 'maximum' in schema and value > schema['maximum']:
                out.append(f'{path}: above maximum')
        if isinstance(value, list):
            if 'minItems' in schema and len(value) < schema['minItems']:
                out.append(f'{path}: fewer than {schema["minItems"]} items')
            if 'maxItems' in schema and len(value) > schema['maxItems']:
                out.append(f'{path}: more than {schema["maxItems"]} items')
            if schema.get('uniqueItems'):
                seen = [_canonical(x) for x in value]
                if len(set(seen)) != len(seen):
                    out.append(f'{path}: items are not unique')
            if 'items' in schema:
                for index, item in enumerate(value):
                    self._validate(item, schema['items'], f'{path}[{index}]', out)
        if isinstance(value, dict):
            for name in schema.get('required', []):
                if name not in value:
                    out.append(f'{path}: missing {name}')
            props = schema.get('properties', {})
            for name, item in value.items():
                if name in props:
                    self._validate(item, props[name], f'{path}.{name}', out)
                elif 'additionalProperties' in schema:
                    extra = schema['additionalProperties']
                    if extra is False:
                        out.append(f'{path}: unexpected property {name}')
                    elif extra is not True:
                        self._validate(item, extra, f'{path}.{name}', out)
        for sub in schema.get('allOf', []):
            self._validate(value, sub, path, out)
        if 'anyOf' in schema and not any(not self._sub(value, s, path) for s in schema['anyOf']):
            out.append(f'{path}: matches none of anyOf')
        if 'oneOf' in schema and sum(1 for s in schema['oneOf'] if not self._sub(value, s, path)) != 1:
            out.append(f'{path}: must match exactly one of oneOf')
        if 'not' in schema and not self._sub(value, schema['not'], path):
            out.append(f'{path}: matches a forbidden schema')
        if 'if' in schema:
            if not self._sub(value, schema['if'], path):
                if 'then' in schema:
                    self._validate(value, schema['then'], path, out)
            elif 'else' in schema:
                self._validate(value, schema['else'], path, out)

    def _sub(self, value, schema, path) -> list[str]:
        out: list[str] = []
        self._validate(value, schema, path, out)
        return out


def validate(instance, schema: dict) -> list[str]:
    return Validator(schema).errors(instance)
