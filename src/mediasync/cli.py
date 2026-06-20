from __future__ import annotations

import argparse
import ctypes
import json
import msvcrt
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

import dropbox
from dropbox.exceptions import ApiError, AuthError
from yt_dlp import YoutubeDL

from mediasync import __version__


MANIFEST_NAME = "mediasync.json"
STATE_DIR_NAME = ".mediasync"
CREDS_NAME = "credentials.json"
DROPBOX_KIND = "dropbox"
LINK_KIND = "link"
DEFAULT_TARGETS = [DROPBOX_KIND, LINK_KIND]
STD_OUTPUT_HANDLE = -11


class CONSOLE_CURSOR_INFO(ctypes.Structure):
    _fields_ = [("dwSize", ctypes.c_int), ("bVisible", ctypes.c_bool)]


@dataclass
class RepoPaths:
    root: Path
    manifest: Path
    state_dir: Path
    credentials: Path


@dataclass
class LocalFile:
    relative_path: str
    absolute_path: Path
    size: int
    modified_at: datetime


@dataclass
class RemoteFile:
    relative_path: str
    remote_path: str
    size: int
    modified_at: datetime


@dataclass
class SyncAction:
    direction: str
    relative_path: str
    source_kind: str
    selected_target: str | None = None
    remote_path: str | None = None
    url: str | None = None


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

    init_parser = subparsers.add_parser("init", help="Initialize a mediasync pool.")
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
            print("No mediasync pool found in the current directory or its parents.")
            parser.print_help()
            return 1

        return cmd_sync(repo)
    except (EOFError, KeyboardInterrupt):
        set_cursor_visibility(True)
        print("\nCancelled.")
        return 1
    except RuntimeError as exc:
        set_cursor_visibility(True)
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
        print(f"Updated {repo.manifest}")

    print(f"Initialized mediasync pool in {repo.root}")
    return 0


def cmd_sync(repo: RepoPaths) -> int:
    manifest = load_manifest(repo.manifest)
    changed = ensure_remote_origin(repo, manifest)
    if changed:
        manifest = load_manifest(repo.manifest)

    credentials = load_credentials(repo.credentials)
    client = create_dropbox_client(credentials)
    remote_manifest = fetch_remote_manifest(client, manifest)
    if remote_manifest:
        manifest = merge_manifests(repo, manifest, remote_manifest)

    manifest_changed = ensure_manifest_defaults(manifest)
    if manifest_changed:
        save_manifest(repo.manifest, manifest)
        sync_manifest_to_dropbox(repo, credentials, manifest["remote_origin"]["path"])

    local_files = scan_local_files(repo)
    remote_files = list_remote_dropbox_files(client, manifest)
    actions = build_sync_actions(manifest, local_files, remote_files)

    if not actions:
        print("Up to date.")
        return 0

    approved_actions = run_sync_tui(actions, manifest)
    if approved_actions is None:
        return 1

    applied = apply_sync_actions(repo, manifest, credentials, client, approved_actions)
    if applied.manifest_changed:
        save_manifest(repo.manifest, manifest)
        sync_manifest_to_dropbox(repo, credentials, manifest["remote_origin"]["path"])

    if not applied.performed:
        print("Up to date.")
        return 0

    print(f"Applied {applied.performed} change(s).")
    return 0


def find_repo(start: Path) -> RepoPaths | None:
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / MANIFEST_NAME).exists():
            return repo_paths(candidate)
    return None


