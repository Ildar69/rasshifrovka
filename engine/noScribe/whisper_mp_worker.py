import gc
import logging
import os
import platform
import sys
import traceback
from dataclasses import asdict, is_dataclass

if sys.version_info >= (3, 12):
    import importlib.resources as impres
else:
    import importlib_resources as impres

from i18n import t

logger = logging.getLogger(__name__)


def whisper_proc_entrypoint(args: dict, q):
    """
    Runs in a child process. Streams progress/logs to parent via `q`.
    Messages put on `q` are dicts with one of the following shapes:
      {"type": "log", "level": "info"|"warn"|"error"|"debug", "msg": "..."}
      {"type": "progress", "pct": float, "detail": "..."}   # optional
      {"type": "result", "ok": True, "segments": [...], "info": {...}}
      {"type": "result", "ok": False, "error": str, "trace": str}
    """
    try:
        # Import heavy libs only in the child
        from faster_whisper import WhisperModel
        from faster_whisper.audio import decode_audio
        from faster_whisper.vad import VadOptions
        import torch
        import yaml
        import i18n

        def plog(level, msg):
            try:
                q.put({"type": "log", "level": level, "msg": str(msg)})
            except Exception:
                pass

        # Initialize python-i18n in child process (PyInstaller uses spawn; no
        # globals shared).
        #
        # TODO: python-i18n is unmaintained for more than five years.
        #
        # See the main file for more information on python-i18n and the
        # approach. Here nothing should actually fail as any possible
        # exceptions were already handled/checked in the main app.
        i18n.set("filename_format", "{locale}.{format}")
        i18n.set("enable_memoization", True)
        i18n.set("fallback", "en")
        i18n.set("locale", args.get("locale", "en"))

        with impres.as_file(impres.files("trans")) as mypath:
            i18n.load_path.append(mypath)

            # Using `t` once here to load the localization files into memory.
            # As there is no `print`, nothing happens really.
            t("app_header")
        
        # determine device
        device = args.get("device", "")
        if device != 'cpu':
            if platform.system() == "Darwin":  # MAC
                device = 'auto'
            elif platform.system() in ('Windows', 'Linux'):
                try:
                    device = 'cuda' if torch.cuda.is_available() and torch.cuda.device_count() > 0 else 'cpu'
                except:
                    device = 'cpu'
            else:
                raise Exception('Platform not supported yet.')
            
        # Build model in child using provided options
        # Ildar: a model folder may carry its own compute type in compute_type.txt
        # (e.g. "int8" for the fast setting on CPU), so one model.bin serves both settings
        compute_type = args.get("compute_type", "float16")
        ct_file = os.path.join(str(args["whisper_model"].path), "compute_type.txt")
        if os.path.exists(ct_file):
            with open(ct_file, encoding="utf-8") as f:
                compute_type = f.read().strip() or compute_type
        # Ildar: on an NVIDIA card int8 weights are multiplied in float16 - the card's own type
        if device == 'cuda' and compute_type == 'int8':
            compute_type = 'int8_float16'
        plog("info", f"whisper device: {device}, compute type: {compute_type}")
        model = WhisperModel(
            str(args["whisper_model"].path),
            device=device,
            compute_type=compute_type,
            cpu_threads=args.get("cpu_threads", 4),
            local_files_only=args.get("local_files_only", True),
        )

        # Define callbacks that forward to parent via queue (not used by faster-whisper directly, but kept for parity)
        def log_cb(level, msg):
            plog(level, msg)

        # Prepare audio and VAD
        audio_path = args.get("audio_path")
        if not audio_path or not os.path.exists(audio_path):
            raise FileNotFoundError(f"Audio path does not exist: {audio_path}")

        log_cb("info", t('vad'))

        # VAD options
        vad_threshold = float(args.get("vad_threshold", 0.5))
        try:
            vad_parameters = VadOptions(min_silence_duration_ms=args.get("vad_min_silence_ms", 500), threshold=vad_threshold, speech_pad_ms=args.get("vad_speech_pad_ms", 50))
        except TypeError:
            vad_parameters = VadOptions(min_silence_duration_ms=args.get("vad_min_silence_ms", 500), onset=vad_threshold, speech_pad_ms=args.get("vad_speech_pad_ms", 50))

        # Language handling
        language_name = args.get("language_name")
        language_code = args.get("language_code")
        multilingual = False
        whisper_lang = None
        
        if not model.model.is_multilingual and language_code != 'en':
            language_name = 'English'
            language_code = 'en'
            log_cb("info", t('language_en_only'))
        
        if language_name == "Multilingual":
            multilingual = True
            whisper_lang = None
        elif language_name == "Auto":
            whisper_lang = None
        else:
            whisper_lang = language_code

        # Detect language if requested (Auto)
        if language_name == "Auto":
            audio = decode_audio(
                audio_path, sampling_rate=model.feature_extractor.sampling_rate
            )
            try:
                whisper_lang, language_probability, _ = model.detect_language(
                    audio, vad_filter=True, vad_parameters=vad_parameters
                )
            finally:
                del audio
                gc.collect()
            log_cb("info", t('language_detect', lang=whisper_lang, prob=f'{language_probability:.2f}'))

        # Build prompt/hotwords if disfluencies suppression is requested
        prompt = ""
        if args.get("disfluencies", False):
            prompt_file = impres.files("prompts") / "prompt.yml"
        else:
            prompt_file = impres.files("prompts") / "prompt_nd.yml"
        try:
            with prompt_file.open("r", encoding="utf-8") as f:
                prompt = yaml.safe_load(f).get(whisper_lang, "")
        except Exception as e:
            logger.exception(e)
            log_cb('error', t('err_loading_prompt') + '\n')
        # Ildar: the user's own names and terms (people, products, places) come from
        # outside the repository, so a public copy carries no one's private vocabulary
        extra = os.environ.get('RASSHIFROVKA_EXTRA_PROMPT', '').strip()
        if extra:
            prompt = f'{prompt} {extra}'.strip()

        # Pass the path so faster-whisper can release its original waveform
        # after VAD, before allocating the full spectrogram (see 28650f2e).
        segments, info = model.transcribe(
            audio_path,
            language=whisper_lang,
            multilingual=multilingual,
            beam_size=args.get("beam_size", 5),
            # temperature=args.get("temperature"),
            word_timestamps=args.get("word_timestamps", True),
            # Ildar: our terms work as initial_prompt (checked on our recordings since 06.10)
            initial_prompt=prompt if args.get("use_initial_prompt", False) else None,
            # Ildar: with initial_prompt on, hotwords repeat the same list and bias decoding
            hotwords=None if args.get("use_initial_prompt", False) else prompt,
            vad_filter=args.get("vad_filter", True),
            vad_parameters=vad_parameters,
        )
        
        log_cb('info', t('start_transcription') + '\n')
        
        # Stream segments to parent as they arrive
        for s in segments:
            try:
                seg_d = {
                    "start": getattr(s, "start", None),
                    "end": getattr(s, "end", None),
                    "text": getattr(s, "text", None),
                }
                words = getattr(s, "words", None)
                if words:
                    seg_d["words"] = [
                        {
                            "word": getattr(w, "word", None),
                            "start": getattr(w, "start", None),
                            "end": getattr(w, "end", None),
                            "prob": getattr(w, "probability", None),
                        }
                        for w in words
                    ]
                q.put({"type": "segment", "segment": seg_d})
            except Exception:
                # Best-effort; continue on serialization issues
                pass

        # info into dict
        if is_dataclass(info):
            info_dict = asdict(info)
        else:
            info_dict = {}
            for k in ("language", "language_probability", "duration", "sample_rate"):
                if hasattr(info, k):
                    info_dict[k] = getattr(info, k)

        try:
            q.put({"type": "result", "ok": True, "info": info_dict})
        except Exception:
            pass

        # Cleanup VRAM (harmless on CPU)
        try:
            del model
        except Exception:
            pass
        try:
            torch.cuda.empty_cache()
        except Exception:
            pass
        gc.collect()
        plog("debug", "Subprocess finished cleanly.")

    except Exception as e:
        try:
            q.put({
                "type": "result",
                "ok": False,
                "error": f"{type(e).__name__}: {e}",
                "trace": traceback.format_exc(),
            })
        except Exception:
            pass
