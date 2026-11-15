# Registers a Task Scheduler task that starts RecordTemperature
#   - at user logon (PC startup)
#   - when the PC resumes from sleep / hibernation (safety net if the process died)
# A named mutex in the app guarantees only one instance runs.
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1
param([string]$TaskName = "RecordTemperature")

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) { throw "仮想環境が見つかりません: $pythonw  (README のセットアップ手順を先に実行してください)" }

$user = "$env:USERDOMAIN\$env:USERNAME"
$esc = { param($s) [System.Security.SecurityElement]::Escape($s) }

$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>温湿度計 7セグ読み取り記録アプリ (RecordTemperature)</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>$(& $esc $user)</UserId>
      <Delay>PT30S</Delay>
    </LogonTrigger>
    <EventTrigger>
      <Enabled>true</Enabled>
      <Subscription>&lt;QueryList&gt;&lt;Query Id="0" Path="System"&gt;&lt;Select Path="System"&gt;*[System[Provider[@Name='Microsoft-Windows-Power-Troubleshooter'] and EventID=1]]&lt;/Select&gt;&lt;/Query&gt;&lt;/QueryList&gt;</Subscription>
      <Delay>PT20S</Delay>
    </EventTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$(& $esc $user)</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>$(& $esc $pythonw)</Command>
      <Arguments>-m recordtemp</Arguments>
      <WorkingDirectory>$(& $esc $root)</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@

Register-ScheduledTask -TaskName $TaskName -Xml $xml -Force | Out-Null
Write-Host "タスク '$TaskName' を登録しました（ログオン時・スリープ復帰時に起動）。"
Write-Host "今すぐ起動するには:  Start-ScheduledTask -TaskName $TaskName"
