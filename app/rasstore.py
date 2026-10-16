# Хранилище расшифровок: одно на окно программы и на агента.
#
# Ильдар 13.10.2026: "одно хранилище общее — голосовые падают в Загрузки,
# расшифровки в отдельную папку, с историей: дата, время, название".
# Каждая расшифровка — папка в STORE:
#   2026-10-13 16-35 Голосовое от коллеги/
#     audio.<ext>       копия записи — плеер работает, даже если оригинал удалён
#     transcript.html   результат noScribe (его же открывает редактор noScribe)
#     transcript.md     для чтения и для агента
#     meta.json         название, даты, длительность, модель, статус, имена говорящих
# Хранилище — в Документах, не в OneDrive: записи не должны уходить с ноутбука.

import datetime as dt
import html
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

STORE = Path(os.environ.get('RASSHIFROVKA_STORE', Path.home() / 'Documents' / 'Расшифровки'))

# The engine (noScribe) runs either as a built exe or from source next to the app:
#   repo/app/rasstore.py + repo/engine/noScribe  — after install.ps1, any computer
#   RASSHIFROVKA_ENGINE=<folder with noScribe.exe> — a build of your own
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
_ENGINE_ENV = os.environ.get('RASSHIFROVKA_ENGINE', '')
_LEGACY_EXE_DIR = Path.home() / 'Tools' / 'noScribe' / 'pyinstaller' / 'dist' / 'noScribe'
if _ENGINE_ENV:
    NOSCRIBE_DIR = Path(_ENGINE_ENV)
elif (_ROOT / 'engine' / 'noScribe' / '__main__.py').exists():
    NOSCRIBE_DIR = _ROOT / 'engine'
else:
    NOSCRIBE_DIR = _LEGACY_EXE_DIR
NOSCRIBE_EXE = NOSCRIBE_DIR / 'noScribe.exe'
FROM_SOURCE = not NOSCRIBE_EXE.exists() and (NOSCRIBE_DIR / 'noScribe' / '__main__.py').exists()
if FROM_SOURCE:
    MODELS_DIR = NOSCRIBE_DIR / 'models'
    EDITOR_SCRIPT = _ROOT / 'editor' / 'noScribeEdit.py'
    EDITOR_EXE = EDITOR_SCRIPT
else:
    MODELS_DIR = NOSCRIBE_DIR / '_internal' / 'models'
    EDITOR_SCRIPT = None
    EDITOR_EXE = NOSCRIBE_DIR / '_internal' / 'noScribeEdit' / 'noScribeEdit.exe'
PROMPT_FILE = STORE / 'словарь.txt'   # names and terms of your own work: one line, comma-separated
DOWNLOADS = Path.home() / 'Downloads'

AUDIO_EXT = {'.mp3', '.m4a', '.mp4', '.ogg', '.opus', '.wav', '.webm', '.aac', '.mpeg', '.mpga', '.flac', '.amr', '.3gp', '.wma'}

LANGS = {'ru': 'Русский', 'kk': 'Казахский', 'auto': 'Определить сам', 'multilingual': 'Русский и казахский вперемешку'}
MODEL_NAMES = {'fast': 'Быстрая', 'precise': 'Точная'}
STATUS_NAMES = {'queued': 'В очереди', 'running': 'Расшифровывается', 'done': 'Готово', 'error': 'Ошибка'}
# сколько секунд работы на секунду записи на ноутбуке без видеокарты (i3-1315U, 8 ГБ), замер 13.10.2026
SPEED = {'fast': 1.6, 'precise': 3.0}
# без разделения по голосам — примерно вдвое быстрее: замер 15.10.2026, 1,2–1,3 против 2,9 на коротких записях
NO_SPEAKERS_FACTOR = 0.5

OFFLINE_ENV = {'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
               'HF_HUB_DISABLE_TELEMETRY': '1', 'PYANNOTE_METRICS_ENABLED': '0'}

LOCK = STORE / '.engine.lock'
LOCK_STALE_SEC = 90
CANCEL = STORE / '.cancel'
# noScribe names its log after the transcript file; ours is always transcript.html
ENGINE_LOG = Path(os.environ.get('LOCALAPPDATA', '')) / 'noScribe' / 'noScribe' / 'log' / 'transcript.log'
# a run cut off by a crash is started again by itself; after this many tries it stops with an error
MAX_ATTEMPTS = 3
STAGE_PROGRESS = re.compile(r'(segmentation|embeddings): (\d+)%')

