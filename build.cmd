@echo off
rem rebuild single-file exe (run in this folder)
cd /d "%~dp0"
python -m PyInstaller --onefile --noconsole --clean --name NiuNiuWatch --icon "%~dp0_icon.ico" --add-data "%~dp0dashboard.html;." --distpath _build\dist --workpath _build\work --specpath _build serve.py || exit /b 1
python -c "import shutil; shutil.copyfile(r'_build\dist\NiuNiuWatch.exe', '\u4eca\u5929\u725b\u725b\u9171\u6295\u5582\u4e86\u6ca1.exe')" || exit /b 1
echo build ok
