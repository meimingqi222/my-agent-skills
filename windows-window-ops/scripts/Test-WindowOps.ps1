[CmdletBinding()]
param(
    [string]$ModulePath,
    [string]$OutputDirectory = (Join-Path ([IO.Path]::GetTempPath()) ("windowops-tests-{0}" -f [guid]::NewGuid().ToString("N")))
)

$ErrorActionPreference = "Stop"
if ([string]::IsNullOrEmpty($ModulePath)) {
    $ModulePath = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "WindowOps.ps1"
}
. $ModulePath
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null

function Assert-WinOpsTest {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw "ASSERTION FAILED: $Message" }
}

function Start-WinOpsTestForm {
    param(
        [Parameter(Mandatory)][string]$Title,
        [string]$Color = "White",
        [switch]$SecondaryWindow,
        [switch]$TextBox
    )
    $secondaryCode = if ($SecondaryWindow) {
        "`$second=[Windows.Forms.Form]::new();`$second.Text='$Title Secondary';`$second.Size=[Drawing.Size]::new(420,280);`$form.Add_Shown({`$second.Show()})"
    } else { "" }
    $textBoxCode = if ($TextBox) {
        "`$box=[Windows.Forms.TextBox]::new();`$box.Dock='Fill';`$form.Controls.Add(`$box);`$box.Add_TextChanged({`$form.Text='$Title Text:' + `$box.Text});`$form.Add_Shown({`$box.Focus()});`$form.Add_Activated({`$box.Focus()})"
    } else { "" }
    $source = @"
Add-Type -AssemblyName System.Windows.Forms,System.Drawing
`$form=[Windows.Forms.Form]::new();`$form.Text='$Title';`$form.Size=[Drawing.Size]::new(600,400);`$form.BackColor=[Drawing.Color]::$Color
$secondaryCode
$textBoxCode
[Windows.Forms.Application]::Run(`$form)
"@
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($source))
    $process = Start-Process powershell.exe -ArgumentList "-NoProfile", "-EncodedCommand", $encoded -PassThru
    $window = Get-AppWindow -ProcessId $process.Id -TitleLike $Title -TimeoutSeconds 10
    if (-not $window) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        throw "Test form '$Title' did not appear."
    }
    [PSCustomObject]@{ Process = $process; Window = $window }
}

$processes = [Collections.Generic.List[Diagnostics.Process]]::new()
$results = [Collections.Generic.List[object]]::new()
try {
    $tag = [guid]::NewGuid().ToString("N").Substring(0, 8)

    $multi = Start-WinOpsTestForm -Title "WindowOps Multi $tag" -Color LightSkyBlue -SecondaryWindow
    $processes.Add($multi.Process)
    $secondary = Get-AppWindow -ProcessId $multi.Process.Id -TitleLike "WindowOps Multi $tag Secondary" -TimeoutSeconds 5
    Assert-WinOpsTest ($null -ne $secondary) "Secondary top-level window was not enumerated."
    Assert-WinOpsTest (@(Get-AppWindows -ProcessId $multi.Process.Id).Count -ge 2) "Expected at least two visible windows."
    $results.Add([PSCustomObject]@{ Test = "MultiWindowEnumeration"; Passed = $true })

    $beforeDpi = Get-WindowInfo -Handle $multi.Window.Handle
    $afterDpi = Get-WindowInfo -Handle $multi.Window.Handle
    Assert-WinOpsTest ($beforeDpi.Left -eq $afterDpi.Left -and $beforeDpi.Width -eq $afterDpi.Width) "DPI coordinates changed between public calls."
    Assert-WinOpsTest ($beforeDpi.Dpi -gt 0) "Window DPI was not reported."
    $results.Add([PSCustomObject]@{ Test = "StablePhysicalDpiCoordinates"; Passed = $true; Dpi = $beforeDpi.Dpi })

    $uniform = Start-WinOpsTestForm -Title "WindowOps Uniform $tag" -Color White
    $processes.Add($uniform.Process)
    $uniformCapture = Save-AppWindowScreenshot -Handle $uniform.Window.Handle -Path (Join-Path $OutputDirectory "uniform.png") -PassThru
    Assert-WinOpsTest ($uniformCapture.CaptureMethod -like "PrintWindow:*") "Uniform content was incorrectly rejected by PrintWindow validation."
    Assert-WinOpsTest (-not $uniformCapture.PrintWindowTimedOut) "Uniform capture unexpectedly timed out."
    $results.Add([PSCustomObject]@{ Test = "UniformBackgroundCapture"; Passed = $true; Milliseconds = $uniformCapture.ElapsedMilliseconds })

    $realCaptureHost = $script:WinOpsCaptureHostPath
    try {
        # A deliberately non-terminating host proves the timeout path is bounded
        # without depending on a particular application's WM_PRINT behavior.
        $script:WinOpsCaptureHostPath = Join-Path $env:WINDIR "System32\notepad.exe"
        $timeoutCapture = Save-AppWindowScreenshot -Handle $uniform.Window.Handle -Path (Join-Path $OutputDirectory "timeout-fallback.png") -PrintWindowTimeoutMilliseconds 200 -PassThru
    }
    finally { $script:WinOpsCaptureHostPath = $realCaptureHost }
    Assert-WinOpsTest ($timeoutCapture.PrintWindowTimedOut -and $timeoutCapture.CaptureMethod -eq "Screen") "Timed-out PrintWindow did not fall back to screen capture."
    Assert-WinOpsTest ($timeoutCapture.ElapsedMilliseconds -lt 2000) "PrintWindow timeout was not bounded."
    $results.Add([PSCustomObject]@{ Test = "BoundedPrintWindowTimeout"; Passed = $true; Milliseconds = $timeoutCapture.ElapsedMilliseconds })

    Show-AppWindow -Handle $uniform.Window.Handle -Mode Maximize
    Wait-AppWindowState -Handle $uniform.Window.Handle -State Maximized -TimeoutSeconds 3 | Out-Null
    $maxInfo = Get-WindowInfo -Handle $uniform.Window.Handle
    $maxCapture = Save-AppWindowScreenshot -Handle $uniform.Window.Handle -Path (Join-Path $OutputDirectory "maximized.png") -CaptureMode Screen -PassThru
    Assert-WinOpsTest ($maxCapture.Width -eq $maxInfo.VisibleWidth -and $maxCapture.Height -eq $maxInfo.VisibleHeight) "Maximized capture did not use DWM visible bounds."
    Assert-WinOpsTest ((Get-WindowInfo -Handle $uniform.Window.Handle).IsMaximized) "Maximized state was not preserved."
    Assert-WinOpsTest ($maxCapture.StateRestored) "Maximized state restoration was not verified."
    $results.Add([PSCustomObject]@{ Test = "MaximizedVisibleFrame"; Passed = $true; Size = "$($maxCapture.Width)x$($maxCapture.Height)" })

    Show-AppWindow -Handle $uniform.Window.Handle -Mode Restore
    Wait-AppWindowState -Handle $uniform.Window.Handle -State Restored -TimeoutSeconds 3 | Out-Null
    Show-AppWindow -Handle $uniform.Window.Handle -Mode Minimize
    Wait-AppWindowState -Handle $uniform.Window.Handle -State Minimized -TimeoutSeconds 3 | Out-Null
    $minCapture = Save-AppWindowScreenshot -Handle $uniform.Window.Handle -Path (Join-Path $OutputDirectory "minimized.png") -PassThru
    Assert-WinOpsTest ((Get-WindowInfo -Handle $uniform.Window.Handle).IsMinimized) "Minimized state was not restored."
    Assert-WinOpsTest ($minCapture.StateRestored) "Minimized restoration report failed."
    $results.Add([PSCustomObject]@{ Test = "MinimizedRestore"; Passed = $true })

    Show-AppWindow -Handle $uniform.Window.Handle -Mode Restore
    Wait-AppWindowState -Handle $uniform.Window.Handle -State Restored -TimeoutSeconds 3 | Out-Null
    Move-AppWindow -Handle $uniform.Window.Handle -X -5000 -Y -4000 -Width 900 -Height 600
    $offscreenBefore = Get-WindowInfo -Handle $uniform.Window.Handle
    $offscreenCapture = Save-AppWindowScreenshot -Handle $uniform.Window.Handle -Path (Join-Path $OutputDirectory "offscreen.png") -CaptureMode Screen -PassThru
    $offscreenAfter = Get-WindowInfo -Handle $uniform.Window.Handle
    Assert-WinOpsTest ($offscreenBefore.Left -eq $offscreenAfter.Left -and $offscreenBefore.Top -eq $offscreenAfter.Top -and $offscreenBefore.Width -eq $offscreenAfter.Width -and $offscreenBefore.Height -eq $offscreenAfter.Height) "Off-screen physical placement was not restored."
    Assert-WinOpsTest ($offscreenCapture.StateRestored) "Off-screen restoration report failed."
    $results.Add([PSCustomObject]@{ Test = "OffscreenRestore"; Passed = $true })

    $keys = Start-WinOpsTestForm -Title "WindowOps Keys $tag" -Color Beige -TextBox
    $processes.Add($keys.Process)
    Send-AppKeys -Handle $keys.Window.Handle -Keys "abc"
    $typed = Get-AppWindow -ProcessId $keys.Process.Id -TitleLike "WindowOps Keys $tag Text:abc" -TimeoutSeconds 3
    Assert-WinOpsTest ($null -ne $typed) "Send-AppKeys did not reach the target text box."
    $results.Add([PSCustomObject]@{ Test = "VerifiedSendKeys"; Passed = $true })

    [PSCustomObject]@{
        Passed = $true
        OutputDirectory = $OutputDirectory
        PowerShell = "$($PSVersionTable.PSEdition) $($PSVersionTable.PSVersion)"
        Results = @($results)
    }
}
finally {
    foreach ($process in $processes) {
        if ($process -and -not $process.HasExited) { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue }
        if ($process) { $process.Dispose() }
    }
}
