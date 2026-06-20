from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import dropbox
from dropbox.exceptions import ApiError, AuthError

from mediasync import __version__


MANIFEST_NAME = "mediasync.json"
STATE_DIR_NAME = ".mediasync"
CREDS_NAME = "credentials.json"


@dataclass
class RepoPaths:
    root: Path
    manifest: Path
    state_dir: Path
    credentials: Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mediasync",
        description="Synchronize media from multiple sources between devices.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    subparsers = parser.add_subparsers(dest="command")

    init_parser = subparsers.add_parser("init", help="Initialize a mediasync repo.")
    init_parser.add_argument(
        "--project",
        help="Project name to write into the local manifest. Defaults to the current directory name.",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "init":
            return cmd_init(args)

        repo = find_repo(Path.cwd())
        if repo is None:
            print("No mediasync repo found in the current directory or its parents.")
            parser.print_help()
            return 1

        manifest = load_manifest(repo.manifest)
        changed = ensure_remote_origin(repo, manifest)
        if changed:
            save_manifest(repo.manifest, manifest)

        print(f"mediasync repo: {repo.root}")
        print(f"manifest: {repo.manifest}")
        remote_origin = manifest.get("remote_origin")
        if remote_origin:
            print(f"remote origin: {remote_origin['url']}")
        else:
            print("remote origin: not configured")
        return 0
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        return 1
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1


def cmd_init(args: argparse.Namespace) -> int:
    root = Path.cwd()
    repo = repo_paths(root)

    repo.state_dir.mkdir(exist_ok=True)

    if repo.manifest.exists():
        manifest = load_manifest(repo.manifest)
    else:
        manifest = new_manifest(args.project or root.name)
        save_manifest(repo.manifest, manifest)
        print(f"Created {repo.manifest}")

    changed = ensure_remote_origin(repo, manifest)
    if changed:
        save_manifest(repo.manifest, manifest)
        print(f"Updated {repo.manifest}")

    print(f"Initialized mediasync repo in {repo.root}")
    return 0


def find_repo(start: Path) -> RepoPaths | None:
    current = start.resolve()
    for candidate in (current, *current.parents):
        manifest = candidate / MANIFEST_NAME
        if manifest.exists():
            return repo_paths(candidate)
    return None


def repo_paths(root: Path) -> RepoPaths:
    return RepoPaths(
        root=root.resolve(),
        manifest=root.resolve() / MANIFEST_NAME,
        state_dir=root.resolve() / STATE_DIR_NAME,
        credentials=root.resolve() / STATE_DIR_NAME / CREDS_NAME,
    )


def new_manifest(project_name: str) -> dict[str, Any]:
    return {
        "version": 1,
        "project": project_name,
        "files": [],
    }


def load_manifest(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object.")
    return data


def save_manifest(path: Path, manifest: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(manifest, handle, indent=2)
        handle.write("\n")


def ensure_remote_origin(repo: RepoPaths, manifest: dict[str, Any]) -> bool:
    remote_origin = manifest.get("remote_origin")
    if remote_origin:
        return False

    target = prompt_target_type()
    if target != "dropbox":
        raise RuntimeError(f"Unsupported target type: {target}")

    creds = prompt_dropbox_credentials()
    repo.state_dir.mkdir(exist_ok=True)
    save_credentials(repo.credentials, creds)

    print("Uploading manifest to Dropbox...")
    remote_origin = bootstrap_dropbox_manifest(repo, creds)
    manifest["remote_origin"] = remote_origin
    save_manifest(repo.manifest, manifest)
    sync_manifest_to_dropbox(repo, creds, remote_origin["path"])
    return True


def prompt_target_type() -> str:
    print("This repo has no remote origin configured.")
    print("Choose a target type:")
    print("1. Dropbox")
    while True:
        selection = input("Target [1]: ").strip()
        if selection in {"", "1", "dropbox", "Dropbox"}:
            return "dropbox"
        print("Only Dropbox is available right now.")


def prompt_dropbox_credentials() -> dict[str, Any]:
    print("Enter Dropbox credentials for the remote manifest.")
    access_token = prompt_non_empty("Dropbox access token: ")
    remote_folder = prompt_non_empty("Dropbox folder path (for example /Apps/mediasync/my-project): ")
    return {
        "dropbox": {
            "access_token": access_token,
            "folder_path": normalize_dropbox_path(remote_folder),
        }
    }


def prompt_non_empty(prompt: str) -> str:
    while True:
        value = input(prompt).strip()
        if value:
            return value
        print("A value is required.")


def save_credentials(path: Path, credentials: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(credentials, handle, indent=2)
        handle.write("\n")


def load_credentials(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def bootstrap_dropbox_manifest(repo: RepoPaths, credentials: dict[str, Any]) -> dict[str, str]:
    dropbox_config = credentials["dropbox"]
    access_token = dropbox_config["access_token"]
    folder_path = dropbox_config["folder_path"]

    client = dropbox.Dropbox(oauth2_access_token=access_token)
    validate_dropbox_auth(client)

    remote_manifest_path = join_dropbox_path(folder_path, MANIFEST_NAME)
    upload_file_to_dropbox(client, repo.manifest, remote_manifest_path)

    shared_url = get_or_create_shared_link(client, remote_manifest_path)
    direct_url = make_direct_url(shared_url)

    return {
        "kind": "dropbox",
        "path": remote_manifest_path,
        "url": direct_url,
    }


def sync_manifest_to_dropbox(repo: RepoPaths, credentials: dict[str, Any], remote_manifest_path: str) -> None:
    client = dropbox.Dropbox(oauth2_access_token=credentials["dropbox"]["access_token"])
    upload_file_to_dropbox(client, repo.manifest, remote_manifest_path)


def upload_file_to_dropbox(client: dropbox.Dropbox, local_path: Path, remote_path: str) -> None:
    with local_path.open("rb") as handle:
        client.files_upload(handle.read(), remote_path, mode=dropbox.files.WriteMode.overwrite)


def validate_dropbox_auth(client: dropbox.Dropbox) -> None:
    try:
        client.users_get_current_account()
    except AuthError as exc:
        raise RuntimeError("Dropbox authentication failed. Check the access token.") from exc


def get_or_create_shared_link(client: dropbox.Dropbox, remote_path: str) -> str:
    try:
        shared_link = client.sharing_create_shared_link_with_settings(remote_path)
        return shared_link.url
    except ApiError as exc:
        if not is_shared_link_conflict(exc):
            raise RuntimeError(f"Failed to create Dropbox shared link for {remote_path}.") from exc

    links = client.sharing_list_shared_links(path=remote_path, direct_only=True).links
    if not links:
        raise RuntimeError(f"Dropbox reported an existing shared link conflict for {remote_path}, but no shared links were returned.")
    return links[0].url


def is_shared_link_conflict(error: ApiError) -> bool:
    if not error.error.is_shared_link_already_exists():
        return False
    return True


def make_direct_url(shared_url: str) -> str:
    parsed = urlparse(shared_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["raw"] = "1"
    query.pop("dl", None)
    return urlunparse(parsed._replace(query=urlencode(query)))


def normalize_dropbox_path(path: str) -> str:
    cleaned = path.strip()
    if not cleaned.startswith("/"):
        cleaned = f"/{cleaned}"
    return cleaned.rstrip("/")


def join_dropbox_path(folder_path: str, name: str) -> str:
    return f"{normalize_dropbox_path(folder_path)}/{name}"
