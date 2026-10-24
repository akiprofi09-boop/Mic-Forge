$ErrorActionPreference = 'Stop'
$projectPython = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
$sharedPython = Join-Path (Split-Path $PSScriptRoot -Parent) '.venv\Scripts\pythonw.exe'
if (Test-Path -LiteralPath $projectPython) { $pythonPath = $projectPython }
elseif (Test-Path -LiteralPath $sharedPython) { $pythonPath = $sharedPython }
else { throw 'Run the setup instructions in README.md first.' }
Start-Process -FilePath $pythonPath -ArgumentList ('"' + (Join-Path $PSScriptRoot 'main.py') + '"') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
