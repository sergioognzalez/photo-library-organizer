$ErrorActionPreference = "Stop"

python -m pip install ".[build]"
if ($LASTEXITCODE -ne 0) {
    throw "No se pudieron instalar las dependencias de build."
}

python -m PyInstaller `
  --noconfirm `
  --windowed `
  --name "AppFoto" `
  --add-data "README.md;." `
  main.py

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller no pudo generar el ejecutable."
}

Write-Host "Ejecutable generado en dist\AppFoto\AppFoto.exe"
