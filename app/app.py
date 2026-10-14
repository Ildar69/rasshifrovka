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

SPEAKER_OPTIONS = [('auto', 'Определить само'), ('1', 'Один'), ('2', 'Двое'), ('3', 'Трое'),
                   ('4', 'Четверо'), ('5', 'Пять'), ('none', 'Не разделять')]
LANG_OPTIONS = [('ru', 'Русский'), ('kk', 'Казахский'), ('multilingual', 'Русский и казахский'),
                ('auto', 'Определить само')]


def load_settings():
    try:
        s = json.loads(SETTINGS.read_text(encoding='utf-8'))
    except Exception:
        s = {}
    return {'model': s.get('model', 'fast'), 'language': s.get('language', 'ru'), 'speakers': s.get('speakers', 'auto')}


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
        est = dur * rs.SPEED.get(m.get('model'), 2.0) + 25
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
                'lang_name': dict(LANG_OPTIONS).get(m.get('language'), m.get('language', '')),
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
            'settings': self.settings,
            'options': {'models': [(m, rs.model_title(m)) for m in rs.models()],
                        'langs': LANG_OPTIONS, 'speakers': SPEAKER_OPTIONS},
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

    def add_paths(self, paths):
        last = None
        for f in paths:
            p = Path(f)
            if not p.is_file() or p.suffix.lower() not in rs.AUDIO_EXT:
                self.toast.emit(f'{p.name} — не аудиофайл, пропущен')
                continue
            last = rs.create_item(p, origin='app', **self.settings)
        rs.ensure_worker()
        self.changed.emit()
        return last.id if last else ''

    @pyqtSlot(str, result=str)
    def addFiles(self, js):
        return self.add_paths(json.loads(js))

    @pyqtSlot(str, result=str)
    def addUris(self, js):
        return self.add_paths([QtCore.QUrl(u).toLocalFile() for u in json.loads(js)])

    @pyqtSlot(result=str)
    def pickFiles(self):
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self.win, 'Выберите запись', str(rs.DOWNLOADS),
            'Аудио (' + ' '.join('*' + e for e in sorted(rs.AUDIO_EXT)) + ');;Все файлы (*.*)')
        return self.add_paths(files) if files else ''

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

    @pyqtSlot(str)
    def retry(self, iid):
        it = self._item(iid)
        it.meta.update(status='queued', error='', attempts=0)
        it.meta.pop('interrupted', None)
        it.save()
        rs.ensure_worker()
        self.changed.emit()

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
                               created=when, **self.settings)
                n += 1
            shutil.rmtree(d, ignore_errors=True)
        if n:
            QtCore.QTimer.singleShot(2500, lambda: self.toast.emit(
                'Найдена прерванная запись — она сохранена в истории и поставлена в расшифровку'))
        return n

    @pyqtSlot(str, result=str)
    def recordStart(self, source):
        import recorder
        now = dt.datetime.now()
        self.rec = recorder.Recorder(self.REC_ROOT / f'{now:%Y-%m-%d %H-%M-%S}', with_system=(source == 'both'))
        self.rec.source = source
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
                            created=rec.started_at, **self.settings)
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
            iid = self.api.add_paths(files)
            if iid:
                self.api.toast.emit('Поставлено в очередь')
                self.view.page().runJavaScript(f'refresh().then(() => openItem({json.dumps(iid)}))')

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
