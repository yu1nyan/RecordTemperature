# ウィジェット提供アプリの登録を解除する。
#   powershell -ExecutionPolicy Bypass -File widget\uninstall.ps1
$ErrorActionPreference = "Stop"
Get-Process RecordTemperatureWidget -ErrorAction SilentlyContinue | Stop-Process -Force
$pkg = Get-AppxPackage -Name "yu1nyan.RecordTemperatureWidget"
if ($pkg) {
    Remove-AppxPackage -Package $pkg.PackageFullName
    Write-Host "登録を解除しました: $($pkg.PackageFullName)"
} else {
    Write-Host "登録されていません"
}
