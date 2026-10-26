@echo off
rem Runs inside the Windows installer once the packages and shortcuts are
rem installed into this version's folder (%PREFIX%). Any failure here fails
rem the installation. The same steps as macos\post_install.sh, which
rem explains each one; Windows needs no uninstaller of its own (the
rem installer writes one) and removes older versions before installing
rem (pre_install.bat).

set "PIP_CONFIG_FILE=NUL"
set "PYTHONNOUSERSITE=1"

echo Installing the app window library (pywebview)
"%PREFIX%\python.exe" -m pip install --no-index --no-deps --require-hashes ^
    --disable-pip-version-check --no-warn-script-location --quiet ^
    --find-links "%PREFIX%\wheels" -r "%PREFIX%\wheels\requirements.txt"
if errorlevel 1 exit /b 1
rmdir /s /q "%PREFIX%\wheels"

echo Compiling the app
"%PREFIX%\python.exe" -m compileall -q -j 0 "%PREFIX%\share\wikipedia-wrapped" > NUL
if errorlevel 1 exit /b 1

rem Load every library once, so that the operating system's first-load
rem checks (antivirus scanning on Windows) happen now rather than during the
rem first launch; see the macOS script. Only a speed-up, so a failure is
rem reported but does not fail the installation.
echo Preparing the app's libraries for their first use
set "NUMBA_CACHE_DIR=%TEMP%\wikipedia-wrapped-warmup"
pushd "%PREFIX%\share\wikipedia-wrapped"
"%PREFIX%\python.exe" -B -c "from app.selftest import check_imports; check_imports()"
set "WARMUP_RESULT=%ERRORLEVEL%"
popd
rmdir /s /q "%NUMBA_CACHE_DIR%" 2> NUL
if not "%WARMUP_RESULT%"=="0" echo Warning: could not preload the libraries; the first launch will be slower.
exit /b 0
