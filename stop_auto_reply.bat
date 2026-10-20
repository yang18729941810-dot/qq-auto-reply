@echo off
chcp 65001 >nul
echo Stopping QQ auto-reply (pythonw running auto_reply.py)...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$status = Join-Path '%~dp0' 'auto_reply_status.json'; $proc = $null; if (Test-Path $status) { try { $want = (Get-Content $status -Raw -Encoding UTF8 | ConvertFrom-Json).pid } catch { $want = 0 }; if ($want) { $c = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $want) -ErrorAction SilentlyContinue; if ($c -and $c.Name -like 'python*' -and $c.CommandLine -like '*auto_reply.py*') { $proc = @($c) } } }; if (-not $proc) { Write-Host '  status.json pid unusable, falling back to command-line match'; $proc = @(Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | Where-Object { $_.CommandLine -like '*auto_reply*' }) }; if ($proc) { $proc | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Host ('  stopped pid ' + $_.ProcessId) } } else { Write-Host '  no auto_reply process found' }"
echo.
echo Done.
pause >nul
