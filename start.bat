@echo off
chcp 65001 > nul
title Antigravity Telegram Bridge & Live Voice
echo ================================================================
echo    Antigravity Telegram Bridge & Live Voice Supervisor
echo ================================================================
echo.

python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ОШИБКА] Python не найден в системе!
    echo Установите Python 3.10+ с сайта python.org и добавьте его в PATH.
    pause
    exit /b 1
)

if not exist ".env" (
    if exist ".env.example" (
        echo [*] Создаю файл настроек .env из шаблона .env.example...
        copy ".env.example" ".env" > nul
        echo [ВНИМАНИЕ] Откройте файл .env и укажите ваш токен бота TELEGRAM_BOT_TOKEN и GEMINI_API_KEY!
        echo.
        pause
    )
)

echo [*] Проверка зависимостей...
python -c "import aiogram, aiohttp, google.genai" >nul 2>&1
if %errorlevel% neq 0 (
    echo [*] Установка зависимостей из requirements.txt...
    pip install -r "%~dp0requirements.txt"
)

echo.
echo [*] Запуск Dual Supervisor (Bridge + Gemini Live)...
python "%~dp0supervisor.py"
if %errorlevel% neq 0 (
    echo.
    echo [ОШИБКА] Скрипт остановлен.
    pause
)
