# Собирает чистый архив проекта для передачи Андрею/на сервер.
#
#   powershell -ExecutionPolicy Bypass -File tools\pack.ps1
#   powershell -ExecutionPolicy Bypass -File tools\pack.ps1 -OutPath D:\timeline-notifier.zip
#
# В архив НЕ попадают: venv/, instance/ (база и debug-дампы), .env (секреты),
# __pycache__/, .git/, сам архив. Внутри должна лежать только папка timeline-notifier
# с кодом, docs/, deploy/, tests/, README.md, requirements.txt, .env.example, .gitignore.

param(
    [string]$OutPath = ''
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$projectName = Split-Path -Leaf $root
if (-not $OutPath) {
    $OutPath = Join-Path (Split-Path -Parent $root) "$projectName.zip"
}

$skipDirs = @('venv', '.venv', 'instance', '__pycache__', '.git', '.idea', '.vscode',
              '.pytest_cache', 'env', '.dsh')
$skipFiles = @('.env', "$projectName.zip", '*.pyc', 'server.env', 'adminpw.txt')

# Хвосты, которые нужны ТОЛЬКО на этой машине и в архиве для Андрея лишние:
# tools/bin/ — шимы docker + lazydocker.exe (12.7 МБ!), tools/wsl-*.sh — обвязка WSL,
# docs/docker-wsl-local.md — инструкция про локальный WSL, на сервере не нужна.
$skipRelPrefixes = @('tools\bin\', 'tools\wsl-')
$skipRelFiles = @('docs\docker-wsl-local.md')

$stage = Join-Path ([System.IO.Path]::GetTempPath()) ("tl-pack-" + [guid]::NewGuid().ToString('N'))
$dest = Join-Path $stage $projectName
New-Item -ItemType Directory -Force -Path $dest | Out-Null

$copied = New-Object System.Collections.Generic.List[string]
$localOnly = New-Object System.Collections.Generic.List[string]

Get-ChildItem -Path $root -Recurse -File -Force | ForEach-Object {
    $rel = $_.FullName.Substring($root.Length + 1)
    $parts = $rel -split '[\\/]'
    if ($parts | Where-Object { $skipDirs -contains $_ }) { return }
    # -like понимает wildcard'ы (*.pyc); -contains сравнивал бы буквально
    if ($skipFiles | Where-Object { $parts[-1] -like $_ }) { return }
    if ($skipRelFiles -contains $rel) { $localOnly.Add($rel); return }
    $skipThis = $false
    foreach ($prefix in $skipRelPrefixes) {
        if ($rel.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            $skipThis = $true
            break
        }
    }
    if ($skipThis) { $localOnly.Add($rel); return }

    $target = Join-Path $dest $rel
    New-Item -ItemType Directory -Force -Path (Split-Path $target) | Out-Null
    Copy-Item -LiteralPath $_.FullName -Destination $target -Force
    $copied.Add($rel)
}

if (Test-Path $OutPath) { Remove-Item -LiteralPath $OutPath -Force }
Compress-Archive -Path $dest -DestinationPath $OutPath -CompressionLevel Optimal
Remove-Item -LiteralPath $stage -Recurse -Force

Write-Host "Архив: $OutPath"
Write-Host ("Файлов внутри: {0} ({1:N1} КБ)" -f $copied.Count,
            ((Get-Item $OutPath).Length / 1KB))

if ($localOnly.Count) {
    Write-Host ("Пропущено (нужно только на этой машине): {0}" -f ($localOnly -join ', '))
}

$leaks = $copied | Where-Object {
    $_ -match '(^|\\)\.env$' -or
    $_ -match '(^|\\)instance\\' -or
    $_ -match '(^|\\)venv\\' -or
    $_ -match '__pycache__' -or
    $_ -match '\.db$' -or
    $_ -match 'lazydocker\.exe$' -or
    $_ -match 'server\.env$' -or
    $_ -match 'adminpw\.txt$'
}
if ($leaks) {
    Write-Warning "Extra files found in archive: $($leaks -join ', ')"
} else {
    Write-Host 'OK: no secrets, no db, no instance, no venv, no lazydocker in archive'
}
