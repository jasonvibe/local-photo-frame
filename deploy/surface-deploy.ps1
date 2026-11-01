# Local Photo Frame Surface 部署器
# 用法: powershell -NoProfile -ExecutionPolicy Bypass -File surface-deploy.ps1 -ZipPath <压缩包路径> [-InstallDir <目录>] [-Firewall] [-AutoStart]
# 行为: 停止旧服务 → 备份并保留 images/recycle/settings → 覆盖代码 → 建 venv 装依赖 → 启动 → 自检
param(
  [Parameter(Mandatory = $true)][string]$ZipPath,
  [string]$InstallDir,
  [switch]$Firewall,
  [switch]$AutoStart
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$Port = 4444

Write-Host '== Local Photo Frame 部署器 =='
if (-not (Test-Path $ZipPath)) { throw "找不到压缩包：$ZipPath" }

# 1. 定位已有安装（优先沿用运行中服务的目录，保留照片数据）
if (-not $InstallDir) {
  $proc = Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
    Where-Object { $_.CommandLine -match 'server\.py' } | Select-Object -First 1
  if ($proc -and $proc.CommandLine -match '([A-Za-z]:\\.+server\.py)') {
    $InstallDir = Split-Path $Matches[1]
    Write-Host "从运行中的服务定位到安装目录：$InstallDir"
  }
}
if (-not $InstallDir) {
  foreach ($c in @("$HOME\local-photo-frame", "$HOME\Desktop\local-photo-frame", "$HOME\Documents\local-photo-frame", "$HOME\OneDrive\Desktop\local-photo-frame", 'C:\local-photo-frame')) {
    if (Test-Path (Join-Path $c 'app.py')) { $InstallDir = $c; break }
  }
}
if (-not $InstallDir) { $InstallDir = "$HOME\local-photo-frame" }
Write-Host "安装目录：$InstallDir"

# 2. 停止旧服务
Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
  Where-Object { $_.CommandLine -match 'server\.py' } | ForEach-Object {
    Write-Host "停止旧服务 (PID $($_.ProcessId))"
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
  }
Start-Sleep -Seconds 1

# 3. 解压并同步代码（数据文件先移出再放回，绝不覆盖照片）
$temp = Join-Path $env:TEMP ("photoframe-src-" + [guid]::NewGuid().ToString('N'))
Expand-Archive -Path $ZipPath -DestinationPath $temp -Force
$source = Get-ChildItem $temp |
  Where-Object { Test-Path (Join-Path $_.FullName 'app.py') } |
  Select-Object -First 1 -ExpandProperty FullName
if (-not $source) { throw '压缩包内没有找到 app.py，请确认是完整项目包' }

$preserve = @('images', 'thumbnails', 'recycle', 'settings.txt', 'display-backlight-state.json')
$backup = Join-Path $env:TEMP ("photoframe-data-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $backup | Out-Null
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
foreach ($item in $preserve) {
  $p = Join-Path $InstallDir $item
  if (Test-Path $p) { Move-Item $p (Join-Path $backup $item) -Force }
}
Copy-Item -Path (Join-Path $source '*') -Destination $InstallDir -Recurse -Force
foreach ($item in $preserve) {
  $b = Join-Path $backup $item
  if (Test-Path $b) { Move-Item $b (Join-Path $InstallDir $item) -Force }
}
Remove-Item $temp -Recurse -Force -ErrorAction SilentlyContinue
Write-Host '代码已更新（照片、回收站与设置保持原样）'

# 4. Python 与虚拟环境
$py = $null
foreach ($cmd in @('py', 'python3.13', 'python3.12', 'python3', 'python')) {
  $v = & $cmd --version 2>$null
  if ($LASTEXITCODE -eq 0 -and $v) { $py = $cmd; break }
}
if (-not $py) { throw '未找到 Python，请先安装 Python 3.10 或更新版本并勾选 pip' }
Write-Host "Python: $(& $py --version)"
$venvDir = Join-Path $InstallDir '.venv'
$venvPython = Join-Path $venvDir 'Scripts\python.exe'
if (-not (Test-Path $venvPython)) {
  & $py -m venv $venvDir
  if (-not (Test-Path $venvPython)) { throw '虚拟环境创建失败，请手动检查 Python 安装' }
}
& $venvPython -m pip install -q --upgrade pip 2>$null
& $venvPython -m pip install -q -r (Join-Path $InstallDir 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw '依赖安装失败，请检查网络或 Python 版本（项目要求 3.10+，推荐 3.13）' }
Write-Host '依赖安装完成'

# 5. 可选项：防火墙放行与开机自启
if ($Firewall) {
  try {
    netsh advfirewall firewall add rule name="Local Photo Frame" dir=in action=allow protocol=TCP localport=$Port profile=private | Out-Null
    Write-Host '防火墙已放行 4444 端口（仅专用网络）'
  } catch { Write-Host '防火墙配置失败（需要管理员权限），手机可能无法访问' }
}
if ($AutoStart) {
  try {
    $pyw = Join-Path $venvDir 'Scripts\pythonw.exe'
    $action = New-ScheduledTaskAction -Execute $pyw -Argument 'server.py --host 0.0.0.0 --port 4444' -WorkingDirectory $InstallDir
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    Register-ScheduledTask -TaskName 'LocalPhotoFrame' -Action $action -Trigger $trigger -Force | Out-Null
    Write-Host '已创建开机自启任务 LocalPhotoFrame'
  } catch { Write-Host '创建自启任务失败，可稍后手动配置任务计划程序' }
}

# 6. 启动服务
$log = Join-Path $InstallDir 'frame-server.log'
$err = Join-Path $InstallDir 'frame-server.err.log'
Start-Process -FilePath $venvPython -ArgumentList 'server.py', '--host', '0.0.0.0', '--port', "$Port" `
  -WorkingDirectory $InstallDir -WindowStyle Hidden `
  -RedirectStandardOutput $log -RedirectStandardError $err

# 7. 自检
Write-Host '等待服务启动…'
$started = $false
foreach ($i in 1..20) {
  Start-Sleep -Milliseconds 800
  try {
    $playlist = Invoke-RestMethod "http://127.0.0.1:$Port/api/playlist" -TimeoutSec 3
    $home = Invoke-WebRequest "http://127.0.0.1:$Port/" -TimeoutSec 3 -UseBasicParsing
    $upload = Invoke-WebRequest "http://127.0.0.1:$Port/upload" -TimeoutSec 3 -UseBasicParsing
    $videoReady = ($home.Content -match 'canplay') -and ($home.Content -match 'wakeVideos')
    $started = $videoReady -and ($upload.StatusCode -eq 200)
    if ($started) {
      Write-Host '== 部署完成，自检通过 =='
      Write-Host ("版本特征: 视频播放 " + $(if ($videoReady) { '已启用' } else { '未启用' }))
      Write-Host ("播放列表: " + @($playlist).Count + " 个媒体文件")
      Write-Host ("app.py SHA256: " + (Get-FileHash (Join-Path $InstallDir 'app.py') -Algorithm SHA256).Hash)
      $ips = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
        Select-Object -ExpandProperty IPAddress
      Write-Host '手机管理页（同一 WiFi）：'
      foreach ($ip in $ips) { Write-Host "  http://$ip`:$Port/upload" }
      Write-Host "播放页: http://localhost:$Port/"
      break
    }
  } catch { }
}
if (-not $started) {
  Write-Host '== 自检未通过 =='
  if (Test-Path $err) { Get-Content $err -Tail 10 }
  throw '服务启动失败，请把上方错误信息反馈给开发者'
}
