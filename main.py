"""
Download all files from a Google Drive folder, recreating the folder structure locally.

Configuration: edit config.json (see config.default.json for the schema).
Usage: uv run main.py

Auth setup:
    1. Create a Google Cloud project and enable the Drive API.
    2. Download OAuth 2.0 credentials and save as credentials.json
       in the same directory as this script.
    3. On first run, a browser window will open for authorization.
       The resulting token is cached in token.json next to credentials.json.
"""

import io
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

_SCRIPT_DIR = Path(__file__).parent

CREDENTIALS_FILE = _SCRIPT_DIR / "credentials.json"
TOKEN_FILE = _SCRIPT_DIR / "token.json"
CONFIG_FILE = _SCRIPT_DIR / "config.json"
METADATA_FILE = "_metadata.json"

GOOGLE_FOLDER_MIME = "application/vnd.google-apps.folder"

AUDIO_VIDEO_MIME_PREFIXES = ("audio/", "video/")

_SEGMENT_RE = re.compile(r'\[(\d{2}:\d{2}:\d{2}\.\d{3}) --> (\d{2}:\d{2}:\d{2}\.\d{3})\]\s*(.*)')

GOOGLE_EXPORT_MAP: dict[str, tuple[str, str]] = {
    "application/vnd.google-apps.document": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".docx",
    ),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
    "application/vnd.google-apps.presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".pptx",
    ),
    "application/vnd.google-apps.drawing": ("image/svg+xml", ".svg"),
}


# ---------------------------------------------------------------------------
# Audio/video transcription (ffmpeg + whisper.cpp)
# ---------------------------------------------------------------------------


def _convert_to_wav(src: Path, wav: Path) -> None:
    """Convert any audio/video file to 16 kHz mono PCM WAV for whisper.cpp."""
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(src),
            "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
            str(wav),
        ],
        check=True,
        capture_output=True,
    )


def _ts_to_sec(ts: str) -> float:
    h, m, s = ts.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def _parse_segments(output: str) -> list[tuple[float, float, str]]:
    out = []
    for line in output.splitlines():
        m = _SEGMENT_RE.match(line.strip())
        if m:
            out.append((_ts_to_sec(m.group(1)), _ts_to_sec(m.group(2)), m.group(3).strip()))
    return out


def _is_loop(text: str) -> bool:
    words = [w.lower().strip(".,!?;:\"'") for w in text.split()]
    words = [w for w in words if w]
    if len(words) < 6:
        return False
    top = max(set(words), key=words.count)
    return words.count(top) / len(words) >= 0.4


def _run_whisper(wav: Path, whisper_bin: Path, model: Path, whisper_dir: Path) -> str:
    result = subprocess.run(
        [str(whisper_bin), "-m", str(model), "-f", str(wav.resolve()), "--language", "it"],
        check=True,
        capture_output=True,
        text=True,
        cwd=str(whisper_dir),
    )
    return result.stdout


def _cut_wav(src: Path, dest: Path, start_sec: float) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(src), "-ss", str(start_sec),
            "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(dest),
        ],
        check=True,
        capture_output=True,
    )


def _transcribe(wav: Path, txt: Path, whisper_dir: Path, whisper_model: str) -> None:
    """Transcribe wav → txt, restarting on hallucination loops."""
    whisper_bin = whisper_dir / "build/bin/whisper-cli"
    model = whisper_dir / whisper_model

    collected: list[str] = []
    current_wav = wav
    temp_wavs: list[Path] = []
    abs_offset = 0.0
    MAX_PASSES = 8

    try:
        for pass_num in range(MAX_PASSES):
            if pass_num > 0:
                print(f"  [whisper] pass {pass_num + 1}: restarting from {abs_offset:.1f}s …")

            raw = _run_whisper(current_wav, whisper_bin, model, whisper_dir)
            segments = _parse_segments(raw)

            if not segments:
                break

            loop_start: float | None = None
            for start, end, text in segments:
                if _is_loop(text):
                    loop_start = start
                    print(f"  [whisper] loop at +{start:.1f}s (abs {abs_offset + start:.1f}s) — skipping")
                    break
                collected.append(text)

            if loop_start is None:
                break

            skip = loop_start + 0.5
            remaining = segments[-1][1] - skip
            if remaining < 2.0:
                break

            temp_wav = wav.parent / f"_temp_{pass_num}.wav"
            temp_wavs.append(temp_wav)
            _cut_wav(current_wav, temp_wav, skip)
            abs_offset += skip
            current_wav = temp_wav
    finally:
        for f in temp_wavs:
            f.unlink(missing_ok=True)

    final = "\n".join(t for t in collected if t)
    txt.write_text(final)
    print(f"  [OK] Transcript '{txt.name}' ({len(final)} chars)")


