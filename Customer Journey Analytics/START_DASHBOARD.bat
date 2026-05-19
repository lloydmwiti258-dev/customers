@echo off
echo ============================================
echo   Customer Journey Analytics Dashboard
echo ============================================
echo.

:: Install dependencies
echo Installing dependencies...
pip install -r requirements.txt --quiet

echo.
echo Starting dashboard server...
echo Open your browser at: http://localhost:5000
echo Press CTRL+C to stop the server
echo.

python app.py
pause
