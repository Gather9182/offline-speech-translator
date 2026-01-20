#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

try:
    import sounddevice as sd  # pip install sounddevice
except Exception:
    sd = None

JsonType = Union[Dict[str, Any], List[Any], str, int, float, bool, None]

ANSI_DIM = "\x1b[90m"
ANSI_RESET = "\x1b[0m"
ANSI_BOLD = "\x1b[1m"
ANSI_GREEN = "\x1b[92m"


ANSI_YELLOW = "\x1b[93m"

def yellow_bold(s: str) -> str:
    return f"{ANSI_BOLD}{ANSI_YELLOW}{s}{ANSI_RESET}"


def enable_ansi_on_windows() -> None:
    if os.name == "nt":
        try:
            os.system("")
        except Exception:
            pass


def dim(s: str) -> str:
    return f"{ANSI_DIM}{s}{ANSI_RESET}"


def green_bold(s: str) -> str:
    return f"{ANSI_BOLD}{ANSI_GREEN}{s}{ANSI_RESET}"


def default_config_path() -> Path:
    """
    This file is expected at: <app-root>\\scripts\\config_editor.py
    The default config is expected at: <app-root>\\config.json
    """
    return Path(__file__).resolve().parents[1] / "config.json"


def load_config_path(argv: Sequence[str]) -> Path:
    # Priority:
    # 1) CLI argument (if provided)
    # 2) environment variable OEBB_CONFIG
    # 3) <app-root>\config.json (parent folder of \scripts\)
    if len(argv) >= 2 and argv[1].strip():
        return Path(argv[1]).expanduser()
    env = os.environ.get("OEBB_CONFIG")
    if env:
        return Path(env).expanduser()
    return default_config_path()


def read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("Config root must be a JSON object.")
    return data


def write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    tmp.replace(path)


def format_value(v: Any, max_len: int = 80) -> str:
    try:
        s = json.dumps(v, ensure_ascii=False)
    except Exception:
        s = str(v)
    if len(s) > max_len:
        s = s[: max_len - 3] + "..."
    return s


def parse_typed_input(raw: str, current: Any) -> Any:
    """
    Tries to match the current type:
    - int/float/bool/null: converts
    - string: raw
    - list/dict: expects JSON
    """
    r = raw.strip()

    # empty input keeps current value
    if r == "":
        return current

    # if current is dict/list, expect JSON input
    if isinstance(current, (dict, list)) or (current is None and (r.startswith("{") or r.startswith("["))):
        try:
            return json.loads(r)
        except Exception as e:
            raise ValueError(f"Invalid JSON: {e}")

    # bool
    if isinstance(current, bool):
        low = r.lower()
        if low in {"true", "1", "yes", "y"}:
            return True
        if low in {"false", "0", "no", "n"}:
            return False
        raise ValueError("Expected true/false (or 1/0).")

    # int
    if isinstance(current, int) and not isinstance(current, bool):
        try:
            return int(r)
        except Exception:
            raise ValueError("Expected an integer.")

    # float
    if isinstance(current, float):
        try:
            return float(r.replace(",", "."))
        except Exception:
            raise ValueError("Expected a number (float).")

    # None: try parse simple literals, else string
    if current is None:
        low = r.lower()
        if low in {"null", "none"}:
            return None
        if low in {"true", "false"}:
            return low == "true"
        try:
            if "." in r or "," in r:
                return float(r.replace(",", "."))
            return int(r)
        except Exception:
            return r

    # default string
    return r