def transcribe_file(local_path: Path, whisper_dir: Path, whisper_model: str) -> None:
    """Extract audio from local_path, convert to WAV, transcribe, save .txt."""
    wav_path = local_path.with_suffix(".wav")
    txt_path = local_path.with_suffix(".txt")

    if txt_path.exists():
        print(f"  [SKIP] Transcript '{txt_path.name}' already exists")
        return

    try:
        print(f"  Converting '{local_path.name}' to WAV …")
        _convert_to_wav(local_path, wav_path)
        _transcribe(wav_path, txt_path, whisper_dir, whisper_model)
    finally:
        if wav_path.exists():
            wav_path.unlink()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def get_credentials() -> Credentials:
    creds: Credentials | None = None

    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                sys.exit(
                    f"credentials.json not found at {CREDENTIALS_FILE}\n"
                    "Download it from the Google Cloud Console (OAuth 2.0 client) "
                    "and place it in the same directory as this script."
                )
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())

    return creds


# ---------------------------------------------------------------------------
# Config & metadata
# ---------------------------------------------------------------------------


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        sys.exit(
            "config.json not found. "
            "Copy config.default.json to config.json and fill in your values."
        )
    try:
        config = json.loads(CONFIG_FILE.read_text())
    except json.JSONDecodeError as e:
        sys.exit(f"config.json is not valid JSON: {e}")
    if "folders" not in config or not isinstance(config["folders"], list):
        sys.exit("config.json must contain a 'folders' list.")
    for entry in config["folders"]:
        if not isinstance(entry, dict) or "url" not in entry:
            sys.exit(
                "Each entry in 'folders' must be an object with at least a 'url' key. "
                "Optionally add an 'output' key to override the global output path."
            )
    return config


def load_metadata(base_dir: Path) -> dict:
    meta_path = base_dir / METADATA_FILE
    if meta_path.exists():
        return json.loads(meta_path.read_text())
    return {}


def save_metadata(base_dir: Path, metadata: dict) -> None:
    meta_path = base_dir / METADATA_FILE
    meta_path.write_text(json.dumps(metadata, indent=2))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def extract_folder_id(url: str) -> str | None:
    m = re.search(r"/folders/([a-zA-Z0-9_-]+)", url)
    if m:
        return m.group(1)
    return None


def list_folder_items(service, folder_id: str) -> list[dict]:
    items: list[dict] = []
    page_token: str | None = None
    query = f"'{folder_id}' in parents and trashed = false"
    fields = "nextPageToken, files(id,name,mimeType,md5Checksum,modifiedTime)"

    while True:
        resp = (
            service.files()
            .list(q=query, fields=fields, pageToken=page_token)
            .execute()
        )
        items.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return items


# ---------------------------------------------------------------------------
# Download logic
# ---------------------------------------------------------------------------


def download_file_item(
    service,
    file_meta: dict,
    dest_dir: Path,
    base_dir: Path,
    metadata: dict,
    stats: dict,
    whisper_dir: Path | None = None,
    whisper_model: str | None = None,
) -> None:
    file_id: str = file_meta["id"]
    name: str = file_meta["name"]
    mime_type: str = file_meta["mimeType"]
    drive_md5: str | None = file_meta.get("md5Checksum")
    modified_time: str = file_meta.get("modifiedTime", "")
    is_native = mime_type.startswith("application/vnd.google-apps.")

    if is_native:
        export_info = GOOGLE_EXPORT_MAP.get(mime_type)
        if not export_info:
            print(f"  [SKIP] Unsupported Google type '{mime_type}' for '{name}'")
            stats["skipped"] += 1
            return
        export_mime, ext = export_info
        filename = name + ext
    else:
        filename = name

    local_path = dest_dir / filename
    cached = metadata.get(file_id, {})

    if is_native:
        if cached.get("modifiedTime") == modified_time:
            print(f"  [SKIP] '{filename}' unchanged (modifiedTime match)")
            stats["skipped"] += 1
            return
        if cached:
            print(f"  [UPDATE] '{filename}' changed on Drive, re-downloading...")
            stats["updated"] += 1
        else:
            stats["downloaded"] += 1
    else:
        if drive_md5 and cached.get("md5") == drive_md5:
            print(f"  [SKIP] '{filename}' unchanged (MD5 match)")
            stats["skipped"] += 1
            return
        if cached:
            stats["updated"] += 1
            if drive_md5:
                print(f"  [UPDATE] '{filename}' MD5 mismatch, re-downloading...")
            else:
                print(f"  [UPDATE] '{filename}' Drive provides no checksum, re-downloading...")
        else:
            stats["downloaded"] += 1

    if local_path.exists():
        local_path.unlink()

    if is_native:
        request = service.files().export_media(fileId=file_id, mimeType=export_mime)
    else:
        request = service.files().get_media(fileId=file_id)

    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        status, done = downloader.next_chunk()
        pct = int(status.progress() * 100) if status else 0
        print(f"  Downloading '{filename}'... {pct}%", end="\r")

    local_path.write_bytes(buf.getvalue())
    print(f"  [OK] '{filename}' saved.          ")

    metadata[file_id] = {
        "filename": filename,
        "modifiedTime": modified_time,
        "md5": drive_md5,
    }

    if mime_type.startswith(AUDIO_VIDEO_MIME_PREFIXES):
        if whisper_dir and whisper_model:
            try:
                transcribe_file(local_path, whisper_dir, whisper_model)
            except subprocess.CalledProcessError as e:
                stderr = e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
                print(f"  [ERROR] Transcription failed: {stderr[:200]}")
                stats["errors"] += 1
        else:
            print(f"  [SKIP] Audio/video file '{filename}' — whisper not configured")