ANCHOR = re.compile(r'<a name="ts_(\d+)_(\d+)_([^"]*)"\s*>(.*?)</a>', re.S)
TAG = re.compile(r'<[^>]+>')
PAUSE = re.compile(r'^\(\.+\)$|^\(\d+ секунд[а-я]* пауз[а-я]*\)$')


def models():
    """Model folders noScribe will accept: the built-in ones plus custom ones."""
    out = []
    for base in (MODELS_DIR, Path(os.environ.get('LOCALAPPDATA', '')) / 'noScribe' / 'noScribe' / 'whisper_models'):
        if base.is_dir():
            for d in sorted(base.iterdir()):
                if (d / 'model.bin').exists() and d.name not in out:
                    out.append(d.name)
    return out or ['fast', 'precise']


def model_title(name):
    return MODEL_NAMES.get(name, name)


def safe_name(s):
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', ' ', s).strip().rstrip('.')
    return re.sub(r'\s+', ' ', s)[:80] or 'Запись'


def audio_duration(path):
    try:
        import av
        with av.open(str(path)) as c:
            if c.duration:
                return c.duration / 1_000_000
            s = c.streams.audio[0]
            return float(s.duration * s.time_base) if s.duration else 0.0
    except Exception:
        return 0.0


def fmt_dur(sec):
    sec = int(round(sec or 0))
    h, sec = divmod(sec, 3600)
    m, s = divmod(sec, 60)
    return f'{h}:{m:02d}:{s:02d}' if h else f'{m}:{s:02d}'


class Item:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.meta = {}
        self.reload()

    def reload(self):
        self._paras = None
        try:
            self.meta = json.loads((self.folder / 'meta.json').read_text(encoding='utf-8'))
        except Exception:
            self.meta = {'title': self.folder.name, 'status': 'error', 'error': 'meta.json не читается'}

    def save(self):
        tmp = self.folder / 'meta.json.tmp'
        tmp.write_text(json.dumps(self.meta, ensure_ascii=False, indent=1), encoding='utf-8')
        os.replace(tmp, self.folder / 'meta.json')

    @property
    def id(self):
        return self.folder.name

    @property
    def title(self):
        return self.meta.get('title') or self.folder.name

    @property
    def created(self):
        try:
            return dt.datetime.fromisoformat(self.meta.get('created'))
        except Exception:
            return dt.datetime.fromtimestamp(self.folder.stat().st_mtime)

    @property
    def status(self):
        return self.meta.get('status', 'queued')

    @property
    def audio(self):
        a = self.meta.get('audio')
        return self.folder / a if a else None

    @property
    def html_path(self):
        return self.folder / 'transcript.html'

    @property
    def md_path(self):
        return self.folder / 'transcript.md'

    def speaker_name(self, spk):
        named = self.meta.get('speaker_names', {}).get(spk)
        if named:
            return named
        if spk.startswith('S') and spk[1:].isdigit():
            order = self.speakers()
            return f'Говорящий {order.index(spk) + 1}' if spk in order else spk
        return spk

    def paragraphs(self):
        """Consecutive segments of one speaker glued into a paragraph: [(start_ms, end_ms, spk, text)]."""
        if getattr(self, '_paras', None) is not None:
            return self._paras
        if not self.html_path.exists():
            return []
        out = []
        for start, end, spk, body in ANCHOR.findall(self.html_path.read_text(encoding='utf-8', errors='replace')):
            text = html.unescape(TAG.sub('', body)).strip()
            spk = html.unescape(spk).strip() or '—'
            if text.startswith(spk + ':'):
                text = text[len(spk) + 1:].strip()
            if not text or PAUSE.match(text):
                continue
            start, end = int(start), int(end)
            if out and out[-1][2] == spk:
                s0, _, _, t0 = out[-1]
                out[-1] = (s0, end, spk, t0 + ' ' + text)
            else:
                out.append((start, end, spk, text))
        self._paras = out
        return out

    def speakers(self):
        seen = []
        for _, _, spk, _ in self.paragraphs():
            if spk not in seen and spk != '—':
                seen.append(spk)
        return seen

    def write_md(self):
        lines = [f'# {self.title}', '',
                 f'{self.created:%d.%m.%Y %H:%M} · {fmt_dur(self.meta.get("duration"))} · '
                 f'модель {model_title(self.meta.get("model"))} · {LANGS.get(self.meta.get("language"), "")}', '']
        for start, _end, spk, text in self.paragraphs():
            who = self.speaker_name(spk)
            lines.append(f'[{fmt_dur(start / 1000)}] ' + (f'**{who}:** ' if who not in ('—', '') else '') + text)
            lines.append('')
        self.md_path.write_text('\n'.join(lines), encoding='utf-8')

    def plain_text(self):
        out = []
        for s, _e, spk, t in self.paragraphs():
            who = self.speaker_name(spk)
            out.append(f'[{fmt_dur(s / 1000)}] ' + (f'{who}: ' if who not in ('—', '') else '') + t)
        return '\n\n'.join(out)


