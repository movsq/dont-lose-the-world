# Checks what tsync needs and installs only what is missing.
# Safe to run any number of times: when everything is present it changes nothing.
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'   # the progress bar makes downloads in PS 5.1 crawl
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Set-Location $PSScriptRoot
$needRestart = $false

function Ok($t)  { Write-Host "  [OK] $t" -ForegroundColor Green }
function Bad($t) { Write-Host "  [!!] $t" -ForegroundColor Yellow }
function Ask($q) {
    $a = Read-Host "$q [A/n]"
    return ($a -eq '' -or $a -match '^(a|ano|y|yes)$')
}
function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                [Environment]::GetEnvironmentVariable('Path', 'User')
}

function Find-Python {
    $cands = @()
    if (Get-Command py -ErrorAction SilentlyContinue) { $cands += , @('py', '-3') }
    if (Get-Command python -ErrorAction SilentlyContinue) { $cands += , @('python') }
    foreach ($c in $cands) {
        $exe = $c[0]; $rest = @($c | Select-Object -Skip 1)
        # The Microsoft Store "python" alias exits with 9009 instead of running.
        $v = & $exe @rest -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and "$v" -match '^3\.(\d+)$' -and [int]$Matches[1] -ge 9) {
            return @{ Exe = $exe; Args = $rest; Version = "$v" }
        }
    }
    return $null
}

function Find-Git {
    $g = Get-Command git -ErrorAction SilentlyContinue
    if ($g) { return $g.Source }
    foreach ($p in "$env:ProgramFiles\Git\cmd\git.exe", "$env:LOCALAPPDATA\Programs\Git\cmd\git.exe") {
        if (Test-Path $p) { return $p }
    }
    return $null
}

function Install-WithWinget($id, $name, $url) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Bad "winget tu není - nainstaluj $name ručně: $url"
        return
    }
    if (-not (Ask "      Nainstalovat $name (winget install $id)?")) { return }
    winget install --id $id --exact --source winget
    Refresh-Path
    $script:needRestart = $true
}

Write-Host "Kontroluju, co je potřeba pro sync savů...`n"

# --- Python
$py = Find-Python
if ($py) { Ok "Python $($py.Version)" }
else {
    Bad "Python 3 chybí"
    Install-WithWinget 'Python.Python.3.13' 'Python 3.13' 'https://www.python.org/downloads/'
    $py = Find-Python
    if ($py) { Ok "Python $($py.Version)" }
}

# --- Git
$git = Find-Git
if ($git) { Ok "Git ($git)" }
else {
    Bad "Git chybí"
    Install-WithWinget 'Git.Git' 'Git for Windows' 'https://git-scm.com/download/win'
    $git = Find-Git
    if ($git) { Ok "Git ($git)" }
}

# --- adb (Google platform-tools, kept inside this folder)
$pt = Join-Path $PSScriptRoot 'platform-tools'
$adbFiles = 'adb.exe', 'AdbWinApi.dll', 'AdbWinUsbApi.dll'
$adbOk = -not ($adbFiles | Where-Object { -not (Test-Path (Join-Path $pt $_)) })
if (-not $adbOk) {
    Bad "adb (Android platform-tools) chybí"
    if (Ask "      Stáhnout platform-tools od Googlu (asi 8 MB, dl.google.com)?") {
        try {
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            $zip = Join-Path $env:TEMP 'platform-tools-latest-windows.zip'
            Invoke-WebRequest -UseBasicParsing -OutFile $zip `
                -Uri 'https://dl.google.com/android/repository/platform-tools-latest-windows.zip'
            Expand-Archive -Path $zip -DestinationPath $PSScriptRoot -Force
            Remove-Item $zip
            $adbOk = -not ($adbFiles | Where-Object { -not (Test-Path (Join-Path $pt $_)) })
        } catch {
            Bad "Stažení se nepovedlo: $($_.Exception.Message)"
        }
    }
}
if ($adbOk) { Ok ("adb: " + (& (Join-Path $pt 'adb.exe') version | Select-Object -First 1)) }

# --- the rest (BlueStacks, devices, game, backups) is checked by tsync itself
Write-Host ""
if (-not ($py -and $git -and $adbOk)) {
    Write-Host "Něco pořád chybí (viz výše)." -ForegroundColor Yellow
    if ($needRestart) { Write-Host "Po instalaci zavři tohle okno a spusť setup.bat znovu." }
    exit 1
}
& $py.Exe @($py.Args) tsync.py --app toca doctor
exit $LASTEXITCODE