def download_folder_recursive(
    service,
    folder_id: str,
    dest_dir: Path,
    base_dir: Path,
    metadata: dict,
    stats: dict,
    whisper_dir: Path | None = None,
    whisper_model: str | None = None,
) -> None:
    try:
        items = list_folder_items(service, folder_id)
    except HttpError as e:
        print(f"  [ERROR] Cannot list folder {folder_id}: {e}")
        stats["errors"] += 1
        return

    for item in items:
        if item["mimeType"] == GOOGLE_FOLDER_MIME:
            subfolder = dest_dir / item["name"]
            subfolder.mkdir(exist_ok=True)
            print(f"\n  Folder: {item['name']}/")
            download_folder_recursive(
                service, item["id"], subfolder, base_dir, metadata, stats,
                whisper_dir, whisper_model,
            )
        else:
            try:
                download_file_item(
                    service, item, dest_dir, base_dir, metadata, stats,
                    whisper_dir, whisper_model,
                )
            except Exception as e:
                print(f"  [ERROR] '{item.get('name', item['id'])}': {e}")
                stats["errors"] += 1
            finally:
                save_metadata(base_dir, metadata)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    config = load_config()
    global_output: str | None = config.get("output")
    folder_entries: list[dict] = config["folders"]

    if not folder_entries:
        sys.exit("No folders listed in config.json 'folders'. Nothing to do.")

    creds = get_credentials()
    service = build("drive", "v3", credentials=creds)

    whisper_dir: Path | None = None
    whisper_model: str | None = None
    if "whisper_dir" in config and "whisper_model" in config:
        whisper_dir = Path(config["whisper_dir"]).expanduser()
        whisper_model = config["whisper_model"]

    today = date.today().isoformat()
    total_stats = {"downloaded": 0, "skipped": 0, "updated": 0, "errors": 0}

    for entry in folder_entries:
        url: str = entry["url"]
        output_str: str | None = entry.get("output") or global_output
        if not output_str:
            print(f"[ERROR] No output path for '{url}'. Set 'output' globally or per-folder.")
            total_stats["errors"] += 1
            continue

        base_dir = Path(output_str).expanduser()
        folder_id = extract_folder_id(url)
        if not folder_id:
            print(f"[ERROR] Cannot extract folder ID from: {url}")
            total_stats["errors"] += 1
            continue

        try:
            folder_meta = (
                service.files().get(fileId=folder_id, fields="name").execute()
            )
        except HttpError as e:
            print(f"[ERROR] Cannot access folder {folder_id}: {e}")
            total_stats["errors"] += 1
            continue

        folder_name: str = folder_meta["name"]
        dest_dir = base_dir / today / folder_name
        dest_dir.mkdir(parents=True, exist_ok=True)

        metadata = load_metadata(base_dir)
        stats = {"downloaded": 0, "skipped": 0, "updated": 0, "errors": 0}

        print(f"\nDownloading folder: '{folder_name}' → {dest_dir}/")
        download_folder_recursive(
            service, folder_id, dest_dir, base_dir, metadata, stats,
            whisper_dir, whisper_model,
        )

        for k in total_stats:
            total_stats[k] += stats[k]

    print(
        f"\nDone. "
        f"{total_stats['downloaded']} downloaded, "
        f"{total_stats['updated']} updated, "
        f"{total_stats['skipped']} skipped, "
        f"{total_stats['errors']} errors."
    )


if __name__ == "__main__":
    main()
