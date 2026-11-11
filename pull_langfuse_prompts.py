#!/usr/bin/env python3
"""Export explicitly selected Langfuse prompt versions into an isolated snapshot."""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from langfuse_release_common import Client, ReleaseError, inside


def export(client, out_dir, *, label=None, version=None, names=None, max_pages=100):
    if Path(out_dir).is_symlink():
        raise ReleaseError("Output directory must not be a symlink.")
    root = Path(out_dir).resolve()
    if root.exists() and any(root.iterdir()):
        raise ReleaseError("Output directory must be empty; exports never delete existing files.")
    if (label is None) == (version is None):
        raise ReleaseError("Choose exactly one label or version.")
    if version is not None and not names:
        raise ReleaseError("Version selection requires explicit prompt names.")
    selected = list(dict.fromkeys(names or client.list_names(label, max_pages=max_pages)))
    if not selected:
        raise ReleaseError("No prompts selected; no complete snapshot was written.")
    entries, paths = [], set()
    for name in selected:
        remote = client.get(name, label=label, version=version)
        slug = re.sub(r"[^a-zA-Z0-9._-]", "_", name).strip("._")[:60] or "prompt"
        suffix = hashlib.sha256(name.encode()).hexdigest()[:12]
        ext = "txt" if remote["type"] == "text" else "json"
        relative = f"prompts/{slug}-{suffix}.{ext}"
        path = inside(root, relative)
        identity = str(path).casefold()
        if identity in paths:
            raise ReleaseError("Export paths collide; no files were written.")
        paths.add(identity)
        entries.append({"name": name, "file": relative, "baseline": remote})
    manifest = {"format_version": 1, "host": client.host,
                "selector": {"label": label, "version": version}, "prompts": entries}
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Existing empty output directories are also made private on POSIX.
    root.chmod(0o700)
    for entry in entries:
        path = inside(root, entry["file"])
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        remote = entry["baseline"]
        text = remote["prompt"] if remote["type"] == "text" else json.dumps(remote["prompt"], ensure_ascii=False, indent=2) + "\n"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
    # Written last: a missing manifest means the snapshot is incomplete.
    manifest_path = root / "prompt-index.json"
    descriptor = os.open(manifest_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--label")
    selection.add_argument("--version", type=int)
    parser.add_argument("--name", action="append", help="Repeat for each selected prompt.")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--max-pages", type=int, default=100)
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()
    try:
        path = export(Client(args.host, args.timeout), args.out_dir, label=args.label,
                      version=args.version, names=args.name, max_pages=args.max_pages)
        print(f"Snapshot written: {path}")
    except (ReleaseError, OSError, ValueError) as exc:
        parser.exit(1, f"Export stopped: {exc}\n")


if __name__ == "__main__":
    main()
