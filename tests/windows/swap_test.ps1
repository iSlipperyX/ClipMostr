# Tests the updater's install step (updater.SWAP_SCRIPT) on Windows, against the things that
# broke real updates: File Explorer open on the app folder, programs whose working folder is
# inside it, a file that stays locked, your own files kept in the app folder.
#
#   python -c "from ytclip.updater import SWAP_SCRIPT as s; open('apply-update.ps1','w',encoding='utf-8-sig').write(s)"
#   powershell -NoProfile -ExecutionPolicy Bypass -File tests\windows\swap_test.ps1 -Swap .\apply-update.ps1
#
# Everything happens in a temporary folder, which is removed at the end.

param([Parameter(Mandatory)][string]$Swap, [switch]$NoExplorer)
$ErrorActionPreference = 'Stop'
$Swap = (Resolve-Path -LiteralPath $Swap).Path
$base = Join-Path $env:TEMP ("cm-swap-test-" + (Get-Random))
$failures = 0

function Write-Build([string]$dir, [string]$tag) {
  foreach ($d in '_internal\sub\deep', '_internal\held') { [void][IO.Directory]::CreateDirectory((Join-Path $dir $d)) }
  Set-Content -LiteralPath (Join-Path $dir 'ClipMonster.exe') -Value "exe $tag"
  Set-Content -LiteralPath (Join-Path $dir 'ytclip.exe') -Value "cli $tag"
  Set-Content -LiteralPath (Join-Path $dir '_internal\a.dll') -Value "dll $tag"
  Set-Content -LiteralPath (Join-Path $dir '_internal\sub\deep\b.pyd') -Value "pyd $tag"
  Set-Content -LiteralPath (Join-Path $dir '_internal\held\c.dat') -Value "dat $tag"
  Set-Content -LiteralPath (Join-Path $dir "_internal\only-$tag.txt") -Value $tag
}

function New-Case([string]$name) {
  $root = Join-Path $base $name
  $app = Join-Path $root 'ClipMonster'
  Write-Build $app 'old'
  [void][IO.Directory]::CreateDirectory((Join-Path $app 'my-stuff'))
  Set-Content -LiteralPath (Join-Path $app 'my-stuff\notes.txt') -Value 'keep me'
  $new = Join-Path $root '.cm-up\ClipMonster'
  Write-Build $new 'new'
  @{ Root = $root; App = $app; New = $new; Log = (Join-Path $root 'update.log') }
}

function Exited-Pid {
  $p = Start-Process cmd.exe -ArgumentList '/c', 'exit' -WindowStyle Hidden -PassThru
  $p.WaitForExit()
  $p.Id
}

