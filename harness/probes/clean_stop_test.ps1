# Can the drive be put down without killing the program? Send CTRL_BREAK and see.
$sp = $PSScriptRoot
$log = "$sp\clean-stop.log"
Remove-Item "$sp\jh-status.json", $log -ErrorAction SilentlyContinue

$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = "$sp\flushproof\ext4win.exe"
$psi.Arguments = "--disk `"$sp\regress.img`" --part 1 --mount R --read-write --status `"$sp\jh-status.json`""
$psi.UseShellExecute = $false
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
# Its own process group, so CTRL_BREAK reaches it and not this window.
$psi.CreateNoWindow = $false
$proc = [System.Diagnostics.Process]::Start($psi)

Start-Sleep 7
"mounted: $(Test-Path R:\)" | Tee-Object -FilePath $log -Append
"status: $(Get-Content "$sp\jh-status.json" -ErrorAction SilentlyContinue)" | Tee-Object -FilePath $log -Append
Set-Content R:\clean-stop-probe.txt "written before the stop" -ErrorAction SilentlyContinue
"wrote a file: $(Test-Path R:\clean-stop-probe.txt)" | Tee-Object -FilePath $log -Append

# CTRL_BREAK to the process group, via the kernel call: PowerShell has no verb for it.
Add-Type -Namespace W -Name K -MemberDefinition @'
[DllImport("kernel32.dll", SetLastError=true)]
public static extern bool GenerateConsoleCtrlEvent(uint dwCtrlEvent, uint dwProcessGroupId);
[DllImport("kernel32.dll", SetLastError=true)]
public static extern bool AttachConsole(uint dwProcessId);
[DllImport("kernel32.dll", SetLastError=true)]
public static extern bool FreeConsole();
'@
$sent = [W.K]::GenerateConsoleCtrlEvent(1, $proc.Id)   # 1 = CTRL_BREAK_EVENT
"CTRL_BREAK sent: $sent" | Tee-Object -FilePath $log -Append

if ($proc.WaitForExit(30000)) {
    "exit code: $($proc.ExitCode)" | Tee-Object -FilePath $log -Append
} else {
    "DID NOT EXIT within 30s" | Tee-Object -FilePath $log -Append
    $proc.Kill()
}
"still mounted after: $(Test-Path R:\)" | Tee-Object -FilePath $log -Append
"status after: $(Get-Content "$sp\jh-status.json" -ErrorAction SilentlyContinue)" | Tee-Object -FilePath $log -Append
"--- program said" | Tee-Object -FilePath $log -Append
$proc.StandardError.ReadToEnd() | Tee-Object -FilePath $log -Append
