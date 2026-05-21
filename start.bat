@echo off
title Bot Discord Parrot
echo Demarrage du Bot Discord Parrot...

:: Verification de l'existence du fichier de configuration
if not exist config.json (
    echo Fichier config.json non trouve!
    echo Le bot va creer un fichier par defaut que vous devrez configurer.
    set /p REPLY="Continuer? (y/N): "
    if /I not "%REPLY%"=="Y" (
        if /I not "%REPLY%"=="y" (
            exit /b
        )
    )
)

:: Verification de l'environnement virtuel
if not exist .venv (
    echo Creation de l'environnement virtuel...
    python -m venv .venv
)

echo Activation de l'environnement virtuel...
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
echo Installation/mise a jour des dependances...
pip install -r requirements.txt

echo Lancement du bot...
python minecraft_bot.py
if %errorlevel% neq 0 (
    echo Le bot ne s'est pas lance correctement. Code erreur : %errorlevel%
) else (
    echo Le bot s'est lance avec succes.
)