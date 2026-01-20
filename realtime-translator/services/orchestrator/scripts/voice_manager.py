"""
Piper Voice Manager (CLI)

Purpose
- Provide a simple, interactive CLI to browse and install Piper voice models.
- Keep voice installation consistent across developers by writing into the shared project folder:
  ...\services\tts\voices\

Data source
- The official Piper voice catalog (voices.json) hosted on Hugging Face.
- Voice files are downloaded from the same repository (resolve URLs).

Key behaviors
- Downloads are verified using size and optional MD5 checks from voices.json metadata.
- Installs only .onnx and .onnx.json artifacts.
- Installation flattens the remote path and stores files directly in the voices folder.

Runtime assumptions
- Script is executed from the orchestrator context:
  ...\services\orchestrator\scripts\voice_manager.py
- Destination voices directory must be:
  ...\services\tts\voices\
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


# -----------------------------------------------------------------------------
# Remote catalog and file base URLs
# -----------------------------------------------------------------------------
VOICES_JSON_URL = "https://huggingface.co/rhasspy/piper-voices/raw/main/voices.json"
RESOLVE_PREFIX = "https://huggingface.co/rhasspy/piper-voices/resolve/main"


# -----------------------------------------------------------------------------
# Project paths
# -----------------------------------------------------------------------------
# Script location:
#   ...\services\orchestrator\scripts
# Voice destination:
#   ...\services\tts\voices
SCRIPT_DIR = Path(__file__).resolve().parent            # ...\services\orchestrator\scripts
ORCH_DIR = SCRIPT_DIR.parent                            # ...\services\orchestrator
SERVICES_DIR = ORCH_DIR.parent                          # ...\services
DEST_DIR = (SERVICES_DIR / "tts" / "voices").resolve()  # ...\services\tts\voices


# -----------------------------------------------------------------------------
# Console helpers
# -----------------------------------------------------------------------------
def clear_screen() -> None:
    """Clear the terminal to keep the interactive UI readable."""
    os.system("cls" if os.name == "nt" else "clear")


def human_bytes(n: int) -> str:
    """Format a byte count into a human-readable string."""
    units = ["B", "KB", "MB", "GB"]
    x = float(n)
    for u in units:
        if x < 1024.0 or u == units[-1]:
            return f"{x:.1f} {u}" if u != "B" else f"{int(x)} {u}"
        x /= 1024.0
    return f"{n} B"


# -----------------------------------------------------------------------------
# File integrity helpers
# -----------------------------------------------------------------------------
def md5_file(path: Path) -> str:
    """Compute MD5 for a file (used to verify downloaded artifacts)."""
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def download_with_progress(
    url: str,
    dst: Path,
    expected_size: int | None,
    expected_md5: str | None,
) -> None:
    """
    Download a file with a simple progress indicator.

    Safety and reliability:
    - Download into a temp file first, then atomically replace the destination.
    - Validate file size if provided.
    - Validate MD5 if provided.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)

    # Use a temp file in the same directory to ensure the final replace is atomic.
    with tempfile.NamedTemporaryFile(
        delete=False,
        dir=str(dst.parent),
        prefix=dst.name + ".",
        suffix=".tmp",
    ) as tf:
        tmp_path = Path(tf.name)

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "piper-voice-manager/1.0"})
        with urllib.request.urlopen(req) as resp, tmp_path.open("wb") as out:
            total = expected_size
            downloaded = 0

            # Read in moderate chunks to keep memory usage low and allow progress output.
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                out.write(chunk)
                downloaded += len(chunk)

                if total and total > 0:
                    pct = int(downloaded * 100 / total)
                    print(
                        f"  {dst.name}: {pct:3d}% ({human_bytes(downloaded)} / {human_bytes(total)})",
                        end="\r",
                    )
                else:
                    print(f"  {dst.name}: {human_bytes(downloaded)}", end="\r")

        print()  # newline after progress line

        # Validate size if available from metadata.
        if expected_size and tmp_path.stat().st_size != expected_size:
            raise RuntimeError(
                f"Size mismatch for {dst.name}: got {tmp_path.stat().st_size}, expected {expected_size}"
            )

        # Validate hash if available from metadata.
        if expected_md5:
            got = md5_file(tmp_path)
            if got.lower() != expected_md5.lower():
                raise RuntimeError(f"MD5 mismatch for {dst.name}: got {got}, expected {expected_md5}")

        # Replace destination atomically.
        if dst.exists():
            dst.unlink()
        tmp_path.replace(dst)

    except Exception:
        # Best-effort cleanup of temp file.
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass
        raise


