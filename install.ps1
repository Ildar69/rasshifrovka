# Установка "Расшифровки" на компьютер с Windows.
# Запуск: правой кнопкой по файлу -> "Выполнить с помощью PowerShell",
# или в PowerShell из папки программы:  powershell -ExecutionPolicy Bypass -File install.ps1
#
# Что делает: ставит библиотеки в папку .venv рядом с программой (с видеокартой NVIDIA —
# и библиотеки для неё), скачивает модель распознавания (~1,6 ГБ) и кладёт ярлык
# "Расшифровка" на рабочий стол. Повторный запуск безопасен: обновляет то, что изменилось.
# Интернет нужен только здесь, на установке. Сама программа в сеть не ходит.

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Step($text) { Write-Host "`n== $text" -ForegroundColor Cyan }

Step 'Ищу Python 3.12'
$py = $null
try { & py -3.12 -c "import sys" 2>$null; if ($LASTEXITCODE -eq 0) { $py = @('py', '-3.12') } } catch {}
if (-not $py) {
    Write-Host 'Нужен Python 3.12: https://www.python.org/downloads/release/python-31210/' -ForegroundColor Yellow
    Write-Host 'При установке отметьте "Add python.exe to PATH". Потом запустите install.ps1 ещё раз.'
    exit 1
}

Step 'Создаю окружение .venv'
if (-not (Test-Path "$Root\.venv\Scripts\python.exe")) { & $py[0] $py[1] -m venv "$Root\.venv" }
$vpy = "$Root\.venv\Scripts\python.exe"
& $vpy -m pip install --upgrade pip --quiet

# С видеокартой NVIDIA — сборка torch с CUDA: распознавание и разделение голосов идут на карте,
# в разы быстрее (RTX 3050 Laptop: запись 16:51 — 22:30 на процессоре, 2:04 на карте).
# Версия указана полностью (+cu126 / +cpu): иначе pip считает уже стоящую другую сборку подходящей
# и при повторном запуске ничего не меняет.
$gpu = $null
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    try { $gpu = (& nvidia-smi --query-gpu=name --format=csv,noheader 2>$null | Select-Object -First 1) } catch {}
}
if ($gpu) {
    Step "Нашёл видеокарту $gpu - ставлю torch с CUDA (около 2,5 ГБ)"
    & $vpy -m pip install --quiet --no-cache-dir --retries 10 --timeout 60 torch==2.8.0+cu126 torchaudio==2.8.0+cu126 --index-url https://download.pytorch.org/whl/cu126
} else {
    Step 'Видеокарты NVIDIA нет - ставлю torch для процессора (около 200 МБ)'
    & $vpy -m pip install --quiet --no-cache-dir --retries 10 --timeout 60 torch==2.8.0+cpu torchaudio==2.8.0+cpu --index-url https://download.pytorch.org/whl/cpu
}

Step 'Ставлю остальные библиотеки'
& $vpy -m pip install --quiet --retries 10 --timeout 60 -r "$Root\requirements.txt"

Step 'Скачиваю модель распознавания large-v3-turbo (~1,6 ГБ, один раз)'
$precise = "$Root\engine\models\precise"
$fast = "$Root\engine\models\fast"
if (-not (Test-Path "$precise\model.bin")) {
    & $vpy -c "from huggingface_hub import snapshot_download; snapshot_download('mobiuslabsgmbh/faster-whisper-large-v3-turbo', local_dir=r'$precise', allow_patterns=['*.json', 'model.bin'])"
}
# "Быстрая" — та же модель в int8: один model.bin на обе настройки, без второй копии на диске
foreach ($f in 'config.json', 'preprocessor_config.json', 'tokenizer.json', 'vocabulary.json') {
    Copy-Item "$precise\$f" "$fast\$f" -Force
}
if (-not (Test-Path "$fast\model.bin")) {
    try { New-Item -ItemType HardLink -Path "$fast\model.bin" -Target "$precise\model.bin" | Out-Null }
    catch { Copy-Item "$precise\model.bin" "$fast\model.bin" }
}

Step 'Кладу ярлык на рабочий стол'
$desk = [Environment]::GetFolderPath('Desktop')
$s = (New-Object -ComObject WScript.Shell).CreateShortcut("$desk\Расшифровка.lnk")
$s.TargetPath = "$Root\.venv\Scripts\pythonw.exe"
$s.Arguments = "`"$Root\app\app.py`""
$s.WorkingDirectory = $Root
$s.Description = 'Расшифровка записей без интернета'
$s.Save()

Write-Host "`nГотово. Откройте ярлык 'Расшифровка' на рабочем столе." -ForegroundColor Green
Write-Host 'Расшифровки будут лежать в Документы\Расшифровки.'