class Validator:
    def __init__(self) -> None:
        self.allowed: Dict[Tuple[str, ...], set] = {
            ("postedit", "mode"): {"full", "fast"},
            ("app", "debug"): {1, 2},
            ("vad", "mode"): {0, 1, 2, 3},
            ("mt", "backend"): {"argos", "modernmt"},
            ("rag", "backend"): {"glossary", "postgres", "postgres_vector"},
        }

        self.help_text: Dict[Tuple[str, ...], str] = {
            ("vad", "mode"): (
                "VAD mode controls aggressiveness of speech detection (webrtcvad).\n"
                "Range: 0..3\n\n"
                "0 = least aggressive: detects more as speech (more noise), avoids cutting real speech.\n"
                "1 = slightly more aggressive: good all-round setting.\n"
                "2 = aggressive: filters background better, may miss quiet speech.\n"
                "3 = most aggressive: best against noise, most likely to cut quiet parts.\n\n"
                "Rule of thumb:\n"
                "- quiet office: 1–2\n"
                "- loud environment: 2–3 (consider increasing end_silence_ms if speech is cut)\n"
            ),
            ("postedit", "mode"): (
                "postedit.mode controls how much post-editing is applied and how much latency it adds.\n\n"
                "fast:\n"
                "- Only quick deterministic rules (regex/heuristics)\n"
                "- No LanguageTool / no Java grammar check\n"
                "- Pros: very fast, stable, no startup overhead\n"
                "- Cons: only fixes cases covered by rules\n\n"
                "full:\n"
                "- fast + LanguageTool grammar corrections\n"
                "- Applies suggestions while protecting glossary terms\n"
                "- Pros: better grammar/style fixes\n"
                "- Cons: slower, LanguageTool/Java startup overhead\n"
            ),
        }

    def validate(self, path: Tuple[str, ...], value: Any) -> Optional[str]:
        if path in self.allowed:
            allowed = self.allowed[path]
            if value not in allowed:
                return f"Invalid value. Allowed: {sorted(list(allowed))}"
        return None

    def hint(self, path: Tuple[str, ...]) -> Optional[str]:
        if path in self.allowed:
            return f"Allowed: {sorted(list(self.allowed[path]))}"
        return None

    def help(self, path: Tuple[str, ...]) -> Optional[str]:
        return self.help_text.get(path)



def prompt(msg: str) -> str:
    return input(msg)


def press_enter() -> None:
    input(dim("Press Enter to continue... "))


def clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def menu_header(title: str, path: Tuple[str, ...], cfg_path: Path) -> None:
    clear_screen()
    print(title)
    print(dim(f"Config: {cfg_path}"))
    if path:
        print(dim("Path: " + " > ".join(path)))
    print("")


def get_node(root: Dict[str, Any], path: Tuple[str, ...]) -> Any:
    node: Any = root
    for p in path:
        if not isinstance(node, dict) or p not in node:
            raise KeyError("Invalid path.")
        node = node[p]
    return node


def list_audio_devices() -> None:
    if sd is None:
        print("Audio device listing is not available (missing dependency).")
        print(dim("Install: pip install sounddevice"))
        return

    try:
        devices = sd.query_devices()
        default_in, default_out = sd.default.device
    except Exception as e:
        print(f"Failed to query audio devices: {e}")
        return

    inputs: List[str] = []
    outputs: List[str] = []

    for idx, d in enumerate(devices):
        name = d.get("name", "Unknown")
        max_in = int(d.get("max_input_channels", 0) or 0)
        max_out = int(d.get("max_output_channels", 0) or 0)

        line = f"{green_bold(f'[{idx:2d}]')} {name} {dim(f'(in:{max_in} out:{max_out})')}"
        if idx == default_in:
            line += " " + dim("[default input]")
        if idx == default_out:
            line += " " + dim("[default output]")

        if max_in > 0:
            inputs.append(line)
        if max_out > 0:
            outputs.append(line)

    print("Input devices:")
    if inputs:
        for l in inputs:
            print("  " + l)
    else:
        print(dim("  (none found)"))

    print("")
    print("Output devices:")
    if outputs:
        for l in outputs:
            print("  " + l)
    else:
        print(dim("  (none found)"))


def edit_scalar(
    key_path: Tuple[str, ...],
    current_value: Any,
    validator: Validator,
) -> Any:
    while True:
        hint = validator.hint(key_path)
        if hint:
            print(dim(hint))

        raw = prompt(f"New value (empty keeps current) [{format_value(current_value)}]: ")
        try:
            new_val = parse_typed_input(raw, current_value)
        except Exception as e:
            print(f"Error: {e}")
            continue

        err = validator.validate(key_path, new_val)
        if err:
            print(f"Error: {err}")
            continue

        return new_val


