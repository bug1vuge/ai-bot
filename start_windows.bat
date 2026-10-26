@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Бот «ИИ без тёмного леса»

rem Ищем рабочий Python 3.12-3.14: команда python может указывать на удалённую установку
set "PY="
for %%C in ("python" "py -3.12" "py -3.13" "py -3.14" "py -3") do (
  if not defined PY (
    %%~C -c "import sys; sys.exit(0 if (3,12)<=sys.version_info[:2]<=(3,14) else 1)" >nul 2>nul && set "PY=%%~C"
  )
)
rem Если команды не сработали — ищем Python в стандартных папках установки
for %%V in (313 312 314) do (
  if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" (
    "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" -c "import sys" >nul 2>nul && set PY="%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
  )
  if not defined PY if exist "%ProgramFiles%\Python%%V\python.exe" (
    "%ProgramFiles%\Python%%V\python.exe" -c "import sys" >nul 2>nul && set PY="%ProgramFiles%\Python%%V\python.exe"
  )
)
if not defined PY (
  echo [!] Не найден рабочий Python версии 3.12, 3.13 или 3.14.
  echo     Установите Python 3.13 с python.org ^(Windows installer 64-bit^),
  echo     поставьте галочку "Add python.exe to PATH" и перезагрузите компьютер.
  pause
  exit /b 1
)
echo Использую Python: %PY%

if not exist ".env" (
  copy ".env.example" ".env" >nul
  echo [!] Создан файл .env. Откройте его в Блокноте, вставьте BOT_TOKEN и сохраните.
  notepad ".env"
)

if exist "venv\.installed" goto run

echo Первый запуск: устанавливаю библиотеки из папки wheels, интернет для этого не нужен...
if exist "venv" rmdir /s /q "venv"
%PY% -m venv venv
if errorlevel 1 (
  echo [!] Не удалось создать окружение Python. Переустановите Python 3.13 с python.org.
  pause
  exit /b 1
)

venv\Scripts\python.exe -m pip install --disable-pip-version-check --no-index --find-links wheels -r requirements.txt
if errorlevel 1 (
  echo.
  echo Офлайн-установка не подошла к вашей версии Python, пробую скачать из интернета...
  venv\Scripts\python.exe -m pip install --disable-pip-version-check -r requirements.txt
  if errorlevel 1 (
    echo.
    echo [!] Не удалось установить библиотеки.
    echo     Если включён VPN — выключите его на время установки, или наоборот включите.
    if exist "venv" rmdir /s /q "venv"
    pause
    exit /b 1
  )
)
echo ok> "venv\.installed"
echo Библиотеки установлены.

:run
echo.
echo Запускаю бота. Чтобы остановить — закройте это окно или нажмите Ctrl+C.
echo.
venv\Scripts\python.exe -m bot.main
pause
