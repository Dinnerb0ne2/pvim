from __future__ import annotations

import ast
from dataclasses import dataclass
import io
import json
import tokenize
from typing import Any, Iterable, Mapping
from urllib import request


@dataclass(slots=True, frozen=True)
class HttpFetchResult:
    url: str
    status: int
    content_type: str
    body: str


def read_json_mapping(path_text: str) -> dict[str, Any]:
    with open(path_text, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("JSON root must be an object")
    return payload


def deep_merge_dicts(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = dict(base)
    for key, value in override.items():
        previous = merged.get(key)
        if isinstance(previous, Mapping) and isinstance(value, Mapping):
            merged[key] = deep_merge_dicts(previous, value)
        else:
            merged[key] = value
    return merged


def validate_required_keys(payload: Mapping[str, Any], required: Iterable[str]) -> list[str]:
    missing: list[str] = []
    for dotted_key in required:
        key = str(dotted_key).strip()
        if not key:
            continue
        current: Any = payload
        found = True
        for part in key.split("."):
            if not isinstance(current, Mapping) or part not in current:
                found = False
                break
            current = current[part]
        if not found:
            missing.append(key)
    return missing


def fetch_http_text(url: str, *, timeout: float = 5.0, user_agent: str = "PVIM/stdlib") -> HttpFetchResult:
    req = request.Request(url, headers={"User-Agent": user_agent})
    with request.urlopen(req, timeout=timeout) as response:  # noqa: S310 - user-driven url is intentional for command
        status = int(getattr(response, "status", 200))
        content_type = response.headers.get("Content-Type", "")
        raw = response.read()
    body = raw.decode("utf-8", errors="replace")
    return HttpFetchResult(url=url, status=status, content_type=content_type, body=body)


def python_source_summary(source: str) -> dict[str, int]:
    tree = ast.parse(source)
    functions = sum(1 for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)))
    classes = sum(1 for node in ast.walk(tree) if isinstance(node, ast.ClassDef))
    imports = sum(1 for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom)))
    token_count = 0
    for _ in tokenize.generate_tokens(io.StringIO(source).readline):
        token_count += 1
    line_count = len(source.splitlines())
    return {
        "lines": line_count,
        "functions": functions,
        "classes": classes,
        "imports": imports,
        "tokens": token_count,
    }