function Run-Swap($case, [int]$waitPid) {
  & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $Swap -WaitPid $waitPid `
    -NewDir $case.New -InstallDir $case.App -Relaunch '' -LogFile $case.Log
}

function Read([string]$path) { if (Test-Path -LiteralPath $path) { (Get-Content -LiteralPath $path -Raw).Trim() } else { '<missing>' } }

function Expect([string]$case, [string]$what, $actual, $expected) {
  if ("$actual" -ne "$expected") { $script:failures++; Write-Host "  FAIL $case - ${what}: got '$actual', expected '$expected'" -ForegroundColor Red }
}

function Check-Updated($c, [string]$name) {
  Expect $name 'exe' (Read (Join-Path $c.App 'ClipMonster.exe')) 'exe new'
  Expect $name 'cli' (Read (Join-Path $c.App 'ytclip.exe')) 'cli new'
  Expect $name 'dll' (Read (Join-Path $c.App '_internal\a.dll')) 'dll new'
  Expect $name 'deep' (Read (Join-Path $c.App '_internal\sub\deep\b.pyd')) 'pyd new'
  Expect $name 'new-only file' (Read (Join-Path $c.App '_internal\only-new.txt')) 'new'
  Expect $name 'old-only file gone' (Test-Path -LiteralPath (Join-Path $c.App '_internal\only-old.txt')) $false
  Expect $name 'your files kept' (Read (Join-Path $c.App 'my-stuff\notes.txt')) 'keep me'
  Expect $name 'backup removed' (@(Get-ChildItem -LiteralPath $c.App -Force -Filter '.cm-old*').Count) 0
  Expect $name 'log' ((Read $c.Log) -match 'Update finished') $true
}

function Check-Untouched($c, [string]$name) {
  Expect $name 'exe' (Read (Join-Path $c.App 'ClipMonster.exe')) 'exe old'
  Expect $name 'cli' (Read (Join-Path $c.App 'ytclip.exe')) 'cli old'
  Expect $name 'dll' (Read (Join-Path $c.App '_internal\a.dll')) 'dll old'
  Expect $name 'deep' (Read (Join-Path $c.App '_internal\sub\deep\b.pyd')) 'pyd old'
  Expect $name 'old-only file' (Read (Join-Path $c.App '_internal\only-old.txt')) 'old'
  Expect $name 'no new files' (Test-Path -LiteralPath (Join-Path $c.App '_internal\only-new.txt')) $false
  Expect $name 'your files kept' (Read (Join-Path $c.App 'my-stuff\notes.txt')) 'keep me'
  Expect $name 'log' ((Read $c.Log) -match 'Previous version restored') $true
}

function Case([string]$name, [scriptblock]$body) {
  Write-Host "== $name"
  $before = $script:failures
  $sw = [Diagnostics.Stopwatch]::StartNew()
  try { & $body } catch { $script:failures++; Write-Host "  FAIL $name - threw: $($_.Exception.Message)" -ForegroundColor Red }
  $result = if ($script:failures -eq $before) { 'ok' } else { 'FAILED' }
  Write-Host ("  {0} in {1:n1} s" -f $result, $sw.Elapsed.TotalSeconds)
}

try {
  Case 'plain update' {
    $c = New-Case 'plain'
    Run-Swap $c (Exited-Pid)
    Check-Updated $c 'plain update'
  }

  Case 'waits for the app to close' {
    $c = New-Case 'wait'
    $p = Start-Process powershell.exe -ArgumentList '-NoProfile', '-Command', 'Start-Sleep -Seconds 4' -WindowStyle Hidden -PassThru
    Run-Swap $c $p.Id
    Check-Updated $c 'waits for the app to close'
  }

  Case 'folders held open by terminals and File Explorer' {
    $c = New-Case 'held'
    $holders = @(
      (Start-Process cmd.exe -WorkingDirectory $c.App -ArgumentList '/c', 'ping -n 120 127.0.0.1 >nul' -WindowStyle Hidden -PassThru),
      (Start-Process cmd.exe -WorkingDirectory (Join-Path $c.App '_internal\held') -ArgumentList '/c', 'ping -n 120 127.0.0.1 >nul' -WindowStyle Hidden -PassThru))
    $shell = New-Object -ComObject Shell.Application
    if (-not $NoExplorer) {
      Start-Process explorer.exe -ArgumentList "`"$($c.App)`""
      Start-Process explorer.exe -ArgumentList "`"$(Join-Path $c.App '_internal')`""
      Start-Sleep -Seconds 3
    }
    try {
      Run-Swap $c (Exited-Pid)
      Check-Updated $c 'folders held open'
      Expect 'folders held open' 'held folder still in place' (Test-Path -LiteralPath (Join-Path $c.App '_internal\held')) $true
    } finally {
      $holders | ForEach-Object { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue }
      foreach ($w in @($shell.Windows())) {
        try { if ($w.Document.Folder.Self.Path -like "$base*") { $w.Quit() } } catch { }
      }
    }
  }

  Case 'a locked file rolls everything back' {
    $c = New-Case 'locked'
    $lock = [IO.File]::Open((Join-Path $c.App '_internal\sub\deep\b.pyd'), 'Open', 'Read', 'None')
    try {
      Run-Swap $c (Exited-Pid)
    } finally { $lock.Close() }
    Check-Untouched $c 'locked file'
    Expect 'locked file' 'reason logged' ((Read $c.Log) -match 'Update failed: .*b\.pyd') $true
  }

  Case 'a leftover backup from an earlier run' {
    $c = New-Case 'leftover'
    Write-Build (Join-Path $c.App '.cm-old') 'stale'
    Run-Swap $c (Exited-Pid)
    Check-Updated $c 'leftover backup'
  }
} finally {
  Start-Sleep -Seconds 1
  Remove-Item -LiteralPath $base -Recurse -Force -ErrorAction SilentlyContinue
}

if ($failures) { Write-Host "$failures check(s) failed" -ForegroundColor Red; exit 1 }
Write-Host 'All swap tests passed' -ForegroundColor Green