def items():
    if not STORE.is_dir():
        return []
    out = [Item(d) for d in STORE.iterdir() if d.is_dir() and (d / 'meta.json').exists()]
    return sorted(out, key=lambda i: i.created, reverse=True)


def create_item(src, title=None, model='fast', language='ru', speakers='auto', origin='app', created=None):
    src = Path(src)
    STORE.mkdir(parents=True, exist_ok=True)
    created = created or dt.datetime.now()
    title = title or src.stem
    base = f'{created:%Y-%m-%d %H-%M} {safe_name(title)}'
    folder, n = STORE / base, 2
    while folder.exists():
        folder, n = STORE / f'{base} ({n})', n + 1
    folder.mkdir(parents=True)
    audio_name = 'audio' + src.suffix.lower()
    shutil.copy2(src, folder / audio_name)
    meta = {'title': title, 'created': created.isoformat(timespec='seconds'),
            'source': str(src), 'source_size': src.stat().st_size, 'audio': audio_name,
            'duration': audio_duration(folder / audio_name), 'model': model, 'language': language,
            'speakers': speakers, 'status': 'queued', 'origin': origin, 'speaker_names': {}}
    (folder / 'meta.json').write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding='utf-8')
    return Item(folder)


def known_sources():
    out = set()
    for it in items():
        s = it.meta.get('source')
        if s:
            out.add((os.path.basename(s).lower(), it.meta.get('source_size')))
    return out


def new_in_downloads(days=14):
    """Audio files in Downloads that are not in the store yet, newest first."""
    if not DOWNLOADS.is_dir():
        return []
    known = known_sources()
    limit = time.time() - days * 86400
    out = []
    for p in DOWNLOADS.iterdir():
        try:
            if p.is_file() and p.suffix.lower() in AUDIO_EXT and p.stat().st_mtime >= limit:
                if (p.name.lower(), p.stat().st_size) not in known:
                    out.append(p)
        except OSError:
            pass
    return sorted(out, key=lambda p: p.stat().st_mtime, reverse=True)


# --- one engine at a time: the laptop has 8 GB, two noScribe runs would not fit ---

def lock_busy():
    try:
        return time.time() - LOCK.stat().st_mtime < LOCK_STALE_SEC
    except OSError:
        return False


def lock_take(owner):
    STORE.mkdir(parents=True, exist_ok=True)
    if lock_busy():
        return False
    LOCK.write_text(f'{owner} {os.getpid()} {dt.datetime.now():%H:%M:%S}', encoding='utf-8')
    return True


def lock_touch():
    try:
        os.utime(LOCK, None)
    except OSError:
        pass


def lock_release():
    try:
        LOCK.unlink()
    except OSError:
        pass


def engine_command(item, folder=None):
    m = item.meta
    if FROM_SOURCE:
        import sys
        exe = Path(sys.executable)
        if exe.name.lower() == 'pythonw.exe':  # the window runs windowless; the engine needs python.exe
            exe = exe.with_name('python.exe')
        head = [str(exe), '-m', 'noScribe']
    else:
        head = [str(NOSCRIBE_EXE)]
    folder = folder or item.folder
    return head + [str(folder / item.audio.name), str(folder / item.html_path.name), '--no-gui',
            '--language', m.get('language', 'ru'), '--model', m.get('model', 'fast'),
            '--speaker-detection', str(m.get('speakers', 'auto'))]


