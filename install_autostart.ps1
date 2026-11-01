# Optional: start TWS Matcher automatically about 90 seconds after TWS opens.
# Run once:  powershell -ExecutionPolicy Bypass -File install_autostart.ps1
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = (Get-Command python).Source
$pythonw = Join-Path (Split-Path $python) 'pythonw.exe'
$script = Join-Path $here 'tws_watcher.pyw'
$link = Join-Path ([Environment]::GetFolderPath('Startup')) 'TWS Matcher watcher.lnk'
$shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($link)
$shortcut.TargetPath = $pythonw
$shortcut.Arguments = '"' + $script + '"'
$shortcut.WorkingDirectory = $here
$shortcut.Save()
Start-Process -FilePath $pythonw -ArgumentList ('"' + $script + '"') -WorkingDirectory $here
Write-Host "Auto-start installed. To remove it, delete: $link"
