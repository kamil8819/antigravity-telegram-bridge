@echo off
chcp 65001 > nul
title Antigravity Telegram Bridge
echo ================================================================
echo    Antigravity Telegram Bridge - Auto-start
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
    if not exist "config.json" (
        if exist ".env.example" (
            echo [*] Создаю файл настроек .env из шаблона .env.example...
            copy ".env.example" ".env" > nul
            echo [ВНИМАНИЕ] Откройте файл .env и вставьте ваш токен бота TELEGRAM_BOT_TOKEN!
            echo.
            pause
        )
    )
)

echo [*] Проверка зависимостей...
python -c "import aiogram, aiohttp" >nul 2>&1
if %errorlevel% neq 0 (
    echo [*] Установка зависимостей из requirements.txt...
    pip install -r "%~dp0requirements.txt"
)

echo.
echo [*] Запуск моста...
python "%~dp0bridge.py"
if %errorlevel% neq 0 (
    echo.
    echo [ОШИБКА] Скрипт остановлен.
    pause
)
