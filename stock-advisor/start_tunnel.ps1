# ============================================================================
#  微信公众号 WeRSS 隧道（本地 stock-advisor -> 远程服务器）
# ============================================================================
#  为什么需要它：远程服务器的 8001 端口被腾讯云安全组挡住，直连连不上。
#  这里借用已经放通的 22 端口做端口转发，不额外暴露任何端口。
#
#  用法：
#     .\start_tunnel.ps1              建立隧道（后台运行）
#     .\start_tunnel.ps1 -Status      查看状态
#     .\start_tunnel.ps1 -Stop        断开隧道
# ============================================================================
param(
  [switch]$Stop,
  [switch]$Status
)

$Remote   = "root@101.43.25.101"
$LocalPrt = 8001
$Key      = "$env:USERPROFILE\.ssh\sa_deploy_ed25519"

if ($Status) {
  $t = Test-NetConnection -ComputerName 127.0.0.1 -Port $LocalPrt -WarningAction SilentlyContinue
  Write-Host ("本地 {0} 监听: {1}" -f $LocalPrt, $t.TcpTestSucceeded)
  Get-CimInstance Win32_Process -Filter "Name='ssh.exe'" |
    Where-Object { $_.CommandLine -like "*$LocalPrt`:127.0.0.1:$LocalPrt*" } |
    ForEach-Object { Write-Host ("  隧道进程 pid={0}" -f $_.ProcessId) }
  exit 0
}

if ($Stop) {
  $procs = Get-CimInstance Win32_Process -Filter "Name='ssh.exe'" |
           Where-Object { $_.CommandLine -like "*$LocalPrt`:127.0.0.1:$LocalPrt*" }
  if (-not $procs) { Write-Host "没有正在运行的隧道"; exit 0 }
  $procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Host "已断开隧道 pid=$($_.ProcessId)" }
  exit 0
}

# 先清掉旧的，避免端口冲突
Get-CimInstance Win32_Process -Filter "Name='ssh.exe'" |
  Where-Object { $_.CommandLine -like "*$LocalPrt`:127.0.0.1:$LocalPrt*" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

if (-not (Test-Path $Key)) {
  Write-Host "找不到密钥 $Key" -ForegroundColor Red
  Write-Host "先执行：ssh-keygen -t ed25519 -f `"$Key`" -N `"`" -C `"stock-advisor@we-mp-rss-tunnel`""
  exit 1
}

$p = Start-Process -FilePath "ssh.exe" -PassThru -WindowStyle Hidden -ArgumentList @(
  "-N",
  "-L", "$LocalPrt`":127.0.0.1:$LocalPrt",
  "-i", $Key,
  "-o", "StrictHostKeyChecking=accept-new",
  "-o", "BatchMode=yes",
  "-o", "ServerAliveInterval=30",
  "-o", "ServerAliveCountMax=3",
  "-o", "ExitOnForwardFailure=yes",
  $Remote
)

Start-Sleep -Seconds 4
$t = Test-NetConnection -ComputerName 127.0.0.1 -Port $LocalPrt -WarningAction SilentlyContinue
if ($t.TcpTestSucceeded) {
  Write-Host "隧道已建立  pid=$($p.Id)" -ForegroundColor Green
  Write-Host "  本地 http://127.0.0.1:$LocalPrt  ->  远程 $Remote`:$LocalPrt"
  Write-Host "  stock-advisor 的 mp.base_url 填 http://127.0.0.1:$LocalPrt"
} else {
  Write-Host "隧道启动失败，先手动排查：ssh -v -N -L $LocalPrt`:127.0.0.1:$LocalPrt $Remote" -ForegroundColor Red
  exit 1
}
