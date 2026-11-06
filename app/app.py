# Расшифровка — окно для Ильдара поверх движка noScribe.
#
# Интерфейс — HTML в ui/index.html, показывается встроенным Chromium (Qt WebEngine)
# с загрузкой только с диска; сетевые службы Chromium выключены флагами, а на exe
# стоит запрет в брандмауэре. Python отвечает за хранилище (rasstore.py), очередь
# движка, запись (recorder.py) и связь с окном через QWebChannel.
# Почему HTML, а не виджеты: 13.10 Ильдар — "пользоваться трудно, цвета, расположение
# кнопок, всё не интуитивно"; понятный современный интерфейс на виджетах Qt не собрать.

import datetime as dt
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault('QTWEBENGINE_CHROMIUM_FLAGS', ' '.join([
    '--disable-background-networking', '--disable-component-update', '--disable-domain-reliability',
    '--disable-sync', '--no-pings', '--disable-breakpad',
    '--disable-features=AutofillServerCommunication,OptimizationHints,MediaRouter,Translate,SafeBrowsing']))

from PyQt6 import QtCore, QtGui, QtWidgets  # noqa: E402
from PyQt6.QtCore import Qt, pyqtSignal, pyqtSlot  # noqa: E402
from PyQt6.QtWebChannel import QWebChannel  # noqa: E402
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings  # noqa: E402
from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: E402

import rasstore as rs  # noqa: E402

APP_NAME = 'Расшифровка'
BASE = Path(getattr(sys, '_MEIPASS', Path(__file__).parent))
SETTINGS = rs.STORE / '.settings.json'
TRASH = rs.STORE / 'Удалённые'

# Разделение по голосам — тумблер в окне добавления, по умолчанию выключен: оно съедает больше
# половины времени (запись 1:18 — 35 минут только на голоса), а чаще нужно понять, что в записи,
# а не кто что сказал. Здесь — сколько человек говорит, когда тумблер включён.
SPEAKER_COUNTS = [('auto', 'Определить само'), ('2', 'Двое'), ('3', 'Трое'), ('4', 'Четверо'), ('5', 'Пять')]
LANG_OPTIONS = [('ru', 'Русский'), ('kk', 'Казахский'), ('multilingual', 'Русский и казахский'),
                ('auto', 'Определить само')]


def load_settings():
    """Model, language, the speaker count and the device are remembered between additions; the speaker switch is not."""
    try:
        s = json.loads(SETTINGS.read_text(encoding='utf-8'))
    except Exception:
        s = {}
    spk = str(s.get('speakers', 'auto'))
    if spk not in dict(SPEAKER_COUNTS):  # 'none' and '1' from the old settings mean no split, not a count
        spk = 'auto'
    device = s.get('device', 'auto')
    if device not in dict(rs.DEVICES):
        device = 'auto'
    return {'model': s.get('model', 'fast'), 'language': s.get('language', 'ru'), 'speakers': spk, 'device': device}


def item_options(settings, split=False):
    return {'model': settings['model'], 'language': settings['language'],
            'speakers': settings['speakers'] if split else 'none', 'device': settings['device']}


def speed_table():
    """{device: {model: [without speakers, with speakers]}} — for the estimate in the "how to transcribe" window."""
    ready = rs.gpu()['ready']
    return {d: {m: [rs.speed_factor(m, split, ready and d != 'cpu') for split in (False, True)] for m in rs.models()}
            for d, _ in rs.DEVICES}


def theme_name():
    try:
        return 'dark' if QtGui.QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark else 'light'
    except Exception:
        return 'light'


class Page(QWebEnginePage):
    """Only our own file is shown; any link that tries to leave goes nowhere."""

    def acceptNavigationRequest(self, url, nav_type, is_main):
        return url.isLocalFile() or url.scheme() in ('qrc', 'data', 'about')


