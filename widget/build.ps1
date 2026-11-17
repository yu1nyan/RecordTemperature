# ウィジェット提供アプリをビルドして、このPCに開発用として登録する（開発者モードが必要）。
#   powershell -ExecutionPolicy Bypass -File widget\build.ps1
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$publish = Join-Path $root "bin\publish"
$layout = Join-Path $root "bin\layout"

Write-Host "ビルド中..."
dotnet publish (Join-Path $root "RecordTemperatureWidget.csproj") -c Release -o $publish --nologo
if ($LASTEXITCODE -ne 0) { throw "ビルドに失敗しました" }

# 動いているウィジェット提供アプリを止めてから、登録用のフォルダを作り直す
Get-Process RecordTemperatureWidget -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Milliseconds 500
if (Test-Path $layout) { Remove-Item $layout -Recurse -Force }
New-Item $layout -ItemType Directory | Out-Null
Copy-Item (Join-Path $publish "*") $layout -Recurse
Copy-Item (Join-Path $root "Package\AppxManifest.xml") $layout
Copy-Item (Join-Path $root "Assets") (Join-Path $layout "Assets") -Recurse
New-Item (Join-Path $layout "Public") -ItemType Directory | Out-Null

Write-Host "登録中..."
Add-AppxPackage -Register (Join-Path $layout "AppxManifest.xml") -ForceApplicationShutdown
$pkg = Get-AppxPackage -Name "yu1nyan.RecordTemperatureWidget"
Write-Host "登録しました: $($pkg.PackageFullName)"
Write-Host "Win+W でウィジェットボードを開き、「+」から「温湿度記録」を追加してください。"