# The engine prints the file paths to its stdout, and the frozen exe writes stdout in the ANSI
# code page (cp1251 here), ignoring PYTHONUTF8. A path with a letter outside it dies with
# 'charmap' codec can't encode, exit 1 before any work: Kazakh Ә Қ Ң Ү Ө Ғ Ұ І Һ, and "й"
# from an iPhone, which comes as и + U+0306 (checked 15.10.2026). Such an item is handed to
# the engine through a junction with a Latin name; the files still land in the item folder.
# Not in AppData: the Store Python redirects writes there into its own package folder,
# and the engine would not see the link.
RUN_LINK = Path.home() / '.rasshifrovka' / 'run'


def _ansi_encoding():
    import locale
    try:
        return locale.getencoding()  # 3.11+: the ANSI code page even in UTF-8 mode
    except AttributeError:
        return locale.getpreferredencoding(False)


def _engine_can_print(path):
    try:
        str(path).encode(_ansi_encoding())
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def _drop_run_link():
    try:
        os.rmdir(RUN_LINK)  # removes the junction itself, never what it points to
    except OSError:
        pass


def engine_folder(item):
    """The folder as the engine should see it: the item folder, or RUN_LINK pointing to it."""
    if os.name != 'nt' or _engine_can_print(item.html_path):
        return item.folder
    try:
        import _winapi
        RUN_LINK.parent.mkdir(parents=True, exist_ok=True)
        _drop_run_link()
        _winapi.CreateJunction(str(item.folder), str(RUN_LINK))
    except (OSError, ImportError, AttributeError):
        return item.folder
    return RUN_LINK if _engine_can_print(RUN_LINK) else item.folder


def extra_prompt():
    try:
        return ' '.join(PROMPT_FILE.read_text(encoding='utf-8').split())
    except OSError:
        return ''


def engine_env():
    return dict(os.environ, **OFFLINE_ENV, RASSHIFROVKA_EXTRA_PROMPT=extra_prompt())


def engine_ready():
    return NOSCRIBE_EXE.exists() or FROM_SOURCE


def editor_command(html_path):
    if EDITOR_SCRIPT is not None:
        import sys
        exe = Path(sys.executable).with_name('pythonw.exe')
        return str(exe if exe.exists() else sys.executable), [str(EDITOR_SCRIPT), str(html_path)]
    return str(EDITOR_EXE), [str(html_path)]


# --- the worker: a process of its own that runs the queue ---
#
# Ildar 14.10.2026: a 1:18 meeting died at minute 8 because the engine was a child of
# the agent's session and went down with it; the window showed it running for an hour.
# So the engine belongs to nobody: the window and the agent only put a recording in the
# queue and make sure the worker is alive. The worker outlives both, keeps the laptop
# awake while it works, and a run that died anyway is started again by itself.

def started_ts(item):
    try:
        return dt.datetime.fromisoformat(item.meta.get('started')).timestamp()
    except Exception:
        return 0.0


def recover_stale():
    """Runs marked "running" while nobody holds the engine died: back into the queue."""
    if lock_busy():
        return
    for it in items():
        if it.status == 'running':
            requeue_dead(it)


def requeue_dead(it):
    tries = it.meta.get('attempts', 1)
    if tries >= MAX_ATTEMPTS:
        it.meta.update(status='error', error=f'Расшифровка прерывалась {tries} раза подряд. '
                                              'Нажмите "Расшифровать заново"; если повторится — запись, возможно, повреждена.')
    else:
        it.meta.update(status='queued', error='', interrupted=dt.datetime.now().isoformat(timespec='seconds'))
    it.save()


def queued():
    return sorted((i for i in items() if i.status == 'queued'), key=lambda i: i.created)


def worker_command():
    import sys
    if getattr(sys, 'frozen', False):
        return [sys.executable, '--worker']
    exe = _HERE / 'dist' / 'Rasshifrovka' / 'Rasshifrovka.exe'
    if exe.exists():
        return [str(exe), '--worker']
    py = Path(sys.executable)
    if py.with_name('pythonw.exe').exists():
        py = py.with_name('pythonw.exe')
    return [str(py), str(Path(__file__).resolve()), '--worker']


def ensure_worker():
    """Start the worker if there is work and nobody is doing it. Returns True if one was started."""
    recover_stale()
    if lock_busy() or not queued():
        return False
    # the lock is taken before the start: the window asks every 1.5 s, the worker needs a few
    # seconds to come up, and a second worker must not be started meanwhile
    if not lock_take('starting'):
        return False
    try:
        spawn_detached(worker_command())
    except Exception:
        lock_release()
        raise
    return True


