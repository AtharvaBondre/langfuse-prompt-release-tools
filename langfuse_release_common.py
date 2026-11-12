"""Small, fail-closed helpers for explicit-version Langfuse prompt releases.

Uses the public v2 prompt API. No SDK, implicit production selector, redirect
following, or automatic write retries. Environment variables supply credentials.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import http.client
import ipaddress
import json
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PureWindowsPath
from typing import Any


class ReleaseError(Exception):
    def __init__(self, message: str, unknown_write: bool = False) -> None:
        super().__init__(message)
        self.unknown_write = unknown_write


def _positive_int(value: Any) -> bool:
    return type(value) is int and value > 0


def _json_value(value: Any) -> None:
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _json_value(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _json_value(item)
        return
    raise ReleaseError("Expected JSON data with string keys and finite numbers.")


def fingerprint(value: Any) -> str:
    """Canonical JSON hash: unlike text normalization, preserves actual content."""
    try:
        _json_value(value)
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ReleaseError("Value cannot be represented as canonical JSON.") from None
    return hashlib.sha256(raw).hexdigest()


def _strings(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
        raise ReleaseError(f"Prompt {field} must be a list of strings.")
    return value


def _name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 255:
        raise ReleaseError("Prompt name must be a non-empty string of at most 255 characters.")
    if any(ord(char) < 32 for char in value):
        raise ReleaseError("Prompt name cannot contain control characters.")
    return value


def _label(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9_.-]{1,36}", value):
        raise ReleaseError("Label must be 1–36 lowercase letters, digits, underscores, dots or hyphens.")
    return value


def _content(value: Any, prompt_type: Any) -> None:
    if prompt_type == "text":
        if not isinstance(value, str):
            raise ReleaseError("Text prompt content must be a string.")
    elif prompt_type == "chat":
        if not isinstance(value, list):
            raise ReleaseError("Chat prompt content must be a list.")
        for item in value:
            if not isinstance(item, dict):
                raise ReleaseError("Every chat item must be an object.")
            kind = item.get("type")
            if "name" in item and kind != "placeholder":
                raise ReleaseError("Chat placeholders require explicit type=placeholder.")
            placeholder = kind == "placeholder"
            if placeholder:
                name = item.get("name")
                valid = (set(item) <= {"type", "name"} and isinstance(name, str) and
                         re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name) is not None)
            else:
                valid = (("type" not in item or kind == "chatmessage") and
                         set(item) <= {"type", "role", "content"} and
                         isinstance(item.get("role"), str) and
                         isinstance(item.get("content"), str))
            if not valid:
                raise ReleaseError("Unsupported or malformed chat message/placeholder.")
    else:
        raise ReleaseError("Prompt type must be text or chat.")
    fingerprint(value)


def _canonical_content(value: Any, prompt_type: str) -> Any:
    """Mirror runtime stripping of the optional normal-message discriminator only."""
    _content(value, prompt_type)
    result = copy.deepcopy(value)
    if prompt_type == "chat":
        for item in result:
            if item.get("type") == "chatmessage":
                del item["type"]
    return result


def _check_contract_names(content: Any, prompt_type: str) -> None:
    """The create API rejects chat variables that collide with placeholder names."""
    if prompt_type != "chat":
        return
    variables = {match.strip() for item in content if "content" in item
                 for match in re.findall(r"{{\s*([^{}]+?)\s*}}", item["content"])}
    placeholders = {item["name"] for item in content if item.get("type") == "placeholder"}
    if variables & placeholders:
        raise ReleaseError("Chat template variables and placeholder names must not overlap.")


def validate_prompt(data: Any, expected_name: str | None = None,
                    expected_version: int | None = None) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ReleaseError("Prompt response must be an object.")
    _name(data.get("name"))
    if not _positive_int(data.get("version")):
        raise ReleaseError("Prompt response requires a positive integer version.")
    _content(data.get("prompt"), data.get("type"))
    for field in ("labels", "tags"):
        _strings(data.get(field), field)
    if "config" not in data:
        raise ReleaseError("Prompt response is missing config.")
    fingerprint(data["config"])
    if expected_name is not None and data["name"] != expected_name:
        raise ReleaseError("Returned prompt name does not match the requested name.")
    if expected_version is not None:
        if not _positive_int(expected_version) or data["version"] != expected_version:
            raise ReleaseError("Returned prompt version does not match the requested version.")
    return data


def inside(root: Path, relative: str) -> Path:
    """Resolve a relative path under root, rejecting traversals and symlink entries."""
    if (not isinstance(relative, str) or not relative or "\\" in relative or
            any(ord(char) < 32 for char in relative)):
        raise ReleaseError("Expected a non-empty relative path using forward slashes.")
    rel = Path(relative)
    if rel.is_absolute() or PureWindowsPath(relative).drive or ".." in rel.parts or not rel.parts:
        raise ReleaseError("Absolute paths and parent traversal are not allowed.")
    root = Path(root)
    if root.is_symlink():
        raise ReleaseError("Root directory must not be a symlink.")
    base = root.resolve()
    candidate = base
    for part in rel.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise ReleaseError("Symlink paths are not allowed.")
    try:
        resolved = candidate.resolve()
        resolved.relative_to(base)
    except (ValueError, OSError, RuntimeError):
        raise ReleaseError("Path escapes its root or cannot be resolved.") from None
    return resolved


def contract_signature(prompt_content: Any) -> tuple[Any, ...]:
    """Detect variable/schema-boundary/placeholder changes, not JSON Schema validity."""
    _content(prompt_content, "text" if isinstance(prompt_content, str) else "chat")
    if isinstance(prompt_content, str):
        texts, placeholders = [prompt_content], []
    else:
        texts = [x["content"] for x in prompt_content if "content" in x]
        placeholders = [x["name"] for x in prompt_content if "name" in x]
    variables: set[str] = set()
    schemas: list[str] = []
    for text in texts:
        variables.update(x.strip() for x in re.findall(r"{{\s*([^{}]+?)\s*}}", text))
        blocks = re.findall(r"<OUTPUT_SCHEMA>(.*?)</OUTPUT_SCHEMA>", text, re.DOTALL)
        if text.count("<OUTPUT_SCHEMA>") != len(blocks) or text.count("</OUTPUT_SCHEMA>") != len(blocks):
            raise ReleaseError("Unbalanced or nested OUTPUT_SCHEMA markers.")
        schemas.extend(block.replace("\r\n", "\n").replace("\r", "\n") for block in blocks)
    return tuple(sorted(variables)), tuple(schemas), tuple(sorted(placeholders))


def make_payload(remote: dict[str, Any], new_content: Any,
                 commit_message: str | None = None) -> dict[str, Any]:
    validate_prompt(remote)
    _content(new_content, remote["type"])
    _check_contract_names(new_content, remote["type"])
    labels = [label for label in remote["labels"] if label != "latest"]
    for label in labels:
        _label(label)
    payload = copy.deepcopy({
        "name": remote["name"], "type": remote["type"],
        "prompt": _canonical_content(new_content, remote["type"]),
        # The pinned public API service normalizes submitted null config to {}.
        "config": {} if remote["config"] is None else remote["config"],
        "tags": remote["tags"], "labels": labels,
    })
    if commit_message is not None:
        if not isinstance(commit_message, str) or len(commit_message) > 500:
            raise ReleaseError("Commit message must be a string of at most 500 characters.")
        payload["commitMessage"] = commit_message
    return payload


def verify_exact(actual: Any, payload: dict[str, Any], version: int) -> None:
    validate_prompt(actual, expected_name=payload.get("name"), expected_version=version)
    for field in ("name", "type", "config"):
        if field not in payload or fingerprint(actual[field]) != fingerprint(payload[field]):
            raise ReleaseError(f"Published prompt {field} does not match the intended payload.")
    if fingerprint(_canonical_content(actual["prompt"], actual["type"])) != fingerprint(
            _canonical_content(payload.get("prompt"), payload["type"])):
        raise ReleaseError("Published prompt content does not match the intended payload.")
    if set(_strings(payload.get("tags"), "tags")) != set(actual["tags"]):
        raise ReleaseError("Published prompt tags do not match the intended payload.")
    expected_labels = set(_strings(payload.get("labels"), "labels")) - {"latest"}
    if set(actual["labels"]) - {"latest"} != expected_labels:
        raise ReleaseError("Published prompt labels do not match the intended payload.")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _normalize_host(host: str) -> str:
    if not isinstance(host, str) or any(ord(x) < 33 for x in host) or "\\" in host:
        raise ReleaseError("Host must be an explicit URL without whitespace or backslashes.")
    try:
        url = urllib.parse.urlsplit(host)
        hostname = url.hostname
        port = url.port
    except ValueError:
        raise ReleaseError("Host URL is invalid.") from None
    if (not hostname or url.username is not None or url.password is not None or
            "?" in host or "#" in host or url.scheme not in ("https", "http")):
        raise ReleaseError("Use an HTTPS base URL without credentials, query or fragment.")
    loopback = hostname.lower().rstrip(".") == "localhost"
    try:
        loopback = loopback or ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        pass
    if url.scheme == "http" and not loopback:
        raise ReleaseError("HTTP is allowed only for loopback testing.")
    # Accessing .port above validates a supplied port; keep a valid base-path prefix.
    return urllib.parse.urlunsplit((url.scheme, url.netloc, url.path.rstrip("/"), "", ""))


def strict_json_loads(raw):
    """Reject ambiguous keys and non-finite values before binding local evidence."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ReleaseError("Duplicate JSON key in release evidence.")
            result[key] = value
        return result
    try:
        value = json.loads(raw, object_pairs_hook=pairs)
        fingerprint(value)
        return value
    except (ValueError, RecursionError, UnicodeError):
        raise ReleaseError("Release evidence must be finite, valid JSON.") from None


