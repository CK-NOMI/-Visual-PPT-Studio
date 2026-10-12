@echo off
cd /d %~dp0
if not exist .venv python -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install -r requirements.txt
if /I "%PPT_RENDERER%"=="node" (
  cd renderer
  call npm install
  cd ..
)
python run.py
