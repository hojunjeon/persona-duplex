& "$PSScriptRoot\scripts\persona-duplex.ps1" @args
if (-not $?) { exit 1 }
if ($null -eq $LASTEXITCODE) { exit 0 }
exit $LASTEXITCODE