def spawn_detached(cmd):
    """A process that survives whoever started it. Windows itself (WMI) starts it, so it is
    nobody's child: closing the agent's session kills the whole tree under it (checked
    14.10.2026 — a detached child with CREATE_BREAKAWAY_FROM_JOB died with its parent's task),
    a WMI-started process is not in that tree. Plain detached start is the fallback."""
    if os.name != 'nt':
        subprocess.Popen(cmd, start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
        return
    try:
        _spawn_wmi(cmd)
        return
    except Exception:
        pass
    DETACHED, NEW_GROUP = 0x00000008, 0x00000200
    subprocess.Popen(cmd, creationflags=DETACHED | NEW_GROUP, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _spawn_wmi(cmd):
    import base64
    line = subprocess.list2cmdline(cmd).replace("'", "''")
    ps = (f"$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
          f"-Arguments @{{CommandLine='{line}'; CurrentDirectory='{str(_HERE)}'}}; "
          f"Write-Output \"$($r.ReturnValue) $($r.ProcessId)\"")
    r = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-EncodedCommand',
                        base64.b64encode(ps.encode('utf-16-le')).decode()],
                       creationflags=0x08000000, timeout=60, capture_output=True, text=True)
    code, _, pid = (r.stdout or '').strip().partition(' ')
    if code != '0':
        raise OSError(f'WMI не запустил процесс: {r.stdout.strip()} {r.stderr.strip()[:200]}')
    return int(pid)


def keep_awake(on):
    if os.name != 'nt':
        return
    try:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))
    except Exception:
        pass


def request_cancel(iid, wait_sec=20):
    """Stop the engine on this item (before deleting it); waits until the worker lets go of its files."""
    CANCEL.write_text(iid, encoding='utf-8')
    t_end = time.time() + wait_sec
    while time.time() < t_end:
        it = Item(STORE / iid)
        if it.status != 'running' or not lock_busy():
            break
        time.sleep(0.5)
    try:
        CANCEL.unlink()
    except OSError:
        pass


def _cancel_requested(it):
    try:
        return CANCEL.read_text(encoding='utf-8').strip() == it.id
    except OSError:
        return False


def _lock_owner():
    try:
        return LOCK.read_text(encoding='utf-8').split()[0]
    except (OSError, IndexError):
        return ''


def run_worker():
    if lock_busy() and _lock_owner() == 'starting':
        LOCK.write_text(f'worker {os.getpid()} {dt.datetime.now():%H:%M:%S}', encoding='utf-8')
    elif not lock_take('worker'):
        return
    keep_awake(True)
    try:
        for it in items():  # we hold the engine now, so anything still "running" is dead
            if it.status == 'running':
                requeue_dead(it)
        while True:
            q = queued()
            if not q:
                break
            run_item(q[0])
    finally:
        keep_awake(False)
        lock_release()


