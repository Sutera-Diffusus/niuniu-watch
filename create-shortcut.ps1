$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
# Keep this file pure ASCII: PowerShell 5.1 reads BOM-less .ps1 as ANSI/GBK,
# so Chinese strings are embedded as base64 instead.
function B64S($b64){ [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($b64)) }
$exeName = (B64S '5LuK5aSp54mb54mb6YWx5oqV5ZaC5LqG5rKh') + '.exe'   # today-niuniu exe name
$lnkName = (B64S '5LuK5aSp54mb54mb6YWx5oqV5ZaC5LqG5rKh') + '.lnk'
$oldName = (B64S '54mb54mb6YWx5oqV5ZaC') + '.lnk'                    # legacy shortcut name
$exe = Join-Path $PSScriptRoot $exeName
$desktop = [Environment]::GetFolderPath('Desktop')
$w = New-Object -ComObject WScript.Shell
$old = Join-Path $desktop $oldName
if (Test-Path $old) { Remove-Item $old -Force }
$lnk = Join-Path $desktop $lnkName
$s = $w.CreateShortcut($lnk)
$s.TargetPath = $exe
$s.Arguments = ''
$s.WorkingDirectory = $PSScriptRoot
$s.IconLocation = $exe + ',0'
$s.Description = 'NiuNiu feeding dashboard'
$s.Save()
Write-Output ('saved: ' + $lnk)
$r = $w.CreateShortcut($lnk)
Write-Output ('target: ' + $r.TargetPath)
