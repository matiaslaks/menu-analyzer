@echo off
echo.
echo  =====================================================
echo   Menu Analyzer — powered by Claude AI
echo  =====================================================
echo.
echo  Instalando dependencias...
pip install -r requirements.txt -q

echo.
echo  Iniciando servidor + tunel publico (ngrok)...
echo.
echo  IMPORTANTE: Para que la URL publica funcione necesitas:
echo    1. Cuenta gratis en https://ngrok.com
echo    2. Ejecutar UNA SOLA VEZ:
echo       set NGROK_AUTHTOKEN=tu_token_aqui
echo       (o ngrok config add-authtoken tu_token_aqui)
echo.
echo  La URL publica aparecera en pantalla al iniciar.
echo  =====================================================
echo.
python server.py
pause
