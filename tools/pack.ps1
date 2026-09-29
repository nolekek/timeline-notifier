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
              '.pytest_cache', 'env')
$skipFiles = @('.env', "$projectName.zip", '*.pyc')

$stage = Join-Path ([System.IO.Path]::GetTempPath()) ("tl-pack-" + [guid]::NewGuid().ToString('N'))
$dest = Join-Path $stage $projectName
New-Item -ItemType Directory -Force -Path $dest | Out-Null

$copied = New-Object System.Collections.Generic.List[string]

Get-ChildItem -Path $root -Recurse -File -Force | ForEach-Object {
    $rel = $_.FullName.Substring($root.Length + 1)
    $parts = $rel -split '[\\/]'
    if ($parts | Where-Object { $skipDirs -contains $_ }) { return }
    if ($skipFiles -contains $parts[-1]) { return }

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

$leaks = $copied | Where-Object { $_ -match '(^|\\)\.env$|instance\\|venv\\|__pycache__' }
if ($leaks) {
    Write-Warning ("В архив попали лишние файлы: " + ($leaks -join ', '))
} else {
    Write-Host 'OK: секретов (.env), базы (instance/) и venv в архиве нет'
}