# -----------------------------------------------------------------------------
# Catalog and local inventory
# -----------------------------------------------------------------------------
def fetch_voices() -> dict[str, Any]:
    """Download and parse the voices.json catalog."""
    req = urllib.request.Request(VOICES_JSON_URL, headers={"User-Agent": "piper-voice-manager/1.0"})
    with urllib.request.urlopen(req) as resp:
        data = resp.read().decode("utf-8", errors="replace")
    return json.loads(data)


def installed_onnx_files() -> list[Path]:
    """List installed .onnx voice files from the destination folder."""
    if not DEST_DIR.exists():
        return []
    return sorted([p for p in DEST_DIR.glob("*.onnx") if p.is_file()])


# -----------------------------------------------------------------------------
# Display helpers
# -----------------------------------------------------------------------------
def voice_display_name(meta: dict[str, Any]) -> str:
    """Return a human-friendly voice name from catalog metadata."""
    return str(meta.get("name", "")).strip() or "unknown"


def lang_label(meta: dict[str, Any]) -> str:
    """Return a readable language label for UI display."""
    lang = meta.get("language", {})
    code = str(lang.get("code", "?"))
    name = str(lang.get("name_english", "?"))
    country = str(lang.get("country_english", "")).strip()
    fam = str(lang.get("family", "")).strip()
    where = f"{name} {country}".strip()

    # Family is helpful when browsing languages with multiple variants.
    if fam:
        return f"{where} [{code}, family={fam}]"
    return f"{where} [{code}]"


def fmt_voice_row(i: int, key: str, meta: dict[str, Any]) -> str:
    """Render one voice entry as a table-like row."""
    lang = meta.get("language", {})
    lang_code = str(lang.get("code", "?"))
    lang_name = str(lang.get("name_english", "?"))
    country = str(lang.get("country_english", "")).strip()
    quality = str(meta.get("quality", "?"))
    speakers = str(meta.get("num_speakers", "?"))

    friendly = voice_display_name(meta)
    where = f"{lang_name} {country}".strip()

    return (
        f"{i:4d}) {friendly:24s}  key={key:32s}  "
        f"{where:24s}  {lang_code:10s}  {quality:7s}  spk:{speakers}"
    )


# -----------------------------------------------------------------------------
# Menu primitives
# -----------------------------------------------------------------------------
def choose_from_list(
    items: list[tuple[str, dict[str, Any]]],
    title: str,
) -> tuple[str, dict[str, Any]] | None:
    """
    Paginated selection helper.

    Returns the selected (key, meta) pair or None if the user cancels.
    """
    if not items:
        print("No results.")
        input("Press Enter...")
        return None

    page_size = 30
    page = 0

    while True:
        clear_screen()
        print(title)
        print("=" * len(title))
        print()

        start = page * page_size
        end = min(start + page_size, len(items))
        for idx in range(start, end):
            k, m = items[idx]
            print(fmt_voice_row(idx + 1, k, m))

        print()
        if len(items) > page_size:
            print(f"Showing {start + 1} to {end} of {len(items)}")
            print("n = next page, p = previous page")
        print("Enter a number to select, or press Enter to go back.")
        print()

        sel = input("Selection: ").strip().lower()
        if sel == "":
            return None
        if sel == "n" and len(items) > page_size:
            if end < len(items):
                page += 1
            continue
        if sel == "p" and len(items) > page_size:
            if page > 0:
                page -= 1
            continue
        if not sel.isdigit():
            continue

        n = int(sel)
        if 1 <= n <= len(items):
            return items[n - 1]