def edit_object_menu(
    cfg: Dict[str, Any],
    cfg_path: Path,
    path: Tuple[str, ...],
    validator: Validator,
) -> str:
    """
    Returns: "back" | "quit" | "stay"
    """
    node = get_node(cfg, path) if path else cfg
    if not isinstance(node, dict):
        raise ValueError("Expected dict at current path.")

    keys = sorted(node.keys())

    menu_header("Config Editor", path, cfg_path)

    print("Fields:")
    if not keys:
        print(dim("  (empty)"))
    else:
        for i, k in enumerate(keys, start=1):
            v = node[k]
            t = "object" if isinstance(v, dict) else "list" if isinstance(v, list) else type(v).__name__
            print(f"  {i:2d}) {k:20s} = {format_value(v)} {dim('[' + t + ']')}")

    print("")
    print("Actions:")
    print("  s) Save")
    print("  r) Reload from disk")
    if path == ("audio",):
        print("  " + yellow_bold("l) List audio devices"))
    print("  b) Back")
    print("  q) Quit")
    print("")

    raw = prompt("Select (number or action): ").strip().lower()

    if raw in {"q", "quit", "exit"}:
        return "quit"
    if raw in {"b", "back"}:
        return "back"

    if raw in {"s", "save"}:
        write_json(cfg_path, cfg)
        print("Saved.")
        press_enter()
        return "stay"

    if raw in {"r", "reload"}:
        new_cfg = read_json(cfg_path)
        cfg.clear()
        cfg.update(new_cfg)
        print("Reloaded.")
        press_enter()
        return "stay"

    if raw == "l" and path == ("audio",):
        menu_header("Audio Devices", path, cfg_path)
        list_audio_devices()
        press_enter()
        return "stay"

    # numeric selection
    try:
        idx = int(raw)
    except Exception:
        print("Invalid input.")
        press_enter()
        return "stay"

    if idx < 1 or idx > len(keys):
        print("Invalid selection.")
        press_enter()
        return "stay"

    selected_key = keys[idx - 1]
    selected_path = path + (selected_key,)
    val = node[selected_key]

    # nested object
    if isinstance(val, dict):
        return navigate(cfg, cfg_path, selected_path, validator)

    # list editing as JSON list
    if isinstance(val, list):
        menu_header("Edit List", selected_path, cfg_path)
        print("Current value:")
        print(json.dumps(val, indent=2, ensure_ascii=False))
        print("")
        print(dim("Input must be a JSON list. Empty keeps current."))
        raw_list = prompt("New value: ")
        if raw_list.strip() == "":
            return "stay"
        try:
            new_val = json.loads(raw_list)
            if not isinstance(new_val, list):
                print("Expected a JSON list.")
                press_enter()
                return "stay"
            err = validator.validate(selected_path, new_val)
            if err:
                print(f"Error: {err}")
                press_enter()
                return "stay"
            node[selected_key] = new_val
            print("Updated.")
            press_enter()
        except Exception as e:
            print(f"Error: {e}")
            press_enter()
        return "stay"

    while True:
        menu_header("Edit Value", selected_path, cfg_path)

        help_txt = validator.help(selected_path)
        if help_txt:
            print("Actions:")
            print("  h) Help")
            print("")

        if help_txt:
            raw2 = prompt("Press Enter to edit, or type 'h' for help: ").strip().lower()
            if raw2 == "h":
                menu_header("Help", selected_path, cfg_path)
                print(help_txt)
                press_enter()
                continue

        new_val = edit_scalar(selected_path, val, validator)
        node[selected_key] = new_val
        print("Updated.")
        press_enter()
        return "stay"



def navigate(
    cfg: Dict[str, Any],
    cfg_path: Path,
    start_path: Tuple[str, ...],
    validator: Validator,
) -> str:
    path = start_path
    while True:
        result = edit_object_menu(cfg, cfg_path, path, validator)
        if result == "back":
            return "stay"
        if result == "quit":
            return "quit"


def main(argv: List[str]) -> int:
    enable_ansi_on_windows()
    cfg_path = load_config_path(argv)

    try:
        cfg = read_json(cfg_path)
    except Exception as e:
        print(f"Failed to load config: {e}")
        print(dim(r"Tip: python scripts\config_editor.py C:\path\to\config.json"))
        return 2

    validator = Validator()

    root_path: Tuple[str, ...] = tuple()
    while True:
        res = edit_object_menu(cfg, cfg_path, root_path, validator)
        if res == "quit":
            return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv))
