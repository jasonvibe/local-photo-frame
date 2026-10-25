param([ValidateSet('on','off')][string]$Action)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$statePath = Join-Path $PSScriptRoot 'display-backlight-state.json'
$monitor = Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods
$brightness = Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness | Select-Object -First 1
if ($Action -eq 'off') {
    if (-not (Test-Path $statePath)) {
        $planText = powercfg /getactivescheme | Out-String
        $plan = [regex]::Match($planText, '[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}').Value
        if (-not $plan) { throw 'Power plan not found' }
        $values = [regex]::Matches((powercfg /query $plan SUB_VIDEO ADAPTBRIGHT | Out-String), '0x([0-9a-fA-F]+)')
        if ($values.Count -lt 2) { throw 'Adaptive brightness settings not found' }
        $state = @{brightness=[int]$brightness.CurrentBrightness;plan=$plan;
            ac=[Convert]::ToInt32($values[$values.Count-2].Groups[1].Value,16);
            dc=[Convert]::ToInt32($values[$values.Count-1].Groups[1].Value,16)}
        $state | ConvertTo-Json | Set-Content -Encoding UTF8 $statePath
    }
    $state = Get-Content -Raw $statePath | ConvertFrom-Json
    powercfg /setacvalueindex $state.plan SUB_VIDEO ADAPTBRIGHT 0 | Out-Null
    powercfg /setdcvalueindex $state.plan SUB_VIDEO ADAPTBRIGHT 0 | Out-Null
    powercfg /setactive $state.plan | Out-Null
    Invoke-CimMethod -InputObject $monitor -MethodName WmiSetALSBrightnessState -Arguments @{State=$false} -ErrorAction SilentlyContinue | Out-Null
    $result = Invoke-CimMethod -InputObject $monitor -MethodName WmiSetBrightness -Arguments @{Timeout=[uint32]0;Brightness=[byte]0}
    if ($null -ne $result.ReturnValue -and $result.ReturnValue -ne 0) { throw 'Cannot lower backlight' }
} else {
    if (Test-Path $statePath) {
        $state = Get-Content -Raw $statePath | ConvertFrom-Json
        $result = Invoke-CimMethod -InputObject $monitor -MethodName WmiSetBrightness -Arguments @{Timeout=[uint32]0;Brightness=[byte]$state.brightness}
        if ($null -ne $result.ReturnValue -and $result.ReturnValue -ne 0) { throw 'Cannot restore backlight' }
        powercfg /setacvalueindex $state.plan SUB_VIDEO ADAPTBRIGHT $state.ac | Out-Null
        powercfg /setdcvalueindex $state.plan SUB_VIDEO ADAPTBRIGHT $state.dc | Out-Null
        powercfg /setactive $state.plan | Out-Null
        Invoke-CimMethod -InputObject $monitor -MethodName WmiSetALSBrightnessState -Arguments @{State=[bool]($state.ac -or $state.dc)} -ErrorAction SilentlyContinue | Out-Null
        Remove-Item $statePath
    }
}
@{brightness=[int](Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness | Select-Object -First 1).CurrentBrightness} | ConvertTo-Json -Compress