def build_language_index(voices: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Build a compact language index from the full voices catalog.

    Output entries:
      { "code": str, "label": str, "families": set[str], "count": int }
    """
    by_code: dict[str, dict[str, Any]] = {}

    for _, meta in voices.items():
        lang = meta.get("language", {})
        code = str(lang.get("code", "")).strip()
        if not code:
            continue

        name = str(lang.get("name_english", "?"))
        country = str(lang.get("country_english", "")).strip()
        fam = str(lang.get("family", "")).strip()
        label = f"{name} {country}".strip()

        if code not in by_code:
            by_code[code] = {"code": code, "label": label, "families": set(), "count": 0}

        if fam:
            by_code[code]["families"].add(fam)
        by_code[code]["count"] += 1

    items = list(by_code.values())
    items.sort(key=lambda x: (x["label"].lower(), x["code"].lower()))
    return items


def choose_language(lang_items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Paginated language selector. Returns a language entry or None."""
    if not lang_items:
        print("No languages found.")
        input("Press Enter...")
        return None

    page_size = 35
    page = 0

    while True:
        clear_screen()
        print("Languages")
        print("=========")
        print()

        start = page * page_size
        end = min(start + page_size, len(lang_items))
        for i in range(start, end):
            it = lang_items[i]
            fam = ""
            if it["families"]:
                fam = " families=" + ",".join(sorted(it["families"]))
            print(f"{i + 1:4d}) {it['label']:30s}  [{it['code']}]  voices={it['count']}{fam}")

        print()
        if len(lang_items) > page_size:
            print(f"Showing {start + 1} to {end} of {len(lang_items)}")
            print("n = next page, p = previous page")
        print("Enter a number to pick a language, or press Enter to go back.")
        print()

        sel = input("Selection: ").strip().lower()
        if sel == "":
            return None
        if sel == "n" and len(lang_items) > page_size:
            if end < len(lang_items):
                page += 1
            continue
        if sel == "p" and len(lang_items) > page_size:
            if page > 0:
                page -= 1
            continue
        if not sel.isdigit():
            continue

        n = int(sel)
        if 1 <= n <= len(lang_items):
            return lang_items[n - 1]


# -----------------------------------------------------------------------------
# Install and uninstall operations
# -----------------------------------------------------------------------------
def install_voice(key: str, meta: dict[str, Any]) -> None:
    """
    Install a voice by downloading its .onnx and optional .onnx.json files.

    Notes
    - The upstream catalog stores files in nested folders.
    - We intentionally flatten by using only the basename so all voices live in one folder.
    """
    files: dict[str, Any] = meta.get("files", {})
    targets: list[tuple[str, Path, dict[str, Any]]] = []

    for rel_path, finfo in files.items():
        if rel_path.endswith(".onnx") or rel_path.endswith(".onnx.json"):
            url = f"{RESOLVE_PREFIX}/{rel_path}"
            dst = DEST_DIR / Path(rel_path).name
            targets.append((url, dst, finfo))

    if not targets:
        print("No .onnx/.onnx.json files found for this voice.")
        input("Press Enter...")
        return

    clear_screen()
    print("Install voice")
    print("=============")
    print(f"Voice name: {voice_display_name(meta)}")
    print(f"Voice key:  {key}")
    print(f"Language:   {lang_label(meta)}")
    print(f"Install to: {DEST_DIR}")
    print()

    DEST_DIR.mkdir(parents=True, exist_ok=True)

    for url, dst, finfo in targets:
        size_bytes = finfo.get("size_bytes")
        md5_digest = finfo.get("md5_digest")

        if dst.exists():
            ans = input(f"{dst.name} already exists. Overwrite? (y/N): ").strip().lower()
            if ans != "y":
                print(f"Skipped: {dst.name}")
                continue

        print(f"Download: {dst.name}")
        try:
            download_with_progress(url, dst, size_bytes, md5_digest)
            print(f"OK: {dst.name}")
        except Exception as e:
            print(f"ERROR for {dst.name}: {e}")
            input("Press Enter...")
            return

    print()
    print("Done.")
    input("Press Enter...")


def uninstall_menu() -> None:
    """
    Uninstall selected voices by deleting the .onnx and matching .onnx.json if present.
    """
    files = installed_onnx_files()
    if not files:
        print("No installed .onnx files found in the voices folder.")
        input("Press Enter...")
        return

    clear_screen()
    print("Uninstall")
    print("=========")
    print(f"Folder: {DEST_DIR}")
    print()

    pairs: list[tuple[Path, Path | None]] = []
    for onnx in files:
        cfg = onnx.with_suffix(onnx.suffix + ".json")  # .onnx.json
        pairs.append((onnx, cfg if cfg.exists() else None))

    for i, (onnx, cfg) in enumerate(pairs, start=1):
        print(f"{i:4d}) {onnx.name}" + (f"  +  {cfg.name}" if cfg else ""))

    print()
    sel = input("Select number (Enter = back): ").strip()
    if sel == "" or not sel.isdigit():
        return

    n = int(sel)
    if not (1 <= n <= len(pairs)):
        return

    onnx, cfg = pairs[n - 1]
    ans = input(
        f"Really delete {onnx.name}" + (f" and {cfg.name}" if cfg else "") + "? (y/N): "
    ).strip().lower()
    if ans != "y":
        return

    try:
        onnx.unlink(missing_ok=True)
        if cfg:
            cfg.unlink(missing_ok=True)
        print("Deleted.")
    except Exception as e:
        print(f"Error: {e}")

    input("Press Enter...")


# -----------------------------------------------------------------------------
# Main interactive loop
# -----------------------------------------------------------------------------
def main() -> None:
    """
    Main interactive menu.

    The catalog is fetched on demand and kept in memory for the session.
    """
    voices: dict[str, Any] | None = None

    while True:
        clear_screen()
        print("Piper Voice Manager")
        print(f"Script folder:  {SCRIPT_DIR}")
        print(f"Install folder: {DEST_DIR}")
        print()
        print("1) Fetch voices list (voices.json)")
        print("2) List available languages")
        print("3) Browse voices by language")
        print("4) Search voices (text filter)")
        print("5) List installed voices")
        print("6) Uninstall (delete .onnx and .onnx.json)")
        print("0) Exit")
        print()

        choice = input("Choice: ").strip()

        if choice == "0":
            return

        if choice == "1":
            clear_screen()
            print("Fetching voices.json ...")
            try:
                voices = fetch_voices()
                print(f"OK. Voices available: {len(voices)}")
            except urllib.error.URLError as e:
                print(f"Network error: {e}")
            except Exception as e:
                print(f"Error: {e}")
            input("Press Enter...")

        elif choice == "2":
            if voices is None:
                print("Please run option 1 first (fetch voices list).")
                input("Press Enter...")
                continue

            lang_items = build_language_index(voices)
            picked = choose_language(lang_items)
            if picked:
                clear_screen()
                fam = ""
                if picked["families"]:
                    fam = " families=" + ",".join(sorted(picked["families"]))
                print(f"{picked['label']} [{picked['code']}] voices={picked['count']}{fam}")
                print()
                input("Press Enter...")

        elif choice == "3":
            if voices is None:
                print("Please run option 1 first (fetch voices list).")
                input("Press Enter...")
                continue

            lang_items = build_language_index(voices)
            picked_lang = choose_language(lang_items)
            if not picked_lang:
                continue

            code = picked_lang["code"].lower().strip()
            items: list[tuple[str, dict[str, Any]]] = []

            for k, m in voices.items():
                lang = m.get("language", {})
                if str(lang.get("code", "")).lower().strip() == code:
                    items.append((k, m))

            items.sort(key=lambda x: (voice_display_name(x[1]).lower(), x[0].lower()))
            picked_voice = choose_from_list(items, f"Voices for: {picked_lang['label']} [{picked_lang['code']}]")
            if picked_voice:
                install_voice(picked_voice[0], picked_voice[1])

        elif choice == "4":
            if voices is None:
                print("Please run option 1 first (fetch voices list).")
                input("Press Enter...")
                continue

            q = input("Search text (voice name, key, language, quality): ").strip().lower()
            if not q:
                continue

            items: list[tuple[str, dict[str, Any]]] = []
            for k, m in voices.items():
                lang = m.get("language", {})
                hay = " ".join(
                    [
                        voice_display_name(m),
                        k,
                        str(m.get("quality", "")),
                        str(lang.get("code", "")),
                        str(lang.get("name_english", "")),
                        str(lang.get("country_english", "")),
                        str(lang.get("family", "")),
                    ]
                ).lower()
                if q in hay:
                    items.append((k, m))

            items.sort(key=lambda x: (voice_display_name(x[1]).lower(), x[0].lower()))
            picked = choose_from_list(items, f"Results for: {q}")
            if picked:
                install_voice(picked[0], picked[1])

        elif choice == "5":
            clear_screen()
            print("Installed (.onnx in voices folder):")
            print(f"{DEST_DIR}")
            print()

            files = installed_onnx_files()
            if not files:
                print("None.")
            else:
                for p in files:
                    cfg = p.with_suffix(p.suffix + ".json")  # .onnx.json
                    has_cfg = " + cfg" if cfg.exists() else ""
                    print(f"- {p.name}{has_cfg}")

            print()
            input("Press Enter...")

        elif choice == "6":
            uninstall_menu()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        # Allow clean exit on Ctrl+C without a stack trace.
        pass
