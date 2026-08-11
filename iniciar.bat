@echo off
TITLE Meet Assistant Launcher

:: %~dp0 é uma variável mágica que pega a pasta onde este arquivo está
cd /d "%~dp0"

:: Verifica se o ambiente virtual existe
IF NOT EXIST "venv\Scripts\activate.bat" (
    echo [ERRO] Ambiente virtual nao encontrado!
    echo Por favor, rode: python -m venv venv
    pause
    exit
)

:: Ativa o ambiente e roda o app
echo Iniciando Meet Assistant...
call venv\Scripts\activate
python main.py

:: Se o app fechar com erro, o pause permite ler o que aconteceu
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo Ocorreu um erro na execucao.
    pause
)