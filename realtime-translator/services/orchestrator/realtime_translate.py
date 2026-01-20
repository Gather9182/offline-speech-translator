"""
realtime_translate.py

End-to-end prototype for near real-time speech translation (offline-first):

Audio In (microphone)
  -> VAD (webrtcvad) to segment speech
  -> STT (faster-whisper) to transcribe speech to text
  -> Glossary layer (placeholder protection/forced terms)
  -> MT (Argos by default, optionally ModernMT via HTTP)
  -> Glossary restore
  -> Post-edit (LanguageTool) to fix common grammar issues
  -> TTS (Piper) to synthesize translated text
Audio Out (speaker/headset)

Design goals:
- Offline development on Windows (microphone/speaker devices via sounddevice)
- Config-driven for later deployment (Azure-friendly)
- Modular translation backends (Argos now, ModernMT later)
- Terminology handling via glossary layer (RAG/term protection foundation)
"""

import json
import locale
import os
import queue
import re
import subprocess
import sys
import time
import wave
import psycopg
import numpy as np
import sounddevice as sd
import webrtcvad

from faster_whisper import WhisperModel
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict
from typing import List

from app.translation import Translator, TranslationConfig
from app.glossary import Term, load_terms, apply_glossary, restore_glossary
from app.postedit import PostEditor
from app.termstore_postgres_vector import PostgresVectorCfg, PostgresVectorTermStore


# -----------------------------------------------------------------------------
# STT post-processing
# -----------------------------------------------------------------------------
# Simple correction map to fix predictable ASR mis-hearings.
# This is intentionally small, deterministic and serves as a small Hot-fix (no ML).
# Extend as needed for railway-specific abbreviations, station names, etc.
STT_CORRECTIONS = {
    "upp": "ÖBB",
    "u p p": "ÖBB",
    "oebb": "ÖBB",
    "obb": "ÖBB",
}

ANSI_DIM = "\x1b[90m"    # dark gray
ANSI_RESET = "\x1b[0m"

def _enable_ansi_on_windows() -> None:
    """
    Enable ANSI escape sequence handling on Windows consoles.

    Some Windows console configurations do not interpret ANSI escape sequences
    (e.g., for colored or dim text) unless "virtual terminal processing" is
    enabled. Calling `os.system("")` is a common lightweight trick that enables
    ANSI processing in many Windows setups, especially in newer terminals.

    This function is safe to call multiple times and is a no-op on non-Windows
    platforms.

    Returns:
        None
    """
    if os.name == "nt":
        try:
            os.system("")
        except Exception:
            pass


def _debug_print(enabled: bool, msg: str) -> None:
    """
    Print a debug message in dim (dark gray) style, controlled by a flag.

    Uses ANSI escape sequences to render the message in a darker color
    to visually separate debug output from normal application logs.

    Args:
        enabled: If False, the function does nothing.
        msg: Debug message to print.

    Returns:
        None
    """
    if not enabled:
        return
    print(f"{ANSI_DIM}{msg}{ANSI_RESET}")



def normalize_stt_text(text: str) -> str:
    """
    Normalize and correct STT output.

    - Collapses repeated whitespace (STT sometimes emits odd spacing)
    - Applies case-insensitive word-boundary replacements for known mis-hearings

    Args:
        text: Raw transcription from STT.

    Returns:
        Cleaned text suitable for glossary + translation.
    """
    t = re.sub(r"\s+", " ", text).strip()
    for wrong, right in STT_CORRECTIONS.items():
        pattern = re.compile(r"\b" + re.escape(wrong) + r"\b", re.IGNORECASE)
        t = pattern.sub(right, t)
    return t

