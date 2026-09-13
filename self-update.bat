@echo off & chcp 65001 >nul & set "TSYNC_SELF=%~f0" & powershell -NoProfile -ExecutionPolicy Bypass -Command "$c = Get-Content -LiteralPath $env:TSYNC_SELF -Encoding UTF8; $i = [array]::IndexOf($c, '::PS'); iex ($c[($i + 1)..($c.Length - 1)] -join [char]10)" & echo. & pause & exit /b
::PS
# Everything below is PowerShell (the line above runs it and never lets cmd read further,
# so this file can safely replace itself). Installs or updates tsync from GitHub:
# downloads the repo as a zip (no git needed), copies only files that changed,
# never touches local data, then runs setup.ps1 (which only fixes what's missing).
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$repo = 'movsq/dont-lose-the-world'
$branch = 'main'
$protected = @('store', 'devices.json', 'platform-tools', '.git', '.claude_trash')

$self = $env:TSYNC_SELF
$here = Split-Path -Parent $self
$others = @(Get-ChildItem -LiteralPath $here -Force | Where-Object { $_.FullName -ne $self })
if ((Test-Path -LiteralPath (Join-Path $here 'tsync.py')) -or $others.Count -eq 0) { $dir = $here }
else { $dir = Join-Path $here 'dont-lose-the-world' }   # don't spill files onto e.g. the Desktop
$ok = $false

if (Test-Path -LiteralPath (Join-Path $dir '.git')) {
    Write-Host "Tohle je vývojová kopie (git). Aktualizuj ji přes 'git pull', self-update ji nepřepíše." -ForegroundColor Yellow
} else {
    $tmp = Join-Path $env:TEMP ('tsync-update-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $tmp | Out-Null
    try {
        $zip = Join-Path $tmp 'src.zip'
        if ($env:TSYNC_UPDATE_ZIP) { Copy-Item -LiteralPath $env:TSYNC_UPDATE_ZIP $zip }   # tests
        else {
            Write-Host "Stahuju nejnovější verzi z github.com/$repo ..."
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            Invoke-WebRequest -UseBasicParsing -OutFile $zip -Uri "https://github.com/$repo/archive/refs/heads/$branch.zip"
        }
        Expand-Archive -LiteralPath $zip -DestinationPath (Join-Path $tmp 'x')
        $src = @(Get-ChildItem -LiteralPath (Join-Path $tmp 'x') -Directory)[0].FullName
        if (-not (Test-Path -LiteralPath (Join-Path $src 'tsync.py'))) { throw 'stažený archiv neobsahuje tsync.py' }

        Write-Host "Složka: $dir"
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        $changed = 0
        foreach ($f in Get-ChildItem -LiteralPath $src -Recurse -File -Force) {
            $rel = $f.FullName.Substring($src.Length + 1)
            if ($protected -contains ($rel -split '[\\/]')[0]) { continue }
            $dst = Join-Path $dir $rel
            if ((Test-Path -LiteralPath $dst) -and
                (Get-FileHash -LiteralPath $dst).Hash -eq (Get-FileHash -LiteralPath $f.FullName).Hash) { continue }
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dst) | Out-Null
            Copy-Item -LiteralPath $f.FullName -Destination $dst -Force
            Write-Host "  aktualizováno: $rel"
            $changed++
        }
        if ($changed -eq 0) { Write-Host '  Všechno už je aktuální.' -ForegroundColor Green }
        else { Write-Host "  Aktualizováno souborů: $changed" -ForegroundColor Green }
        $ok = $true
    } catch {
        Write-Host "Aktualizace se nepovedla: $($_.Exception.Message)" -ForegroundColor Yellow
        Write-Host 'Zálohy (store) a nastavení zařízení zůstaly netknuté.'
    } finally {
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
}

if ($ok) {
    Write-Host ''
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $dir 'setup.ps1')
}
