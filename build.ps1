# Сборка "Расшифровки" в exe: движок, редактор и окно.
#
# Зачем exe, если есть install.ps1: на exe можно поставить запрет сети в брандмауэре
# Windows одним правилом на файл — гарантия, что звук и текст не уйдут с компьютера.
# Правило брандмауэра привязано к пути exe: пересобирайте на то же место, и оно
# продолжит действовать. Окно при пересборке не удаляется, а обновляется поверх.
#
# Запуск из папки программы (после install.ps1):
#   powershell -ExecutionPolicy Bypass -File build.ps1
# Параметры:
#   -Python     Python с установленными библиотеками (по умолчанию .venv)
#   -EngineOut  куда собрать движок  (по умолчанию dist в папке программы)
#   -AppOut     куда собрать окно    (по умолчанию dist в папке программы)
# Результат: dist\Rasshifrovka\Rasshifrovka.exe — окно, dist\noScribe\noScribe.exe — движок.

param(
    [string]$Python = '',
    [string]$EngineOut = '',
    [string]$AppOut = ''
)
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Python) { $Python = "$Root\.venv\Scripts\python.exe" }
if (-not $EngineOut) { $EngineOut = "$Root\dist" }
if (-not $AppOut) { $AppOut = "$Root\dist" }
if (-not (Test-Path $Python)) { throw "Нет Python с библиотеками: $Python. Сначала запустите install.ps1 или укажите -Python." }
& $Python -m PyInstaller --version *> $null
if ($LASTEXITCODE) { & $Python -m pip install --quiet pyinstaller==6.14.1; if ($LASTEXITCODE) { throw 'Не удалось поставить PyInstaller' } }
$Work = "$Root\build"
function Step($t) { Write-Host "`n== $t" -ForegroundColor Cyan }
function PyInstaller { & $Python -m PyInstaller --noconfirm @args; if ($LASTEXITCODE) { throw "PyInstaller: код $LASTEXITCODE" } }

# лок движка: не собирать поверх идущей расшифровки (хранилище — как в app\rasstore.py)
$store = if ($env:RASSHIFROVKA_STORE) { $env:RASSHIFROVKA_STORE } else { "$HOME\Documents\Расшифровки" }
$lock = "$store\.engine.lock"
if ((Test-Path $lock) -and ((Get-Date) - (Get-Item $lock).LastWriteTime).TotalSeconds -lt 90) {
    throw 'Сейчас идёт расшифровка — дождитесь конца и запустите сборку ещё раз.'
}

Step 'Редактор'
PyInstaller "$Root\editor\noScribeEdit_win.spec" --distpath "$Work\editor-dist" --workpath "$Work\editor-work"
Get-ChildItem "$Root\engine\noScribeEdit" -Exclude 'readme.txt' | Remove-Item -Recurse -Force
Copy-Item "$Work\editor-dist\noScribeEdit\*" "$Root\engine\noScribeEdit\" -Recurse -Force

Step 'Движок'
# "Быстрая" — жёсткая ссылка на model.bin "Точной": в сборку её не копируем, ставим ссылку после
$fastBin = "$Root\engine\models\fast\model.bin"
$aside = "$Work\fast-model.bin.aside"
New-Item -ItemType Directory -Force $Work | Out-Null
if (Test-Path $fastBin) { Move-Item $fastBin $aside -Force }
try {
    PyInstaller "$Root\engine\pyinstaller\noScribe_win.spec" --distpath $EngineOut --workpath "$Work\engine-work"
} finally {
    if (Test-Path $aside) { Move-Item $aside $fastBin -Force }
}
$distModels = "$EngineOut\noScribe\_internal\models"
if (-not (Test-Path "$distModels\fast\model.bin")) {
    New-Item -ItemType HardLink -Path "$distModels\fast\model.bin" -Target "$distModels\precise\model.bin" | Out-Null
}

Step 'Окно'
Push-Location "$Root\app"
try {
    PyInstaller --windowed --name Rasshifrovka --distpath "$Work\app-dist" --workpath "$Work\app-work" `
        --add-data "ui;ui" --hidden-import recorder --hidden-import av `
        --collect-all soundcard --collect-all _soundfile_data --collect-data faster_whisper `
        --collect-binaries ctranslate2 --collect-binaries onnxruntime `
        --exclude-module torch --exclude-module torchaudio --exclude-module pyannote --exclude-module lightning `
        --exclude-module matplotlib --exclude-module scipy --exclude-module pandas `
        --exclude-module customtkinter --exclude-module tkinter app.py
} finally { Pop-Location }
# копируем поверх, а не удаляем папку: так сохраняется путь exe (и правило брандмауэра),
# а открытое окно не мешает обновлению
New-Item -ItemType Directory -Force "$AppOut\Rasshifrovka" | Out-Null
Copy-Item "$Work\app-dist\Rasshifrovka\*" "$AppOut\Rasshifrovka\" -Recurse -Force
Set-Content -Path "$AppOut\Rasshifrovka\engine_dir.txt" -Value "$EngineOut\noScribe" -Encoding UTF8 -NoNewline

Write-Host "`nГотово." -ForegroundColor Green
Write-Host "Окно:   $AppOut\Rasshifrovka\Rasshifrovka.exe"
Write-Host "Движок: $EngineOut\noScribe\noScribe.exe"
Write-Host "`nЗапрет сети (PowerShell от имени администратора, один раз):"
foreach ($p in "$AppOut\Rasshifrovka\Rasshifrovka.exe",
               "$AppOut\Rasshifrovka\_internal\PyQt6\Qt6\bin\QtWebEngineProcess.exe",
               "$EngineOut\noScribe\noScribe.exe",
               "$EngineOut\noScribe\_internal\noScribeEdit\noScribeEdit.exe") {
    Write-Host "New-NetFirewallRule -DisplayName `"Расшифровка - без сети: $(Split-Path $p -Leaf)`" -Direction Outbound -Action Block -Program `"$p`""
}
