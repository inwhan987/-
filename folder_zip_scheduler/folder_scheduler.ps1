<#
    폴더 자동 압축/해제 스케줄러 (PowerShell 버전, 파이썬 불필요)

      - 저녁(기본 18:00): 지정한 폴더를 zip으로 백업 (원본 폴더는 유지)
      - 아침(기본 09:10): zip을 풀어서 폴더에 덮어쓰기

    사용법 (보통은 install.bat / uninstall.bat 을 더블클릭하면 됨):
        powershell -ExecutionPolicy Bypass -File folder_scheduler.ps1 install
        powershell -ExecutionPolicy Bypass -File folder_scheduler.ps1 uninstall
        powershell -ExecutionPolicy Bypass -File folder_scheduler.ps1 status
        powershell -ExecutionPolicy Bypass -File folder_scheduler.ps1 zip
        powershell -ExecutionPolicy Bypass -File folder_scheduler.ps1 unzip

    설정은 스크립트 옆의 folder_scheduler.ini 에서 읽는다.
#>

param(
    [Parameter(Position = 0)]
    [ValidateSet('zip', 'unzip', 'install', 'uninstall', 'status', 'help')]
    [string]$Action = 'status'
)

$ErrorActionPreference = 'Stop'

$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$ConfigPath = Join-Path $ScriptDir 'folder_scheduler.ini'
$LogPath    = Join-Path $ScriptDir 'folder_scheduler.log'

$TaskZip   = 'FolderZipScheduler_Zip'
$TaskUnzip = 'FolderZipScheduler_Unzip'

function Write-Log([string]$Message) {
    $line = "[{0:yyyy-MM-dd HH:mm:ss}] {1}" -f (Get-Date), $Message
    Write-Host $line
    try { Add-Content -Path $LogPath -Value $line -Encoding UTF8 } catch { }
}

function New-DefaultConfig {
@"
[settings]
; 압축할 대상 폴더 (필수). 예: C:\Users\me\Documents\work
target_folder = C:\변경하세요\대상폴더

; 만들 zip 파일 경로. 비우면 대상폴더 옆에 <폴더이름>.zip 으로 생성
zip_path =

; 압축 시각 (24시간, HH:MM)
zip_time = 18:00

; 해제 시각 (24시간, HH:MM)
unzip_time = 09:10
"@ | Set-Content -Path $ConfigPath -Encoding UTF8
}

function Get-Config {
    if (-not (Test-Path $ConfigPath)) {
        New-DefaultConfig
        Write-Log "설정 파일을 새로 만들었습니다: $ConfigPath"
        Write-Log "target_folder 값을 실제 폴더 경로로 수정한 뒤 다시 실행하세요."
        return $null
    }

    $cfg = @{}
    foreach ($raw in Get-Content -Path $ConfigPath -Encoding UTF8) {
        $line = $raw.Trim()
        if ($line -eq '' -or $line.StartsWith(';') -or $line.StartsWith('[')) { continue }
        $idx = $line.IndexOf('=')
        if ($idx -lt 1) { continue }
        $key = $line.Substring(0, $idx).Trim()
        $val = $line.Substring($idx + 1).Trim()
        $cfg[$key] = $val
    }

    $target = $cfg['target_folder']
    if ([string]::IsNullOrWhiteSpace($target) -or $target -like '*변경하세요*') {
        Write-Log "설정의 target_folder 값을 실제 폴더 경로로 지정하세요: $ConfigPath"
        return $null
    }
    $target = $target.TrimEnd('\')

    $zipPath = $cfg['zip_path']
    if ([string]::IsNullOrWhiteSpace($zipPath)) {
        $parent = Split-Path -Parent $target
        $name   = Split-Path -Leaf   $target
        $zipPath = Join-Path $parent ($name + '.zip')
    }

    $zipTime   = if ($cfg.ContainsKey('zip_time'))   { $cfg['zip_time'] }   else { '18:00' }
    $unzipTime = if ($cfg.ContainsKey('unzip_time')) { $cfg['unzip_time'] } else { '09:10' }

    return [pscustomobject]@{
        Target    = $target
        ZipPath   = $zipPath
        ZipTime   = $zipTime
        UnzipTime = $unzipTime
    }
}

function Invoke-Zip($cfg) {
    if (-not (Test-Path -LiteralPath $cfg.Target -PathType Container)) {
        Write-Log "대상 폴더가 없습니다: $($cfg.Target)"
        return 1
    }

    $tmp = $cfg.ZipPath + '.tmp'
    if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force }

    $zipDir = Split-Path -Parent $cfg.ZipPath
    if ($zipDir -and -not (Test-Path -LiteralPath $zipDir)) {
        New-Item -ItemType Directory -Path $zipDir -Force | Out-Null
    }

    try {
        # 폴더 자체를 지정하면 zip 루트에 폴더 이름이 포함됨 -> 해제 시 폴더가 그대로 복원
        Compress-Archive -Path $cfg.Target -DestinationPath $tmp -CompressionLevel Optimal -Force
        if (Test-Path -LiteralPath $cfg.ZipPath) { Remove-Item -LiteralPath $cfg.ZipPath -Force }
        Move-Item -LiteralPath $tmp -Destination $cfg.ZipPath -Force
    } catch {
        Write-Log "압축 실패: $($_.Exception.Message)"
        if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue }
        return 1
    }

    $mb = [math]::Round((Get-Item -LiteralPath $cfg.ZipPath).Length / 1MB, 1)
    Write-Log "압축 완료: '$(Split-Path -Leaf $cfg.Target)' -> $($cfg.ZipPath) ($mb MB) / 원본 유지"
    return 0
}