def validate_project_id(value):
    if (not isinstance(value, str) or not value or len(value) > 256
            or value != value.strip() or any(ord(c) < 33 or ord(c) == 127 for c in value)):
        raise ReleaseError("Choose an explicit nonempty project ID from your target project settings.")
    return value


def selected_project(client, project_id=None, *, snapshot=None):
    expected = validate_project_id(project_id if project_id is not None else
                                   getattr(client, "expected_project_id", None))
    if snapshot is not None:
        if snapshot.get("format_version") != 2 or not snapshot.get("project_id"):
            raise ReleaseError("Legacy snapshot has no project binding; re-export into a new directory before remote operations.")
        if snapshot["project_id"] != expected:
            raise ReleaseError("Snapshot project ID does not match the independently selected target project.")
        binding = snapshot.get("evaluation_policy")
        policy = binding.get("policy") if isinstance(binding, dict) else None
        remote = policy.get("remote_verification") if isinstance(policy, dict) else None
        if remote is not None and (not isinstance(remote, dict) or remote.get("project_id") != expected):
            raise ReleaseError("Remote evaluation policy project differs from the release destination project.")
    return expected


def ensure_project(client, project_id=None, *, snapshot=None):
    expected = selected_project(client, project_id, snapshot=snapshot)
    if not callable(getattr(client, "get_project_id", None)):
        raise ReleaseError("Client must support independent project identity verification.")
    if client.get_project_id() != expected:
        raise ReleaseError("Credentials belong to a different Langfuse project; no release write was sent.")
    if isinstance(client, Client):
        client.expected_project_id = expected
    return expected