def run_item(item):
    item.meta.update(status='running', started=dt.datetime.now().isoformat(timespec='seconds'), error='',
                     attempts=item.meta.get('attempts', 0) + 1, worker_pid=os.getpid())
    item.save()
    if not engine_ready():
        item.meta.update(status='error', error=f'не найден движок расшифровки в {NOSCRIBE_DIR} — запустите install.ps1')
        item.save()
        return
    t0 = time.time()
    try:  # a new run starts from scratch; a fresh file also tells when the text stage began
        item.html_path.unlink()
    except OSError:
        pass
    flags = 0x08000000 if os.name == 'nt' else 0  # CREATE_NO_WINDOW
    folder = engine_folder(item)
    try:
        p = subprocess.Popen(engine_command(item, folder), cwd=str(NOSCRIBE_DIR), env=engine_env(),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
        canceled = False
        while p.poll() is None:
            lock_touch()
            if not (item.folder / 'meta.json').exists() or _cancel_requested(item):
                p.kill()
                p.wait()
                canceled = True
                break
            time.sleep(2)
    finally:
        if folder != item.folder:
            _drop_run_link()
    if not (item.folder / 'meta.json').exists():
        return
    item.reload()
    if canceled:
        item.meta.update(status='error', error='Остановлено')
    elif p.returncode == 0 and item.html_path.exists():
        item.meta.update(status='done', took=round(time.time() - t0), error='', attempts=0)
        item.save()
        item.write_md()
        return
    else:
        item.meta.update(status='error', error=f'Движок остановился (код {p.returncode}). Попробуйте ещё раз; '
                                               'если повторится — запись, возможно, повреждена.')
    item.save()


def progress(item):
    """Where the engine really is: (stage, 0..1). Speakers come first (noScribe log), then the
    text (the transcript file noScribe rewrites every few seconds)."""
    if item.status != 'running':
        return '', 0.0
    t0 = started_ts(item)
    dur = item.meta.get('duration') or 0
    speakers = str(item.meta.get('speakers', 'auto')) != 'none'
    text_from = 0.5 if speakers else 0.05
    try:
        if item.html_path.stat().st_mtime >= t0 and dur:
            paras = item.paragraphs()
            if paras:
                return 'Распознаю речь', min(0.99, text_from + (paras[-1][1] / 1000 / dur) * (0.99 - text_from))
    except OSError:
        pass
    if speakers:
        try:
            if ENGINE_LOG.stat().st_mtime >= t0:
                seg = emb = 0
                for name, pct in STAGE_PROGRESS.findall(ENGINE_LOG.read_text(encoding='utf-8', errors='replace')):
                    if name == 'segmentation':
                        seg = int(pct)
                    else:
                        emb = int(pct)
                if seg or emb:
                    return 'Разбираю, кто говорит', 0.05 + 0.45 * (0.3 * seg + 0.7 * emb) / 100
        except OSError:
            pass
    return 'Готовлю звук', 0.02


def time_left(item):
    """Seconds left, or None while it cannot be told honestly. Speakers and text take very
    different time, so: on the text stage — from the real pace of the text (the file is
    created when the stage starts); before it — from the measured speed of the laptop."""
    dur = item.meta.get('duration') or 0
    elapsed = max(0.0, time.time() - started_ts(item))
    try:
        st = item.html_path.stat()
        if st.st_mtime >= started_ts(item):
            paras = item.paragraphs()
            pos = paras[-1][1] / 1000 if paras else 0
            if pos >= 60:
                return max(0.0, (dur - pos) * (time.time() - st.st_ctime) / pos)
    except OSError:
        pass
    left = estimate(item.meta) - elapsed
    return left if left > 60 else None


def estimate(meta):
    """Seconds of work expected for an item, before anything is measured; 25 s is loading the model."""
    k = SPEED.get(meta.get('model'), 2.0)
    if str(meta.get('speakers', 'auto')) == 'none':
        k *= NO_SPEAKERS_FACTOR
    return (meta.get('duration') or 0) * k + 25


def run_blocking(item, owner='agent', wait=True):
    """For the agent: hand the item to the worker and, if asked, wait for it. Waiting is only
    watching: if the agent goes away, the worker carries on."""
    ensure_worker()
    while wait:
        item.reload()
        if item.status in ('done', 'error'):
            break
        if not lock_busy():
            ensure_worker()
        time.sleep(10)
    return item


if __name__ == '__main__':
    import sys
    if '--worker' in sys.argv:
        run_worker()

# --- playback copy: the window's built-in Chromium has no AAC/H.264, so m4a/mp4 voice memos
# get an Opus copy for the player only; transcription always uses the original ---
NATIVE_PLAY = {'.ogg', '.opus', '.mp3', '.wav', '.flac', '.webm'}


def play_path(item):
    a = item.audio
    if a is None or not a.exists():
        return None
    if a.suffix.lower() in NATIVE_PLAY:
        return a
    p = item.folder / 'play.ogg'
    return p if p.exists() else None


def make_playable(item):
    a = item.audio
    if a is None or not a.exists() or a.suffix.lower() in NATIVE_PLAY:
        return
    dst = item.folder / 'play.ogg'
    if dst.exists():
        return
    import av
    tmp = item.folder / 'play.tmp.ogg'
    with av.open(str(a)) as src, av.open(str(tmp), 'w', format='ogg') as out:
        try:
            st = out.add_stream('libopus', rate=48000)
            st.bit_rate = 32000
        except Exception:
            st = out.add_stream('flac', rate=48000)
        st.layout = 'mono'
        rs_ = av.AudioResampler(format=st.format.name if hasattr(st, 'format') and st.format else 's16', layout='mono', rate=48000)
        for frame in src.decode(audio=0):
            frame.pts = None
            for f in rs_.resample(frame):
                for pkt in st.encode(f):
                    out.mux(pkt)
        for f in rs_.resample(None):
            for pkt in st.encode(f):
                out.mux(pkt)
        for pkt in st.encode(None):
            out.mux(pkt)
    os.replace(tmp, dst)