def load_terms_from_postgres(dsn: str, src_lang: str, tgt_lang: str) -> List[Term]:
    import psycopg  # lazy import, damit glossary-backend ohne psycopg laufen kann

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT src_lang, tgt_lang, src_term, tgt_term, mode, priority
                FROM public.glossary_terms
                WHERE src_lang = %s AND tgt_lang = %s
                """,
                (src_lang, tgt_lang),
            )
            rows = cur.fetchall()

    return [
        Term(
            src_lang=r[0],
            tgt_lang=r[1],
            src_term=r[2],
            tgt_term=r[3],
            mode=(r[4] or "").strip().lower(),
            priority=int(r[5] or 0),
        )
        for r in rows
    ]




# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
def load_config() -> Dict[str, Any]:
    """
    Load runtime configuration.

    Uses environment variable OEBB_CONFIG if set. Otherwise reads config.json
    located next to this script.

    Returns:
        Parsed configuration dictionary.

    Raises:
        FileNotFoundError: If the config file is missing.
        json.JSONDecodeError: If config JSON is invalid.
    """
    cfg_path = os.environ.get("OEBB_CONFIG")
    if cfg_path:
        path = Path(cfg_path)
    else:
        path = Path(__file__).with_name("config.json")

    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


# -----------------------------------------------------------------------------
# TTS helpers (Piper)
# -----------------------------------------------------------------------------
def piper_tts_to_wav(text: str, model_path: Path, out_wav: Path) -> None:
    """
    Synthesize text to a WAV file using Piper.

    Why file-based input:
      On Windows, piping Unicode via stdin can produce broken umlauts depending
      on codepage/console encoding. Writing a temporary text file using the
      system preferred encoding is the most reliable approach.

    Strategy:
      1) Prefer Piper's --input-file/--output-file mode (robust)
      2) Fallback to stdin mode if needed

    Args:
        text: Text to synthesize.
        model_path: Path to Piper ONNX voice model.
        out_wav: Output WAV file path.

    Raises:
        RuntimeError: If Piper fails in both modes.
    """
    enc = locale.getpreferredencoding(False)

    # Write TTS text to a sidecar file next to the wav. Keep file lifetime simple.
    tmp_txt = out_wav.with_suffix(".txt")
    tmp_txt.write_text(text + "\n", encoding=enc, errors="strict")

    # Preferred mode: explicit input/output files
    cmd1 = [
        "piper",
        "--model",
        str(model_path),
        "--input-file",
        str(tmp_txt),
        "--output-file",
        str(out_wav),
    ]
    p = subprocess.run(
        cmd1,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding=enc,
        errors="replace",
    )
    if p.returncode == 0:
        return

    # Fallback: stdin -> wav
    cmd2 = ["piper", "-m", str(model_path), "-f", str(out_wav)]
    p2 = subprocess.run(
        cmd2,
        input=text + "\n",
        text=True,
        encoding=enc,
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if p2.returncode != 0:
        raise RuntimeError((p.stderr or "") + "\n" + (p2.stderr or ""))


def play_wav(path: Path, device_index: int) -> None:
    """
    Play a WAV file using sounddevice.

    Piper typically produces 16-bit PCM WAV. We decode into float32 [-1, 1]
    for playback via sounddevice.

    Args:
        path: Path to WAV file.
        device_index: sounddevice output device index.

    Raises:
        RuntimeError: If the WAV format is unexpected.
    """
    with wave.open(str(path), "rb") as wf:
        channels = wf.getnchannels()
        sr = wf.getframerate()
        sampwidth = wf.getsampwidth()
        if sampwidth != 2:
            raise RuntimeError(f"Expected 16-bit WAV, got sampwidth={sampwidth}")
        data = wf.readframes(wf.getnframes())

    audio = np.frombuffer(data, dtype=np.int16)
    if channels > 1:
        audio = audio.reshape(-1, channels)
    audio_f32 = audio.astype(np.float32) / 32768.0

    sd.play(audio_f32, samplerate=sr, device=device_index)
    sd.wait()


# -----------------------------------------------------------------------------
# Audio capture structures
# -----------------------------------------------------------------------------
@dataclass
class AudioFrame:
    """
    A single chunk of audio captured from the microphone.

    pcm16: raw 16-bit PCM mono frame bytes (required by webrtcvad)
    timestamp: capture timestamp (not currently used, but useful for debugging)
    """
    pcm16: bytes
    timestamp: float


def int16_bytes_from_float32(x: np.ndarray) -> bytes:
    """
    Convert float32 audio samples in [-1, 1] to 16-bit PCM bytes.

    Args:
        x: Mono float32 numpy array.

    Returns:
        Byte string of int16 PCM values.
    """
    x = np.clip(x, -1.0, 1.0)
    return (x * 32767.0).astype(np.int16).tobytes()


# -----------------------------------------------------------------------------
# Main pipeline
# -----------------------------------------------------------------------------
def main() -> int:
    """
    Run the real-time translation loop.

    Pipeline:
      - audio input stream callback pushes frames into a Queue
      - main thread reads frames, performs VAD to decide segment boundaries
      - when segment ends, run STT -> glossary -> MT -> glossary restore -> post-edit -> TTS

    Returns:
        Process exit code (0 for clean shutdown).
    """
    print("")
    cfg = load_config()

    # --- App config ---
    _enable_ansi_on_windows()
    debug_enabled = bool(cfg.get("app", {}).get("debug", 0))
    
    # --- Audio config ---
    sample_rate = int(cfg["audio"]["sample_rate"])
    mic_device_index = int(cfg["audio"]["mic_device_index"])
    out_device_index = int(cfg["audio"]["out_device_index"])

    # --- STT config ---
    stt_model_name = cfg["stt"]["model"]
    stt_device = cfg["stt"]["device"]
    stt_compute_type = cfg["stt"]["compute_type"]
    stt_language = cfg["stt"]["language"]  # fixed language for deterministic STT

    # --- VAD config ---
    vad_mode = int(cfg["vad"]["mode"])  # 0..3 (3 = most aggressive)
    frame_ms = int(cfg["vad"]["frame_ms"])  # must be 10/20/30ms for webrtcvad
    min_speech_ms = int(cfg["vad"]["min_speech_ms"])  # minimum speech before transcribing
    end_silence_ms = int(cfg["vad"]["end_silence_ms"])  # silence to close segment

    # Derived constants
    blocksize = int(sample_rate * frame_ms / 1000)

    # --- Paths ---
    rag_cfg = cfg.get("rag", {})
    rag_backend = (rag_cfg.get("backend", "glossary") or "glossary").strip().lower()

    pg_dsn = rag_cfg.get("pg_dsn")
    top_k = int(rag_cfg.get("top_k", 50))
    term_store = None
    terms = None
    src_lang = cfg["mt"]["src_lang"]
    tgt_lang = cfg["mt"]["tgt_lang"]

    tts_voice_model = Path(cfg["tts"]["voice_model"])
    tts_tmp_wav = Path(cfg["tts"]["tmp_wav"])

    

    # Validate critical files early (fail fast)
    if not tts_voice_model.exists():
        raise FileNotFoundError(f"TTS voice model not found: {tts_voice_model}")


    # --- MT backend ---
    translator = Translator(
        TranslationConfig(
            backend=cfg["mt"].get("backend", "argos"),
            src_lang=cfg["mt"]["src_lang"],
            tgt_lang=cfg["mt"]["tgt_lang"],
            modernmt_url=cfg["mt"].get("modernmt_url", "http://127.0.0.1:8045/translate"),
        )
    )

    # Initialize post-editor once (LanguageTool startup is expensive)
    post_cfg = cfg.get("postedit", {})
    post_editor = PostEditor(
        lang=post_cfg.get("lang", "de-DE"),
        mode=post_cfg.get("mode", "fast"),
    )


    # Warmup (Load) glossary/postgres terms
   
    if rag_backend == "postgres_vector":
        if not pg_dsn:
            raise ValueError("[Warmup] RAG warmup failed: rag.backend=postgres_vector but rag.pg_dsn is missing")
        term_store = PostgresVectorTermStore(PostgresVectorCfg(dsn=pg_dsn, top_k=top_k))
        print(f"[Warmup] backend=postgres_vector, top_k={top_k}")

    elif rag_backend == "postgres":
        if not pg_dsn:
            raise ValueError("[Warmup] RAG warmup failed: rag.backend=postgres but rag.pg_dsn is missing")
        terms = load_terms_from_postgres(pg_dsn, src_lang, tgt_lang)
        print(f"[Warmup] backend=postgres, loaded {len(terms)} terms")

    else:
        terms_csv = Path(rag_cfg["terms_csv"])
        if not terms_csv.exists():
            raise FileNotFoundError(f"[Warmup] RAG warmup failed: Glossary CSV not found: {terms_csv}")
        terms = load_terms(terms_csv)
        print(f"[Warmup] backend=glossary, loaded {len(terms)} terms")

    # Load STT model once at startup (expensive).
    print("[Warmup] Loading Whisper model (first run downloads model if missing)...")
    model = WhisperModel(stt_model_name, device=stt_device, compute_type=stt_compute_type)
    
    print("[Warmup] Initializing components...")

    # Warmup MT (Argos/ModernMT)
    try:
        _ = translator.translate("warmup", cfg["mt"]["src_lang"], cfg["mt"]["tgt_lang"])
    except Exception as e:
        print(f"[Warmup] MT warmup failed: {e}")

    # Warmup LanguageTool
    try:
        _ = post_editor.correct("Das ist ein Warmup.", protected_terms=set(), inflectable_terms=set())
    except Exception as e:
        print(f"[Warmup] PostEdit warmup failed: {e}")

    # Warmup Piper (optional, costs some seconds but avoids first-run lag)
    try:
        piper_tts_to_wav("Warmup.", tts_voice_model, tts_tmp_wav)
    except Exception as e:
        print(f"[Warmup] TTS warmup failed: {e}")

    print("[Warmup] Done..")
    print("")

    
    
    print("Ready. Speak into the microphone. Ctrl+C to stop.")

    # Initialize VAD and audio frame queue
    vad = webrtcvad.Vad(vad_mode)
    q: "queue.Queue[AudioFrame]" = queue.Queue()

    def callback(indata, frames, time_info, status):
        """
        sounddevice InputStream callback.

        This must be fast and non-blocking:
          - Convert to int16 PCM bytes required by webrtcvad
          - Enqueue for processing in the main thread
        """
        if status:
            print(status, file=sys.stderr)

        mono = indata[:, 0].copy()
        pcm16 = int16_bytes_from_float32(mono)
        q.put(AudioFrame(pcm16=pcm16, timestamp=time.time()))

    # Segmentation state
    voiced_bytes = bytearray()
    speech_ms = 0
    silence_ms = 0
    in_speech = False

    with sd.InputStream(
        samplerate=sample_rate,
        channels=1,
        dtype="float32",
        blocksize=blocksize,
        device=mic_device_index,
        callback=callback,
    ):
        try:
            while True:
                frame = q.get()
                is_speech = vad.is_speech(frame.pcm16, sample_rate)

                if is_speech:
                    if not in_speech:
                        in_speech = True
                        voiced_bytes.clear()
                        speech_ms = 0
                        silence_ms = 0
                        print("\n[Speech started. App is listening...]")

                    voiced_bytes.extend(frame.pcm16)
                    speech_ms += frame_ms
                    silence_ms = 0
                else:
                    if in_speech:
                        silence_ms += frame_ms
                        voiced_bytes.extend(frame.pcm16)

                        if silence_ms >= end_silence_ms and speech_ms >= min_speech_ms:
                            audio_i16 = np.frombuffer(bytes(voiced_bytes), dtype=np.int16)
                            audio_f32 = audio_i16.astype(np.float32) / 32768.0

                            print("[Transcribing and translating...]")

                            # Total timer should include everything from STT until TTS wav is ready
                            t_total0 = time.perf_counter()

                            # STT
                            t_stt0 = time.perf_counter()
                            segments, _info = model.transcribe(
                                audio_f32,
                                language=stt_language,
                                vad_filter=False,
                                beam_size=1,
                                temperature=0.0,
                            )

                            text = "".join(seg.text for seg in segments).strip()
                            t_stt1 = time.perf_counter()
                            
                            text = normalize_stt_text(text)

                            if text:
                                print("[Transcribed output:]")
                                print(f"--[{stt_language}] {text}")

                                # Glossary apply (pre-MT)
                                t_gloss_apply0 = time.perf_counter()
                                
                                if rag_backend == "postgres_vector":
                                    terms = term_store.search(text, src_lang, tgt_lang)
                                    sample = ", ".join(f"{t.src_term} -> {t.tgt_term}" for t in terms[:3]) or "-"
                                    _debug_print(debug_enabled, f"[debug] postgres_vector candidates: {len(terms)} | sample: {sample}")

                                
                                protected, protect_map, force_map = apply_glossary(
                                    text, terms, cfg["mt"]["src_lang"], cfg["mt"]["tgt_lang"]
                                )
                                _debug_print(debug_enabled, f"[debug] protected: {protected}")
                                _debug_print(debug_enabled, f"[debug] protect_map: {protect_map}")
                                _debug_print(debug_enabled, f"[debug] force_map: {force_map}")
                                
                                t_gloss_apply1 = time.perf_counter()

                                # MT
                                t_mt0 = time.perf_counter()
                                translated = translator.translate(
                                    protected, cfg["mt"]["src_lang"], cfg["mt"]["tgt_lang"]
                                )
                                t_mt1 = time.perf_counter()

                                _debug_print(debug_enabled, f"[debug] mt_raw: {translated}")
                                
                                # Glossary restore (post-MT)
                                t_gloss_restore0 = time.perf_counter()
                                final_text = restore_glossary(translated, protect_map, force_map)
                                t_gloss_restore1 = time.perf_counter()

                                # protect: darf nicht verändert werden
                                protected_terms = set(protect_map.values())

                                # force: darf nur "sicher" flektiert werden
                                inflectable_terms = set(force_map.values())

                                # Post-edit (LanguageTool + Term-Policy + ggf. deterministische Genitiv-Heuristik)
                                t_pe0 = time.perf_counter()
                                final_text = post_editor.correct(
                                    final_text,
                                    protected_terms=protected_terms,
                                    inflectable_terms=inflectable_terms,
                                )
                                t_pe1 = time.perf_counter()

                                _debug_print(debug_enabled, f"[debug] protected_terms: {protected_terms}")
                                _debug_print(debug_enabled, f"[debug] inflectable_terms: {inflectable_terms}")
                                
                                print(f"--[{cfg['mt']['tgt_lang']}] {final_text}")

                                # TTS wav generation
                                t_tts0 = time.perf_counter()
                                piper_tts_to_wav(final_text, tts_voice_model, tts_tmp_wav)
                                t_tts1 = time.perf_counter()

                                # Totals
                                t_total1 = time.perf_counter()

                                stt_ms = (t_stt1 - t_stt0) * 1000.0
                                mt_ms = (t_mt1 - t_mt0) * 1000.0
                                pe_ms = (t_pe1 - t_pe0) * 1000.0
                                tts_ms = (t_tts1 - t_tts0) * 1000.0
                                gloss_ms = ((t_gloss_apply1 - t_gloss_apply0) + (t_gloss_restore1 - t_gloss_restore0)) * 1000.0
                                total_ms = (t_total1 - t_total0) * 1000.0

                                print(
                                    f"[Timing] STT: {stt_ms:.0f} ms | GLOSS: {gloss_ms:.0f} ms | MT: {mt_ms:.0f} ms | "
                                    f"POST: {pe_ms:.0f} ms | TTS: {tts_ms:.0f} ms | TOTAL(before play): {total_ms:.0f} ms"
                                )

                                print("[TTS] playing...")
                                play_wav(tts_tmp_wav, out_device_index)
                            else:
                                print("[no text]")


                            in_speech = False
                            voiced_bytes.clear()
                            speech_ms = 0
                            silence_ms = 0
                            print("[Speech ended]")

                if in_speech and speech_ms < min_speech_ms and silence_ms >= end_silence_ms:
                    in_speech = False
                    voiced_bytes.clear()
                    speech_ms = 0
                    silence_ms = 0

        except KeyboardInterrupt:
            # Ensure LanguageTool subprocess is stopped cleanly
            try:
                post_editor.close()
                if term_store is not None:
                    term_store.close()
            except Exception:
                pass

            print("\nStopping.")
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