function Invoke-Unzip($cfg) {
    if (-not (Test-Path -LiteralPath $cfg.ZipPath -PathType Leaf)) {
        Write-Log "압축 파일이 없습니다: $($cfg.ZipPath)"
        return 1
    }
    $parent = Split-Path -Parent $cfg.Target
    if (-not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }

    try {
        Expand-Archive -Path $cfg.ZipPath -DestinationPath $parent -Force
    } catch {
        Write-Log "해제 실패: $($_.Exception.Message)"
        return 1
    }

    Write-Log "해제 완료: $($cfg.ZipPath) -> $parent (폴더 '$(Split-Path -Leaf $cfg.Target)' 덮어쓰기)"
    return 0
}

function Parse-Time([string]$hhmm, [string]$fallback) {
    try   { return [datetime]::ParseExact($hhmm, 'HH:mm', $null) }
    catch { return [datetime]::ParseExact($fallback, 'HH:mm', $null) }
}

function Register-OneTask([string]$name, [string]$actionArg, [string]$hhmm, [string]$fallback) {
    $exe  = Join-Path $PSHOME 'powershell.exe'
    $scriptFile = Join-Path $ScriptDir 'folder_scheduler.ps1'
    $psArgs = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" {1}' -f $scriptFile, $actionArg

    $taskAction  = New-ScheduledTaskAction -Execute $exe -Argument $psArgs
    $trigger     = New-ScheduledTaskTrigger -Daily -At (Parse-Time $hhmm $fallback)
    $settings    = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

    Register-ScheduledTask -TaskName $name -Action $taskAction -Trigger $trigger `
        -Settings $settings -Description 'folder_scheduler' -Force | Out-Null
    Write-Log "작업 등록: $name (매일 $hhmm, $actionArg)"
}

function Invoke-Install($cfg) {
    try {
        Register-OneTask $TaskZip   'zip'   $cfg.ZipTime   '18:00'
        Register-OneTask $TaskUnzip 'unzip' $cfg.UnzipTime '09:10'
        Write-Log "설치 완료. 이제 매일 지정 시각에 자동으로 압축/해제됩니다."
        return 0
    } catch {
        Write-Log "작업 등록 실패: $($_.Exception.Message)"
        Write-Log "install.bat 을 마우스 오른쪽 -> '관리자 권한으로 실행' 해 보세요."
        return 1
    }
}

function Invoke-Uninstall {
    $ok = $true
    foreach ($name in @($TaskZip, $TaskUnzip)) {
        try {
            if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
                Unregister-ScheduledTask -TaskName $name -Confirm:$false
                Write-Log "작업 삭제: $name"
            } else {
                Write-Log "작업 없음(건너뜀): $name"
            }
        } catch {
            $ok = $false
            Write-Log "작업 삭제 실패: $name -> $($_.Exception.Message)"
        }
    }
    if ($ok) { return 0 } else { return 1 }
}

function Invoke-Status($cfg) {
    Write-Log "설정 파일: $ConfigPath"
    Write-Log "대상 폴더: $($cfg.Target)"
    Write-Log "zip 경로 : $($cfg.ZipPath)"
    Write-Log "압축 시각: 매일 $($cfg.ZipTime)"
    Write-Log "해제 시각: 매일 $($cfg.UnzipTime)"
    foreach ($name in @($TaskZip, $TaskUnzip)) {
        $t = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        $state = if ($t) { "등록됨 ($($t.State))" } else { "미등록" }
        Write-Log "스케줄러 작업 ${name}: $state"
    }
    return 0
}

# ----- 진입점 -----
if ($Action -eq 'help') {
    Write-Host @"
사용법: folder_scheduler.ps1 <명령>
  install    작업 스케줄러에 압축/해제 등록
  uninstall  등록한 작업 삭제
  status     설정/작업 상태 확인
  zip        지금 바로 압축
  unzip      지금 바로 해제
"@
    exit 0
}

$config = Get-Config
if ($null -eq $config) { exit 1 }

switch ($Action) {
    'zip'       { exit (Invoke-Zip       $config) }
    'unzip'     { exit (Invoke-Unzip     $config) }
    'install'   { exit (Invoke-Install   $config) }
    'uninstall' { exit (Invoke-Uninstall) }
    'status'    { exit (Invoke-Status    $config) }
    default     { exit (Invoke-Status    $config) }
}
