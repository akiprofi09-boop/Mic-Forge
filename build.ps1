$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    $pythonPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $pythonPath)) {
        $pythonPath = Join-Path (Split-Path $PSScriptRoot -Parent) '.venv\Scripts\python.exe'
    }
    & $pythonPath -m PyInstaller --noconfirm --windowed --onedir --name 'Mic Forge' --icon assets/mic-forge.ico --collect-data pyrnnoise --collect-binaries pyrnnoise --exclude-module matplotlib --exclude-module av --exclude-module audiolab main.py
    if ($LASTEXITCODE -ne 0) { throw 'Packaging failed.' }
    & $pythonPath collect_licenses.py
    if ($LASTEXITCODE -ne 0) { throw 'License collection failed.' }
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'README.md') -Destination (Join-Path $PSScriptRoot 'dist\Mic Forge\README.md')
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'THIRD_PARTY.md') -Destination (Join-Path $PSScriptRoot 'dist\Mic Forge\THIRD_PARTY.md')
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'VALIDATION.md') -Destination (Join-Path $PSScriptRoot 'dist\Mic Forge')
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'docs') -Destination (Join-Path $PSScriptRoot 'dist\Mic Forge') -Recurse -Force
    $sourcePath = Join-Path $PSScriptRoot 'dist\Mic Forge\source'
    New-Item -ItemType Directory -Path $sourcePath -Force | Out-Null
    # Keep machine setup scripts, backups and recordings out of public builds.
    $sourceFiles = @('main.py','audio.py','dsp.py','tone_pad.py','runtime_check.py',
        'bootstrap.py','collect_licenses.py','test_audio.py','test_ui.py',
        'build.ps1','launch.ps1','requirements.txt','VALIDATION.md')
    foreach ($sourceFile in $sourceFiles) {
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot $sourceFile) -Destination $sourcePath
    }
    $benchmarkSource = Join-Path $sourcePath 'benchmarks'
    New-Item -ItemType Directory -Path $benchmarkSource -Force | Out-Null
    Get-ChildItem -LiteralPath (Join-Path $PSScriptRoot 'benchmarks') -Filter '*.py' -File | Copy-Item -Destination $benchmarkSource
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'assets') -Destination $sourcePath -Recurse -Force
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'third_party') -Destination $sourcePath -Recurse -Force
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'README.md') -Destination $sourcePath
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'THIRD_PARTY.md') -Destination $sourcePath
    Write-Output (Join-Path $PSScriptRoot 'dist\Mic Forge\Mic Forge.exe')
}
finally { Pop-Location }
