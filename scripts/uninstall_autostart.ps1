# Removes the RecordTemperature scheduled task.
param([string]$TaskName = "RecordTemperature")
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "タスク '$TaskName' を削除しました。"
} else {
    Write-Host "タスク '$TaskName' は登録されていません。"
}