class Api(QtCore.QObject):
    changed = pyqtSignal()
    recTick = pyqtSignal(float, float)
    liveText = pyqtSignal(str, str)
    toast = pyqtSignal(str)

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.settings = load_settings()
        self.sig = None
        self.dur_cache = {}
        self.converting = set()
        self.rec = None
        self.live = None
        self.live_pos = 0.0
        self.watch = rs.load_watch()
        self.watch_sizes = {}
        self.watch_lock = threading.Lock()
        self.watch_running = False
        self.watch_last = 0.0
        self.recover_recordings()
        rs.ensure_worker()
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(1500)
        self.rec_timer = QtCore.QTimer(self)
        self.rec_timer.timeout.connect(self.rec_tick)

    # ---------- data for the page ----------
    def item_dict(self, it, full=False):
        m = it.meta
        dur = m.get('duration') or 0
        est = rs.estimate(m)
        elapsed, progress, stage, left = 0, 0.0, '', est
        if it.status == 'running':
            elapsed = max(0.0, time.time() - rs.started_ts(it))
            stage, progress = rs.progress(it)
            left = rs.time_left(it)
        d = {'id': it.id, 'title': it.title, 'created': it.created.isoformat(), 'duration': dur,
             'status': it.status, 'status_name': rs.STATUS_NAMES.get(it.status, it.status), 'progress': progress}
        if full:
            spk = it.speakers()
            names = m.get('speaker_names', {})
            d.update({
                'created_text': f'{it.created:%d.%m.%Y, %H:%M}',
                'model_name': 'модель ' + rs.model_title(m.get('model')).lower(),
                'device_name': {'gpu': 'на видеокарте', 'cpu': 'на процессоре'}.get(m.get('computed_on'), ''),
                'device': m.get('device', 'auto'),
                'lang_name': dict(LANG_OPTIONS).get(m.get('language'), m.get('language', '')),
                'split': str(m.get('speakers', 'auto')) != 'none', 'model': m.get('model'),
                'language': m.get('language'), 'speaker_count': str(m.get('speakers', 'auto')),
                'took': m.get('took'), 'origin': m.get('origin'), 'error': m.get('error', ''),
                'elapsed': elapsed, 'estimate': est, 'left': left, 'stage': stage,
                'interrupted': bool(m.get('interrupted')), 'ahead': self.queue_ahead(it),
                'audio_url': self.audio_url(it),
                'paragraphs': it.paragraphs() if it.status in ('done', 'running') else [],
                'speakers': [{'id': s, 'name': it.speaker_name(s), 'named': s in names, 'color': n % 6}
                             for n, s in enumerate(spk)],
            })
        return d

    def audio_url(self, it):
        pp = rs.play_path(it)
        if pp is not None:
            return QtCore.QUrl.fromLocalFile(str(pp)).toString()
        if it.audio and it.audio.exists() and it.id not in self.converting:
            self.converting.add(it.id)
            def work(it=it):
                try:
                    rs.make_playable(it)
                except Exception:
                    pass
                QtCore.QMetaObject.invokeMethod(self, 'emitChanged', Qt.ConnectionType.QueuedConnection)
            threading.Thread(target=work, daemon=True).start()
        return ''

    @pyqtSlot()
    def emitChanged(self):
        self.changed.emit()

    def downloads(self):
        out = []
        for p in rs.new_in_downloads()[:30]:
            st = p.stat()
            key = (str(p), st.st_mtime)
            if key not in self.dur_cache:
                self.dur_cache[key] = rs.audio_duration(p)
            out.append({'name': p.stem, 'path': str(p), 'duration': self.dur_cache[key],
                        'when': f'{dt.datetime.fromtimestamp(st.st_mtime):%d.%m %H:%M}'})
        return out

    @pyqtSlot(result=str)
    def state(self):
        return json.dumps({
            'theme': theme_name(),
            'items': [self.item_dict(i) for i in rs.items()],
            'downloads': self.downloads(),
            'watch': self.watch_public(),
            'settings': self.settings,
            'options': {'models': [(m, rs.model_title(m)) for m in rs.models()],
                        'langs': LANG_OPTIONS, 'speakers': SPEAKER_COUNTS,
                        'devices': rs.DEVICES, 'gpu': rs.gpu(), 'speed': speed_table()},
        }, ensure_ascii=False)

    @pyqtSlot(str, result=str)
    def item(self, iid):
        f = rs.STORE / iid
        if not (f / 'meta.json').exists():
            return ''
        return json.dumps(self.item_dict(rs.Item(f), full=True), ensure_ascii=False)

    # ---------- adding ----------
    @pyqtSlot(str)
    def setSettings(self, js):
        self.settings.update(json.loads(js))
        rs.STORE.mkdir(parents=True, exist_ok=True)
        SETTINGS.write_text(json.dumps(self.settings, ensure_ascii=False), encoding='utf-8')

    # A chosen file is not queued at once: the page first shows how to transcribe it
    # (speakers, quality, language) and queues it on "Расшифровать".
    def describe(self, paths):
        out = []
        for f in paths:
            p = Path(f)
            if not p.is_file() or p.suffix.lower() not in rs.AUDIO_EXT:
                self.toast.emit(f'{p.name} — не аудиофайл, пропущен')
                continue
            key = (str(p), p.stat().st_mtime)
            if key not in self.dur_cache:
                self.dur_cache[key] = rs.audio_duration(p)
            out.append({'path': str(p), 'name': p.stem, 'duration': self.dur_cache[key]})
        return json.dumps(out, ensure_ascii=False)

    @pyqtSlot(str, result=str)
    def describeUris(self, js):
        return self.describe([QtCore.QUrl(u).toLocalFile() for u in json.loads(js)])

    @pyqtSlot(result=str)
    def pickFiles(self):
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self.win, 'Выберите запись', str(rs.DOWNLOADS),
            'Аудио (' + ' '.join('*' + e for e in sorted(rs.AUDIO_EXT)) + ');;Все файлы (*.*)')
        return self.describe(files)

    def remember(self, opts):
        keep = {k: opts[k] for k in ('model', 'language', 'device') if opts.get(k)}
        if opts.get('split') and opts.get('speakers'):
            keep['speakers'] = opts['speakers']
        self.setSettings(json.dumps(keep))

    @pyqtSlot(str, result=str)
    def addFiles(self, js):
        """{paths, split, model, language, speakers} -> id of the last queued item."""
        d = json.loads(js)
        self.remember(d)
        opts = item_options(self.settings, bool(d.get('split')))
        last = None
        for f in d['paths']:
            p = Path(f)
            if p.is_file():
                last = rs.create_item(p, origin='app', **opts)
        rs.ensure_worker()
        self.changed.emit()
        return last.id if last else ''

    # ---------- folders with automatic transcription ----------
    def watch_public(self):
        with self.watch_lock:
            return [{'path': r['path'], 'name': Path(r['path']).name or r['path'], 'auto': bool(r.get('auto')),
                     'model': r.get('model', 'fast'), 'language': r.get('language', 'ru'),
                     'speakers': r.get('speakers', 'none'), 'exists': os.path.isdir(r['path'])}
                    for r in self.watch]

    @pyqtSlot()
    def watchAdd(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(self.win, 'Папка с записями', str(rs.DOWNLOADS))
        if not path:
            return
        path = str(Path(path))
        with self.watch_lock:
            if any(r['path'] == path for r in self.watch):
                self.toast.emit('Эта папка уже добавлена')
                return
            self.watch.append({'path': path, 'auto': True, 'model': 'fast', 'language': 'ru', 'speakers': 'none',
                               'seen': rs.watch_snapshot(path)})
            rs.save_watch(self.watch)
        self.changed.emit()

    @pyqtSlot(str)
    def watchSet(self, js):
        d = json.loads(js)
        with self.watch_lock:
            for r in self.watch:
                if r['path'] == d.get('path'):
                    if d.get('auto') and not r.get('auto'):  # what piled up while it was off is not touched
                        r['seen'] = rs.watch_snapshot(r['path'])
                    r.update({k: d[k] for k in ('auto', 'model', 'language', 'speakers') if k in d})
            rs.save_watch(self.watch)
        self.changed.emit()

    @pyqtSlot(str)
    def watchRemove(self, path):
        with self.watch_lock:
            self.watch = [r for r in self.watch if r['path'] != path]
            rs.save_watch(self.watch)
        self.changed.emit()

    def watch_tick(self):
        now = time.time()
        if self.watch_running or now - self.watch_last < 2 or not any(r.get('auto') for r in self.watch):
            return
        self.watch_last = now
        self.watch_running = True
        threading.Thread(target=self._watch_scan, daemon=True).start()  # a cloud folder may be slow

    def _watch_scan(self):
        try:
            with self.watch_lock:
                rows = [dict(r) for r in self.watch if r.get('auto')]
            added = 0
            for row in rows:
                for p in rs.watch_ready(row, self.watch_sizes):
                    opts = {'model': row.get('model', 'fast'), 'language': row.get('language', 'ru'),
                            'speakers': row.get('speakers', 'none')}
                    if 'device' in self.settings:
                        opts['device'] = self.settings['device']
                    size = p.stat().st_size
                    rs.create_item(p, origin='auto', **opts)
                    with self.watch_lock:
                        for r in self.watch:
                            if r['path'] == row['path']:
                                r.setdefault('seen', []).append([p.name.lower(), size])
                        rs.save_watch(self.watch)
                    self.watch_sizes.pop(str(p), None)
                    added += 1
                    self.toast.emit(f'Новая запись из папки: {rs.clean_title(p.name)}')
            if added:
                rs.ensure_worker()
                self.changed.emit()
        except Exception as e:
            self.toast.emit(f'Не удалось взять запись из папки: {e}')
        finally:
            self.watch_running = False

    # ---------- item actions ----------
    def _item(self, iid):
        return rs.Item(rs.STORE / iid)

    @pyqtSlot(str, str)
    def rename(self, iid, title):
        it = self._item(iid)
        it.meta['title'] = title
        it.save()
        if it.status == 'done':
            it.write_md()
        self.changed.emit()

    @pyqtSlot(str, str, str)
    def renameSpeaker(self, iid, spk, name):
        it = self._item(iid)
        it.meta.setdefault('speaker_names', {})[spk] = name
        it.save()
        if it.status == 'done':
            it.write_md()

    @pyqtSlot(str)
    def copyText(self, iid):
        QtWidgets.QApplication.clipboard().setText(self._item(iid).plain_text())

    @pyqtSlot(str)
    def openEditor(self, iid):
        prog, args = rs.editor_command(self._item(iid).html_path)
        QtCore.QProcess.startDetached(prog, args)

    @pyqtSlot(str)
    def openFolder(self, iid):
        os.startfile(str(rs.STORE / iid))

    @pyqtSlot(str, str)
    def retry(self, iid, js=''):
        """Again, with the same settings or, from the window, with new ones. A running one is stopped first."""
        opts = {}
        if js:
            d = json.loads(js)
            self.remember(d)
            opts = item_options(self.settings, bool(d.get('split')))
        if self._item(iid).status == 'running':
            threading.Thread(target=self._restart, args=(iid, opts), daemon=True).start()
            return
        self._requeue(iid, opts)

    def _requeue(self, iid, opts):
        it = self._item(iid)
        it.meta.update(opts)
        it.meta.update(status='queued', error='', attempts=0)
        it.meta.pop('interrupted', None)
        it.save()
        rs.ensure_worker()
        self.changed.emit()

    def _restart(self, iid, opts):
        rs.request_cancel(iid)  # waits up to 20 s, so not on the window's thread
        if self._item(iid).status == 'running':
            self.toast.emit('Не удалось остановить расшифровку, попробуйте ещё раз')
            return
        self._requeue(iid, opts)

    @pyqtSlot(str)
    def remove(self, iid):
        """Not a hard delete: the folder moves to "Удалённые" in the store and can be put back by hand."""
        if self._item(iid).status == 'running':
            rs.request_cancel(iid)
        TRASH.mkdir(parents=True, exist_ok=True)
        dst, n = TRASH / iid, 2
        while dst.exists():
            dst, n = TRASH / f'{iid} ({n})', n + 1
        shutil.move(str(rs.STORE / iid), str(dst))
        self.changed.emit()

    # ---------- engine queue ----------
    def queue_ahead(self, it):
        if it.status != 'queued':
            return 0
        busy = 1 if any(i.status == 'running' for i in rs.items()) else 0
        for n, i in enumerate(rs.queued()):
            if i.id == it.id:
                return n + busy
        return busy

    def tick(self):
        # the engine runs in its own process (rs.run_worker); the window only makes sure
        # someone is working on the queue and shows what is going on
        self.watch_tick()
        rs.ensure_worker()
        its = rs.items()
        sig = tuple((i.id, i.title, i.status) for i in its) + tuple(p.name for p in rs.new_in_downloads()[:30])
        if sig != self.sig or any(i.status == 'running' for i in its):
            self.sig = sig
            self.changed.emit()

    # ---------- recording ----------
    REC_ROOT = rs.STORE / '.recordings'

    def recover_recordings(self):
        """A recording cut off by a crash or power loss becomes a normal item on the next start."""
        import recorder
        n = 0
        for d in recorder.unfinished_sessions(self.REC_ROOT):
            try:
                when = dt.datetime.strptime(d.name[:19], '%Y-%m-%d %H-%M-%S')
            except ValueError:
                when = dt.datetime.fromtimestamp(d.stat().st_mtime)
            out = d / 'joined.flac'
            path = recorder.join_parts(d, out)
            if path:
                rs.create_item(path, title=f'Восстановленная запись {when:%d.%m %H:%M}', origin='app',
                               created=when, **item_options(self.settings))
                n += 1
            shutil.rmtree(d, ignore_errors=True)
        if n:
            QtCore.QTimer.singleShot(2500, lambda: self.toast.emit(
                'Найдена прерванная запись — она сохранена в истории и поставлена в расшифровку'))
        return n

    @pyqtSlot(str, bool, result=str)
    def recordStart(self, source, split):
        import recorder
        now = dt.datetime.now()
        self.rec = recorder.Recorder(self.REC_ROOT / f'{now:%Y-%m-%d %H-%M-%S}', with_system=(source == 'both'))
        self.rec.source = source
        self.rec.split = split
        self.rec.started_at = now
        self.rec.start()
        QtCore.QThread.msleep(500)
        if self.rec.stop_ev.is_set():  # the microphone did not open
            err = self.rec.error
            self.rec.stop(self.rec.dir / 'x.flac')
            shutil.rmtree(self.rec.dir, ignore_errors=True)
            self.rec = None
            return f'Запись не началась: {err}'
        if self.rec.error:
            self.toast.emit(self.rec.error)
        self.live = recorder.LiveText(rs.MODELS_DIR / 'fast', self.settings.get('language', 'ru'),
                                      lambda txt, note: self.liveText.emit(txt or '', note or ''))
        self.live_pos = 0.0
        self.rec_timer.start(200)
        return ''

    def rec_tick(self):
        if self.rec is None:
            return
        import recorder
        if self.rec.stop_ev.is_set():  # microphone lost mid-recording: save what we have
            msg = self.rec.error
            r = json.loads(self.recordStop())
            self.toast.emit(msg or 'Запись остановилась — записанное сохранено')
            if r.get('id'):
                self.win.view.page().runJavaScript(f'recOn=false; renderRec(0,0); refresh().then(() => openItem({json.dumps(r["id"])}))')
            return
        sec = self.rec.seconds
        self.recTick.emit(sec, self.rec.level)
        if self.live is not None and sec - self.live_pos >= recorder.LIVE_CHUNK_SEC:
            self.live.feed(self.rec.snapshot(max(0.0, self.live_pos - recorder.LIVE_OVERLAP_SEC)))
            self.live_pos = sec

    @pyqtSlot(result=str)
    def recordStop(self):
        self.rec_timer.stop()
        if self.rec is None:
            return json.dumps({'error': 'Запись не шла'})
        rec, self.rec = self.rec, None
        if self.live is not None:
            self.live.close()
            self.live = None
        path = rec.stop(rec.dir / 'joined.flac')
        if not path:
            shutil.rmtree(rec.dir, ignore_errors=True)
            return json.dumps({'error': 'Запись короче секунды — ничего не сохранено'})
        kind = 'Созвон' if rec.source == 'both' else 'Встреча'
        it = rs.create_item(path, title=f'{kind} {rec.started_at:%d.%m %H:%M}', origin='app',
                            created=rec.started_at, **item_options(self.settings, rec.split))
        shutil.rmtree(rec.dir, ignore_errors=True)  # only after the copy is safe in the store
        rs.ensure_worker()
        self.changed.emit()
        return json.dumps({'id': it.id})


class Main(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1280, 840)
        self.setMinimumSize(900, 600)
        self.setAcceptDrops(True)
        self.view = QWebEngineView(self)
        self.view.setAcceptDrops(False)  # drops land on the window, which knows real file paths
        page = Page(self.view)
        self.view.setPage(page)
        st = page.settings()
        st.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        st.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
        st.setAttribute(QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, False)
        self.api = Api(self)
        self.channel = QWebChannel(page)
        self.channel.registerObject('api', self.api)
        page.setWebChannel(self.channel)
        self.view.setUrl(QtCore.QUrl.fromLocalFile(str(BASE / 'ui' / 'index.html')))
        self.setCentralWidget(self.view)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        files = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if files:
            self.view.page().runJavaScript(f'askAdd({self.api.describe(files)})')

    def closeEvent(self, e):
        if self.api.rec is not None:
            ans = QtWidgets.QMessageBox.question(self, APP_NAME, 'Идёт запись. Остановить и сохранить её?')
            if ans == QtWidgets.QMessageBox.StandardButton.Yes:
                self.api.recordStop()
            else:
                e.ignore()
                return
        e.accept()  # the transcription is not the window's: it goes on after the window is closed


def make_icon():
    pm = QtGui.QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    p.setBrush(QtGui.QColor('#2756C7'))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(0, 0, 64, 64, 16, 16)
    p.setBrush(QtGui.QColor('#FFFFFF'))
    for x, h in ((12, 16), (22, 30), (32, 42), (42, 24), (52, 12)):
        p.drawRoundedRect(x - 3, 32 - h // 2, 6, h, 3, 3)
    p.end()
    return QtGui.QIcon(pm)


def main():
    if '--worker' in sys.argv:
        rs.run_worker()
        return
    if os.name == 'nt':
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('Ildar.Rasshifrovka')
        except Exception:
            pass
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setWindowIcon(make_icon())
    w = Main()
    w.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