class Client:
    def __init__(self, host: str, timeout: int = 30, *, project_id: str | None = None) -> None:
        self.expected_project_id = project_id
        self.host = _normalize_host(host)
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ReleaseError("Timeout must be a positive finite number.")
        self.timeout = timeout
        public = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
        secret = os.environ.get("LANGFUSE_SECRET_KEY", "")
        if not public or not secret:
            raise ReleaseError("Set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY in the environment.")
        token = base64.b64encode(f"{public}:{secret}".encode("utf-8")).decode("ascii")
        self._headers = {"Authorization": f"Basic {token}", "Content-Type": "application/json"}
        self._opener = urllib.request.build_opener(_NoRedirect())

    def _request(self, method: str, path: str, query: dict[str, Any] | None = None,
                 payload: dict[str, Any] | None = None) -> Any:
        if method in {"POST", "PATCH"}:
            ensure_project(self, self.expected_project_id)
        url = self.host + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        body = None
        if payload is not None:
            fingerprint(payload)
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        request = urllib.request.Request(url, data=body, headers=self._headers, method=method)
        writing = method in {"POST", "PATCH"}
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            raise ReleaseError(f"Langfuse {method} request failed (HTTP {code}); response body omitted.",
                               unknown_write=writing and (code >= 500 or 300 <= code < 400 or
                                                          (method == "PATCH" and code == 409))) from None
        except (urllib.error.URLError, OSError, TimeoutError, http.client.HTTPException):
            raise ReleaseError(f"Langfuse {method} transport failed; no automatic retry was made.",
                               unknown_write=writing) from None
        try:
            data = json.loads(raw)
            fingerprint(data)
            return data
        except (ValueError, UnicodeError, RecursionError, ReleaseError):
            raise ReleaseError(f"Langfuse {method} returned invalid JSON; response body omitted.",
                               unknown_write=writing) from None

    def get_project_id(self) -> str:
        data = self._request("GET", "/api/public/projects")
        if (not isinstance(data, dict) or not isinstance(data.get("data"), list)
                or len(data["data"]) != 1 or not isinstance(data["data"][0], dict)):
            raise ReleaseError("Credentials must resolve to exactly one Langfuse project.")
        return validate_project_id(data["data"][0].get("id"))

    def get(self, name: str, label: str | None = None,
            version: int | None = None) -> dict[str, Any]:
        _name(name)
        if (label is None) == (version is None):
            raise ReleaseError("Select exactly one explicit label or version.")
        query: dict[str, Any] = {"resolve": "false"}
        if label is not None:
            query["label"] = _label(label)
        else:
            if not _positive_int(version):
                raise ReleaseError("Version must be a positive integer.")
            query["version"] = version
        path = "/api/public/v2/prompts/" + urllib.parse.quote(name, safe="")
        data = validate_prompt(self._request("GET", path, query), name, version)
        if label is not None and label not in data["labels"]:
            raise ReleaseError("Returned prompt does not carry the requested label.")
        return data

    def list_names(self, label: str, max_pages: int = 100) -> list[str]:
        _label(label)
        if not _positive_int(max_pages):
            raise ReleaseError("max_pages must be a positive integer.")
        names: list[str] = []
        totals: tuple[int, int] | None = None
        for page in range(1, max_pages + 1):
            data = self._request("GET", "/api/public/v2/prompts",
                                 {"label": label, "page": page, "limit": 100})
            if not isinstance(data, dict) or not isinstance(data.get("data"), list) or not isinstance(data.get("meta"), dict):
                raise ReleaseError("Prompt listing is missing data or pagination metadata.")
            meta, items = data["meta"], data["data"]
            if any(type(meta.get(key)) is not int for key in ("page", "limit", "totalPages", "totalItems")):
                raise ReleaseError("Prompt pagination values must be integers.")
            pages, count = meta["totalPages"], meta["totalItems"]
            if (meta["page"] != page or meta["limit"] != 100 or count < 0 or
                    pages != (count + 99) // 100):
                raise ReleaseError("Prompt pagination metadata is inconsistent.")
            if pages > max_pages:
                raise ReleaseError("Prompt listing exceeds max_pages; refusing a partial export.")
            if totals is not None and totals != (pages, count):
                raise ReleaseError("Prompt listing changed during pagination; restart the snapshot.")
            totals = (pages, count)
            if len(items) != min(100, max(0, count - (page - 1) * 100)):
                raise ReleaseError("Prompt listing is incomplete for its declared pagination.")
            for item in items:
                if not isinstance(item, dict):
                    raise ReleaseError("Prompt listing contains an invalid entry.")
                name = _name(item.get("name"))
                if name in names:
                    raise ReleaseError("Prompt listing repeated a name; refusing a partial snapshot.")
                names.append(name)
            if page >= pages:
                return names
        raise ReleaseError("Prompt listing reached max_pages without completion.")

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ReleaseError("Create payload must be an object.")
        checked = dict(payload, version=1)
        validate_prompt(checked)
        _check_contract_names(payload["prompt"], payload["type"])
        if "latest" in payload["labels"]:
            raise ReleaseError("Do not submit the reserved latest label.")
        if set(payload) - {"name", "type", "prompt", "config", "labels", "tags", "commitMessage"}:
            raise ReleaseError("Create payload contains unsupported fields.")
        for label in payload["labels"]:
            _label(label)
        message = payload.get("commitMessage")
        if message is not None and (not isinstance(message, str) or len(message) > 500):
            raise ReleaseError("Commit message must be a string of at most 500 characters.")
        request_payload = copy.deepcopy(payload)
        request_payload["prompt"] = _canonical_content(payload["prompt"], payload["type"])
        if request_payload["config"] is None:
            request_payload["config"] = {}
        data = self._request("POST", "/api/public/v2/prompts", payload=request_payload)
        try:
            return validate_prompt(data, expected_name=payload["name"])
        except ReleaseError:
            raise ReleaseError("Create response could not be validated; inspect remote versions before any retry.",
                               unknown_write=True) from None

    def add_labels(self, name: str, version: int, labels: list[str]) -> dict[str, Any]:
        """Move explicit labels onto an existing version; never clear labels or set latest."""
        _name(name)
        if not _positive_int(version):
            raise ReleaseError("Label promotion requires a positive integer target version.")
        _strings(labels, "newLabels")
        if not labels:
            raise ReleaseError("Label promotion requires at least one explicit label.")
        for label in labels:
            _label(label)
            if label == "latest":
                raise ReleaseError("latest is server-managed and cannot be promoted with PATCH.")
        path = "/api/public/v2/prompts/" + urllib.parse.quote(name, safe="") + f"/versions/{version}"
        data = self._request("PATCH", path, payload={"newLabels": list(dict.fromkeys(labels))})
        try:
            return validate_prompt(data, expected_name=name, expected_version=version)
        except ReleaseError:
            raise ReleaseError("Label response could not be validated; inspect label targets before retrying.",
                               unknown_write=True) from None