def repo_paths(root: Path) -> RepoPaths:
    resolved = root.resolve()
    return RepoPaths(
        root=resolved,
        manifest=resolved / MANIFEST_NAME,
        state_dir=resolved / STATE_DIR_NAME,
        credentials=resolved / STATE_DIR_NAME / CREDS_NAME,
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
    if manifest.get("remote_origin"):
        changed = ensure_manifest_defaults(manifest)
        if changed:
            save_manifest(repo.manifest, manifest)
        return changed

    target = choose_option("Choose a target type", [DROPBOX_KIND], label_map={DROPBOX_KIND: "Dropbox"})
    if target != DROPBOX_KIND:
        raise RuntimeError(f"Unsupported target type: {target}")

    creds = prompt_dropbox_credentials()
    repo.state_dir.mkdir(exist_ok=True)
    save_credentials(repo.credentials, creds)

    print("Uploading manifest to Dropbox...")
    remote_origin = bootstrap_dropbox_manifest(repo, creds)
    manifest["remote_origin"] = remote_origin
    ensure_manifest_defaults(manifest)
    save_manifest(repo.manifest, manifest)
    sync_manifest_to_dropbox(repo, creds, remote_origin["path"])
    return True


def ensure_manifest_defaults(manifest: dict[str, Any]) -> bool:
    changed = False

    if "files" not in manifest or not isinstance(manifest["files"], list):
        manifest["files"] = []
        changed = True

    if "default_target" not in manifest:
        remote_origin = manifest.get("remote_origin")
        if remote_origin and remote_origin.get("kind") == DROPBOX_KIND and remote_origin.get("path"):
            manifest["default_target"] = {
                "kind": DROPBOX_KIND,
                "path": str(Path(remote_origin["path"]).parent).replace("\\", "/"),
            }
            changed = True

    return changed


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
    client = create_dropbox_client(credentials)
    validate_dropbox_auth(client)

    remote_manifest_path = join_dropbox_path(dropbox_config["folder_path"], MANIFEST_NAME)
    upload_file_to_dropbox(client, repo.manifest, remote_manifest_path)
    shared_url = get_or_create_shared_link(client, remote_manifest_path)
    direct_url = make_direct_url(shared_url)

    return {
        "kind": DROPBOX_KIND,
        "path": remote_manifest_path,
        "url": direct_url,
    }


def create_dropbox_client(credentials: dict[str, Any]) -> dropbox.Dropbox:
    token = credentials["dropbox"]["access_token"]
    client = dropbox.Dropbox(oauth2_access_token=token)
    validate_dropbox_auth(client)
    return client


def fetch_remote_manifest(client: dropbox.Dropbox, manifest: dict[str, Any]) -> dict[str, Any] | None:
    remote_origin = manifest.get("remote_origin")
    if not remote_origin or remote_origin.get("kind") != DROPBOX_KIND:
        return None

    try:
        _, response = client.files_download(remote_origin["path"])
    except ApiError as exc:
        raise RuntimeError(f"Failed to fetch remote manifest from {remote_origin['path']}.") from exc

    payload = response.content.decode("utf-8")
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise RuntimeError("Remote manifest is not a JSON object.")
    return data


def merge_manifests(repo: RepoPaths, local_manifest: dict[str, Any], remote_manifest: dict[str, Any]) -> dict[str, Any]:
    if not remote_manifest.get("remote_origin") and local_manifest.get("remote_origin"):
        remote_manifest["remote_origin"] = local_manifest["remote_origin"]

    if "default_target" not in remote_manifest and "default_target" in local_manifest:
        remote_manifest["default_target"] = local_manifest["default_target"]

    save_manifest(repo.manifest, remote_manifest)
    return remote_manifest


def list_remote_dropbox_files(client: dropbox.Dropbox, manifest: dict[str, Any]) -> dict[str, RemoteFile]:
    folder_path = get_default_dropbox_folder(manifest)
    results = client.files_list_folder(folder_path, recursive=True)
    files: dict[str, RemoteFile] = {}

    while True:
        for entry in results.entries:
            if isinstance(entry, dropbox.files.FileMetadata):
                relative_path = dropbox_relative_to_local(folder_path, entry.path_display)
                if relative_path == MANIFEST_NAME:
                    continue
                files[relative_path] = RemoteFile(
                    relative_path=relative_path,
                    remote_path=entry.path_display,
                    size=entry.size,
                    modified_at=entry.server_modified,
                )
        if not results.has_more:
            break
        results = client.files_list_folder_continue(results.cursor)

    return files


def scan_local_files(repo: RepoPaths) -> dict[str, LocalFile]:
    files: dict[str, LocalFile] = {}
    for path in repo.root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(repo.root).as_posix()
        if should_ignore_local_path(relative):
            continue
        stat = path.stat()
        files[relative] = LocalFile(
            relative_path=relative,
            absolute_path=path,
            size=stat.st_size,
            modified_at=datetime.fromtimestamp(stat.st_mtime),
        )
    return files


def should_ignore_local_path(relative_path: str) -> bool:
    if relative_path == MANIFEST_NAME:
        return True
    if relative_path.startswith(f"{STATE_DIR_NAME}/"):
        return True
    return False


def build_sync_actions(
    manifest: dict[str, Any],
    local_files: dict[str, LocalFile],
    remote_files: dict[str, RemoteFile],
) -> list[SyncAction]:
    actions: list[SyncAction] = []
    manifest_files = build_manifest_file_map(manifest)
    all_paths = set(local_files) | set(remote_files) | set(manifest_files)

    default_kind = get_default_target_kind(manifest)

    for relative_path in sorted(all_paths):
        local_file = local_files.get(relative_path)
        remote_file = remote_files.get(relative_path)
        file_entry = manifest_files.get(relative_path)
        url_source = find_url_source(file_entry)

        if local_file and remote_file:
            if local_file.size != remote_file.size:
                if local_file.modified_at >= remote_file.modified_at.replace(tzinfo=None):
                    actions.append(
                        SyncAction(
                            direction="upload",
                            relative_path=relative_path,
                            source_kind=DROPBOX_KIND,
                            selected_target=default_kind,
                            remote_path=remote_file.remote_path,
                        )
                    )
                else:
                    actions.append(
                        SyncAction(
                            direction="download",
                            relative_path=relative_path,
                            source_kind=DROPBOX_KIND,
                            remote_path=remote_file.remote_path,
                        )
                    )
            continue

        if local_file and not remote_file:
            if url_source and url_source.get("url"):
                continue
            actions.append(
                SyncAction(
                    direction="upload",
                    relative_path=relative_path,
                    source_kind=DROPBOX_KIND,
                    selected_target=default_kind,
                )
            )
            continue

        if remote_file and not local_file:
            actions.append(
                SyncAction(
                    direction="download",
                    relative_path=relative_path,
                    source_kind=DROPBOX_KIND,
                    remote_path=remote_file.remote_path,
                )
            )
            continue

        if url_source and url_source.get("url"):
            actions.append(
                SyncAction(
                    direction="download",
                    relative_path=relative_path,
                    source_kind=LINK_KIND,
                    url=url_source["url"],
                )
            )

    return actions


def build_manifest_file_map(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    file_map: dict[str, dict[str, Any]] = {}
    for item in manifest.get("files", []):
        if isinstance(item, dict) and isinstance(item.get("path"), str):
            file_map[item["path"]] = item
    return file_map


def find_url_source(file_entry: dict[str, Any] | None) -> dict[str, Any] | None:
    if not file_entry:
        return None
    for source in file_entry.get("sources", []):
        if source.get("kind") == "url" and source.get("url"):
            return source
    return None


def get_default_target_kind(manifest: dict[str, Any]) -> str:
    default_target = manifest.get("default_target", {})
    kind = default_target.get("kind")
    return kind if kind in DEFAULT_TARGETS else DROPBOX_KIND


def get_default_dropbox_folder(manifest: dict[str, Any]) -> str:
    default_target = manifest.get("default_target", {})
    if default_target.get("kind") == DROPBOX_KIND and default_target.get("path"):
        return normalize_dropbox_path(default_target["path"])

    remote_origin = manifest.get("remote_origin")
    if not remote_origin or remote_origin.get("kind") != DROPBOX_KIND:
        raise RuntimeError("Only Dropbox remote origins are supported right now.")
    return normalize_dropbox_path(str(Path(remote_origin["path"]).parent).replace("\\", "/"))


def run_sync_tui(actions: list[SyncAction], manifest: dict[str, Any]) -> list[SyncAction] | None:
    selected = 0
    label_map = {DROPBOX_KIND: "dropbox", LINK_KIND: "link"}

    try:
        set_cursor_visibility(False)
        while True:
            clear_screen()
            print("mediasync\n")
            print(invert("Start") if selected == 0 else "Start")
            print()

            for index, action in enumerate(actions, start=1):
                label = render_action(action, manifest, label_map)
                print(invert(label) if selected == index else label)

            key = get_key()

            if key == b"\xe0H":
                selected = (selected - 1) % (len(actions) + 1)
            elif key == b"\xe0P":
                selected = (selected + 1) % (len(actions) + 1)
            elif key == b"\r":
                if selected == 0:
                    clear_screen()
                    return actions
                selected_action = actions[selected - 1]
                if selected_action.direction == "upload":
                    previous_target = selected_action.selected_target or get_default_target_kind(manifest)
                    chosen = choose_option(
                        f"Choose target for {selected_action.relative_path}",
                        DEFAULT_TARGETS,
                        label_map={DROPBOX_KIND: "Dropbox", LINK_KIND: "Link"},
                    )
                    if chosen == LINK_KIND:
                        url = prompt_optional_value(f"URL for {selected_action.relative_path}: ")
                        if url:
                            selected_action.selected_target = LINK_KIND
                            selected_action.url = url
                        else:
                            selected_action.selected_target = previous_target
                    else:
                        selected_action.selected_target = chosen
                        selected_action.url = None
            elif key == b"\x1b":
                clear_screen()
                return None
    finally:
        set_cursor_visibility(True)


def render_action(action: SyncAction, manifest: dict[str, Any], label_map: dict[str, str]) -> str:
    if action.direction == "upload":
        target = action.selected_target or get_default_target_kind(manifest)
        suffix = f": {label_map.get(target, target)}"
        if target == LINK_KIND and action.url:
            suffix = f"{suffix} [{action.url}]"
        return f"\u2191 {action.relative_path}{suffix}"

    source = "dropbox" if action.source_kind == DROPBOX_KIND else "link"
    return f"\u2193 {action.relative_path} <- {source}"


@dataclass
class ApplyResult:
    performed: int
    manifest_changed: bool


def apply_sync_actions(
    repo: RepoPaths,
    manifest: dict[str, Any],
    credentials: dict[str, Any],
    client: dropbox.Dropbox,
    actions: list[SyncAction],
) -> ApplyResult:
    performed = 0
    manifest_changed = False

    for action in actions:
        destination = repo.root / Path(action.relative_path)
        if action.direction == "upload":
            target = action.selected_target or get_default_target_kind(manifest)
            if target == DROPBOX_KIND:
                remote_path = action.remote_path or join_dropbox_path(get_default_dropbox_folder(manifest), action.relative_path)
                upload_file_to_dropbox(client, destination, remote_path)
                performed += 1
            elif target == LINK_KIND and action.url:
                upsert_manifest_link_source(manifest, action.relative_path, action.url)
                manifest_changed = True
                performed += 1
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if action.source_kind == DROPBOX_KIND and action.remote_path:
                download_dropbox_file(client, action.remote_path, destination)
                performed += 1
            elif action.source_kind == LINK_KIND and action.url:
                download_from_url(action.url, destination)
                performed += 1

    return ApplyResult(performed=performed, manifest_changed=manifest_changed)


def upsert_manifest_link_source(manifest: dict[str, Any], relative_path: str, url: str) -> None:
    files = manifest.setdefault("files", [])
    for item in files:
        if item.get("path") == relative_path:
            sources = item.setdefault("sources", [])
            for source in sources:
                if source.get("kind") == "url":
                    source["url"] = url
                    return
            sources.append({"kind": "url", "url": url})
            return

    files.append(
        {
            "path": relative_path,
            "sources": [{"kind": "url", "url": url}],
        }
    )


def download_dropbox_file(client: dropbox.Dropbox, remote_path: str, destination: Path) -> None:
    try:
        _, response = client.files_download(remote_path)
    except ApiError as exc:
        raise RuntimeError(f"Failed to download {remote_path} from Dropbox.") from exc

    with destination.open("wb") as handle:
        handle.write(response.content)


def download_from_url(url: str, destination: Path) -> None:
    if is_youtube_url(url):
        download_youtube(url, destination)
        return

    request = Request(url, headers={"User-Agent": "mediasync/0.1.0"})
    with urlopen(request) as response:
        content_type = response.headers.get_content_type()
        if not is_direct_media_response(url, content_type):
            raise RuntimeError(f"URL did not resolve to a direct media file: {url}")

        with destination.open("wb") as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)


def is_youtube_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return "youtube.com" in host or "youtu.be" in host


def download_youtube(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    options = {
        "outtmpl": str(destination),
        "quiet": True,
        "noplaylist": True,
        "overwrites": True,
    }
    with YoutubeDL(options) as downloader:
        downloader.download([url])


def is_direct_media_response(url: str, content_type: str) -> bool:
    if content_type.startswith("video/") or content_type.startswith("audio/"):
        return True
    path = urlparse(url).path.lower()
    direct_exts = (".mp4", ".mov", ".mkv", ".webm", ".mp3", ".wav", ".m4a")
    return path.endswith(direct_exts)


def sync_manifest_to_dropbox(repo: RepoPaths, credentials: dict[str, Any], remote_manifest_path: str) -> None:
    client = create_dropbox_client(credentials)
    upload_file_to_dropbox(client, repo.manifest, remote_manifest_path)


def upload_file_to_dropbox(client: dropbox.Dropbox, local_path: Path, remote_path: str) -> None:
    with local_path.open("rb") as handle:
        try:
            client.files_upload(handle.read(), remote_path, mode=dropbox.files.WriteMode.overwrite)
        except AuthError as exc:
            raise RuntimeError("Dropbox token is missing required scopes. Regenerate it after enabling the scopes.") from exc
        except ApiError as exc:
            raise RuntimeError(f"Failed to upload {local_path} to Dropbox path {remote_path}.") from exc


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
    return error.error.is_shared_link_already_exists()


def make_direct_url(shared_url: str) -> str:
    parsed = urlparse(shared_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["raw"] = "1"
    query.pop("dl", None)
    return urlunparse(parsed._replace(query=urlencode(query)))


def normalize_dropbox_path(path: str) -> str:
    cleaned = path.strip().replace("\\", "/")
    if not cleaned.startswith("/"):
        cleaned = f"/{cleaned}"
    return cleaned.rstrip("/")


def join_dropbox_path(folder_path: str, name: str) -> str:
    normalized_name = name.replace("\\", "/").lstrip("/")
    return f"{normalize_dropbox_path(folder_path)}/{normalized_name}"


def dropbox_relative_to_local(folder_path: str, file_path: str) -> str:
    prefix = normalize_dropbox_path(folder_path).rstrip("/")
    relative = file_path[len(prefix):].lstrip("/")
    return relative.replace("\\", "/")


def choose_option(title: str, options: list[str], label_map: dict[str, str] | None = None) -> str:
    selected = 0
    label_map = label_map or {}

    try:
        set_cursor_visibility(False)
        while True:
            clear_screen()
            print(f"{title}\n")
            for index, option in enumerate(options):
                label = label_map.get(option, option)
                print(invert(label) if index == selected else label)

            key = get_key()
            if key == b"\xe0H":
                selected = (selected - 1) % len(options)
            elif key == b"\xe0P":
                selected = (selected + 1) % len(options)
            elif key == b"\r":
                clear_screen()
                return options[selected]
            elif key == b"\x1b":
                raise RuntimeError("Cancelled.")
    finally:
        set_cursor_visibility(True)


def prompt_optional_value(prompt: str) -> str:
    set_cursor_visibility(True)
    try:
        return input(prompt).strip()
    finally:
        set_cursor_visibility(False)


def clear_screen() -> None:
    os.system("cls")


def invert(text: str) -> str:
    return "\033[7m" + text + "\033[0m"


def get_key() -> bytes:
    while True:
        if msvcrt.kbhit():
            key = msvcrt.getch()
            if key == b"\xe0":
                return b"\xe0" + msvcrt.getch()
            return key


def set_cursor_visibility(visible: bool) -> None:
    handle = ctypes.windll.kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
    cursor_info = CONSOLE_CURSOR_INFO()
    ctypes.windll.kernel32.GetConsoleCursorInfo(handle, ctypes.byref(cursor_info))
    cursor_info.bVisible = visible
    ctypes.windll.kernel32.SetConsoleCursorInfo(handle, ctypes.byref(cursor_info))
