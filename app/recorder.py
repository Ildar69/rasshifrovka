# Запись разговора: микрофон или микрофон + звук компьютера (созвоны), и живой
# черновик текста по ходу записи.
#
# Звук компьютера — WASAPI loopback через soundcard: собеседник в WhatsApp,
# Zoom, Teams слышен в записи. Когда компьютер молчит, loopback не присылает
# данных — недостающее добивается тишиной, ведёт время микрофон.
# Живой текст — та же модель "fast" (turbo int8), куски по ~10 с, beam 1:
# черновик, итоговая расшифровка после "Стоп" делается движком целиком.

import queue
import threading
import time
from pathlib import Path

import numpy as np

RATE = 48000
BLOCK = 4800          # 0.1 s
LIVE_CHUNK_SEC = 10
LIVE_OVERLAP_SEC = 1


PART_SEC = 30          # every 30 s a finished file lands on disk


def keep_awake(on):
    """Laptop sleep would cut a recording: ask Windows to stay awake while recording."""
    try:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))
    except Exception:
        pass


class Recorder:
    """Records to disk as it goes, in 30-second FLAC parts.

    Ильдар 13.10: "запись голосового не должна ломаться". Nothing waits in memory
    until "Стоп": if the window crashes, the laptop sleeps or power goes off, all
    parts but the last few seconds are already on disk, and recover() glues them
    into a recording on the next start.
    """

    def __init__(self, session_dir, with_system):
        self.dir = Path(session_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.with_system = with_system
        self.recent = []            # last ~20 s, for the live draft only
        self.recent_start = 0.0
        self.total_blocks = 0
        self.lock = threading.Lock()
        self.stop_ev = threading.Event()
        self.q = queue.Queue()
        self.loop_buf = []
        self.loop_lock = threading.Lock()
        self.error = ''
        self.level = 0.0
        self.threads = []

    @property
    def seconds(self):
        return self.total_blocks * BLOCK / RATE

    def start(self):
        keep_awake(True)
        (self.dir / 'source.txt').write_text('both' if self.with_system else 'mic', encoding='utf-8')
        for fn in ([self._writer, self._mic] + ([self._loop] if self.with_system else [])):
            th = threading.Thread(target=fn, daemon=True)
            self.threads.append(th)
            th.start()

    def _writer(self):
        import soundfile as sf
        n, f, in_part = 0, None, 0
        try:
            while True:
                blk = self.q.get()
                if blk is None:
                    break
                if f is None:
                    n += 1
                    f = sf.SoundFile(str(self.dir / f'part_{n:05d}.flac'), 'w', RATE, 1, format='FLAC')
                    in_part = 0
                f.write(blk)
                in_part += blk.size
                if in_part >= PART_SEC * RATE:
                    f.close()
                    f = None
        except Exception as e:
            self.error = f'запись на диск: {e}'
        finally:
            if f is not None:
                f.close()

    def _mic(self):
        try:
            import soundcard as sc
            mic = sc.default_microphone()
            with mic.recorder(samplerate=RATE, channels=1, blocksize=BLOCK) as r:
                while not self.stop_ev.is_set():
                    m = r.record(numframes=BLOCK)[:, 0].astype(np.float32)
                    if self.with_system:
                        with self.loop_lock:
                            buf = np.concatenate(self.loop_buf) if self.loop_buf else np.zeros(0, np.float32)
                            take, rest = buf[:BLOCK], buf[BLOCK:]
                            self.loop_buf = [rest] if rest.size else []
                        if take.size < BLOCK:
                            take = np.pad(take, (0, BLOCK - take.size))
                        m = np.clip(m + take, -1.0, 1.0)
                    self.level = float(np.sqrt(np.mean(m * m)))
                    self.q.put(m)
                    with self.lock:
                        self.total_blocks += 1
                        self.recent.append(m)
                        if len(self.recent) > 200:      # keep ~20 s
                            self.recent.pop(0)
                            self.recent_start += BLOCK / RATE
        except Exception as e:  # microphone unplugged, device taken by another app
            self.error = f'микрофон отключился ({e}); всё записанное до этого сохранено'
            self.stop_ev.set()

    def _loop(self):
        try:
            import soundcard as sc
            spk = sc.default_speaker()
            lb = sc.get_microphone(id=str(spk.name), include_loopback=True)
            with lb.recorder(samplerate=RATE, channels=2, blocksize=BLOCK) as r:
                while not self.stop_ev.is_set():
                    d = r.record(numframes=BLOCK)
                    mono = d.mean(axis=1).astype(np.float32)
                    with self.loop_lock:
                        self.loop_buf.append(mono)
                        total = sum(b.size for b in self.loop_buf)
                        while total > RATE * 2 and self.loop_buf:
                            total -= self.loop_buf.pop(0).size
        except Exception as e:
            self.error = f'звук компьютера недоступен ({e}); микрофон пишется дальше'

    def snapshot(self, from_sec):
        with self.lock:
            skip = max(0, int((from_sec - self.recent_start) * RATE / BLOCK))
            blocks = self.recent[skip:]
        return np.concatenate(blocks) if blocks else np.zeros(0, np.float32)

    def stop(self, path):
        self.stop_ev.set()
        for th in self.threads[1:]:
            th.join(timeout=3)
        self.q.put(None)
        self.threads[0].join(timeout=10)
        keep_awake(False)
        return join_parts(self.dir, path)


def join_parts(session_dir, path):
    """Glue the parts into one FLAC; a part cut off by a crash is read as far as it goes."""
    import soundfile as sf
    session_dir, path = Path(session_dir), Path(path)
    parts = sorted(session_dir.glob('part_*.flac'))
    total = 0
    with sf.SoundFile(str(path), 'w', RATE, 1, format='FLAC') as out:
        for p in parts:
            try:
                with sf.SoundFile(str(p)) as f:
                    while True:
                        d = f.read(RATE * 10, dtype='float32')
                        if not len(d):
                            break
                        out.write(d)
                        total += len(d)
            except Exception:
                total += _salvage(p, out)
    if total < RATE:  # under a second
        path.unlink(missing_ok=True)
        return None
    return path


def _salvage(part, out):
    """Read a damaged part with PyAV, which decodes up to the break."""
    n = 0
    try:
        import av
        with av.open(str(part)) as c:
            res = av.AudioResampler(format='flt', layout='mono', rate=RATE)
            for fr in c.decode(audio=0):
                for g in res.resample(fr):
                    a = g.to_ndarray().reshape(-1).astype(np.float32)
                    out.write(a)
                    n += a.size
    except Exception:
        pass
    return n


def unfinished_sessions(rec_root):
    """Recording folders left by a crash: they have parts but no "Стоп"."""
    root = Path(rec_root)
    if not root.is_dir():
        return []
    return [d for d in sorted(root.iterdir()) if d.is_dir() and any(d.glob('part_*.flac'))]


class LiveText:
    """Draft text while recording; runs in its own thread so the window never freezes."""

    def __init__(self, model_dir, language, on_text):
        self.model_dir = str(model_dir)
        self.language = None if language in ('auto', 'multilingual') else language
        self.on_text = on_text
        self.q = queue.Queue()
        self.model = None
        self.th = threading.Thread(target=self._run, daemon=True)
        self.th.start()

    def feed(self, audio_48k):
        self.q.put(audio_48k)

    def close(self):
        self.q.put(None)

    def _run(self):
        try:
            from faster_whisper import WhisperModel
            self.model = WhisperModel(self.model_dir, device='cpu', compute_type='int8',
                                      cpu_threads=2, local_files_only=True)
        except Exception as e:
            self.on_text(None, f'черновик недоступен: {e}')
            return
        while True:
            a = self.q.get()
            if a is None:
                return
            # newest chunk only: skip a backlog rather than fall behind the speaker
            while not self.q.empty():
                nxt = self.q.get()
                if nxt is None:
                    return
                a = nxt
            try:
                a16 = a[::3].astype(np.float32)  # 48 kHz -> 16 kHz
                segs, _ = self.model.transcribe(a16, language=self.language, beam_size=1,
                                                vad_filter=True, condition_on_previous_text=False)
                txt = ' '.join(s.text.strip() for s in segs).strip()
                if txt:
                    self.on_text(txt, '')
            except Exception as e:
                self.on_text(None, f'черновик: {e}')
