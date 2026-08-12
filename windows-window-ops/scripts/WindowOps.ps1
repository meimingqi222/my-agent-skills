<#
.SYNOPSIS
Reusable Win32 window-automation helpers backing the `windows-window-ops` skill.

.DESCRIPTION
Dot-source this file once per PowerShell session, then call the functions below.
All P/Invoke signatures live in this one file so nothing gets retyped/re-verified
per invocation. Safe to dot-source more than once in the same session (native type
registration is guarded).

.EXAMPLE
. "$PSScriptRoot\WindowOps.ps1"
$win = Get-AppWindow -ProcessName "crush-gui-dev" -TimeoutSeconds 10
Show-AppWindow -Handle $win.Handle -Mode Foreground
Save-AppWindowScreenshot -Handle $win.Handle -Path "$env:TEMP\shot.png" -Foreground
#>

if (-not ("WinOpsNative" -as [type])) {
    Add-Type -AssemblyName System.Drawing -ErrorAction SilentlyContinue
    $winOpsNativeSource = @"
using System;
using System.Drawing;
using System.Runtime.InteropServices;
using System.Text;

// Native ABI v0.5.0. Changing this source automatically rotates the cached assembly.
public delegate bool WinOpsEnumWindowsProc(IntPtr hWnd, IntPtr lParam);

public struct WinOpsRect { public int Left; public int Top; public int Right; public int Bottom; }
public struct WinOpsPoint { public int X; public int Y; }

[StructLayout(LayoutKind.Sequential)]
public struct WinOpsWindowPlacement {
    public int Length;
    public int Flags;
    public int ShowCmd;
    public WinOpsPoint MinPosition;
    public WinOpsPoint MaxPosition;
    public WinOpsRect NormalPosition;
}

public class WinOpsNative {
    [DllImport("user32.dll")] public static extern bool EnumWindows(WinOpsEnumWindowsProc lpEnumFunc, IntPtr lParam);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out WinOpsRect lpRect);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool IsZoomed(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool IsHungAppWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool GetWindowPlacement(IntPtr hWnd, ref WinOpsWindowPlacement lpwndpl);
    [DllImport("user32.dll")] public static extern bool SetWindowPlacement(IntPtr hWnd, ref WinOpsWindowPlacement lpwndpl);
    [DllImport("user32.dll")] public static extern int GetSystemMetrics(int nIndex);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] public static extern IntPtr SetThreadDpiAwarenessContext(IntPtr dpiContext);
    [DllImport("user32.dll")] public static extern uint GetDpiForWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr hWnd, uint Msg, IntPtr wParam, IntPtr lParam);
    [DllImport("user32.dll")] public static extern bool MoveWindow(IntPtr hWnd, int X, int Y, int nWidth, int nHeight, bool bRepaint);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowText(IntPtr hWnd, StringBuilder lpString, int nMaxCount);
    [DllImport("user32.dll")] public static extern int GetWindowTextLength(IntPtr hWnd);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetClassName(IntPtr hWnd, StringBuilder lpClassName, int nMaxCount);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
    [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool fAttach);
    [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr hWnd, IntPtr hdcBlt, uint nFlags);
    [DllImport("user32.dll")] public static extern IntPtr GetWindowDC(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern int ReleaseDC(IntPtr hWnd, IntPtr hDC);
    [DllImport("gdi32.dll")] public static extern IntPtr CreateCompatibleDC(IntPtr hdc);
    [DllImport("gdi32.dll")] public static extern IntPtr CreateCompatibleBitmap(IntPtr hdc, int nWidth, int nHeight);
    [DllImport("gdi32.dll")] public static extern IntPtr SelectObject(IntPtr hdc, IntPtr hgdiobj);
    [DllImport("gdi32.dll")] public static extern bool DeleteObject(IntPtr hObject);
    [DllImport("gdi32.dll")] public static extern bool DeleteDC(IntPtr hdc);
    [DllImport("gdi32.dll")] public static extern bool BitBlt(IntPtr hdcDest, int nXDest, int nYDest, int nWidth, int nHeight, IntPtr hdcSrc, int nXSrc, int nYSrc, uint dwRop);
    [DllImport("dwmapi.dll")] public static extern int DwmFlush();
    [DllImport("dwmapi.dll")] public static extern int DwmGetWindowAttribute(IntPtr hWnd, int dwAttribute, out WinOpsRect pvAttribute, int cbAttribute);

    public static bool CapturePrintWindowPng(IntPtr hWnd, string path, int width, int height, uint flags) {
        IntPtr windowDc = GetWindowDC(hWnd);
        if (windowDc == IntPtr.Zero) return false;
        try {
            using (WinOpsGdiBitmap gdi = new WinOpsGdiBitmap(windowDc, width, height)) {
                if (!PrintWindow(hWnd, gdi.Hdc, flags)) return false;
                using (Bitmap bitmap = Bitmap.FromHbitmap(gdi.Hbmp)) {
                    bitmap.Save(path, System.Drawing.Imaging.ImageFormat.Png);
                }
            }
            return true;
        }
        finally { ReleaseDC(hWnd, windowDc); }
    }
}

public class WinOpsGdiBitmap : System.IDisposable {
    public IntPtr Hdc;
    public IntPtr Hbmp;
    public IntPtr OldObj;
    public int Width;
    public int Height;
    public System.Drawing.Bitmap Bitmap;

    public WinOpsGdiBitmap(IntPtr sourceHdc, int width, int height) {
        Width = width;
        Height = height;
        Hdc = WinOpsNative.CreateCompatibleDC(sourceHdc);
        Hbmp = WinOpsNative.CreateCompatibleBitmap(sourceHdc, width, height);
        OldObj = WinOpsNative.SelectObject(Hdc, Hbmp);
        if (Hdc == IntPtr.Zero || Hbmp == IntPtr.Zero) {
            throw new System.InvalidOperationException("Failed to create off-screen DC/bitmap.");
        }
    }

    public void Dispose() {
        if (Bitmap != null) { Bitmap.Dispose(); Bitmap = null; }
        if (OldObj != IntPtr.Zero) { WinOpsNative.SelectObject(Hdc, OldObj); OldObj = IntPtr.Zero; }
        if (Hbmp != IntPtr.Zero) { WinOpsNative.DeleteObject(Hbmp); Hbmp = IntPtr.Zero; }
        if (Hdc != IntPtr.Zero) { WinOpsNative.DeleteDC(Hdc); Hdc = IntPtr.Zero; }
    }
}
"@
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try {
        $sourceHashBytes = $sha256.ComputeHash([Text.Encoding]::UTF8.GetBytes($winOpsNativeSource))
        $sourceHash = ([BitConverter]::ToString($sourceHashBytes)).Replace("-", "").Substring(0, 16)
    }
    finally { $sha256.Dispose() }
    $cacheRoot = Join-Path $env:LOCALAPPDATA "Codex\WindowOps"
    $runtimeKey = "$($PSVersionTable.PSEdition)-$($PSVersionTable.PSVersion.Major)"
    $cachedAssembly = Join-Path $cacheRoot "WinOpsNative-$runtimeKey-$sourceHash.dll"
    if (-not (Test-Path -LiteralPath $cachedAssembly)) {
        New-Item -ItemType Directory -Force -Path $cacheRoot | Out-Null
        $temporaryAssembly = "$cachedAssembly.$PID.tmp.dll"
        try {
            $drawingReferences = @([System.Drawing.Bitmap].Assembly.Location)
            $drawingAssemblyDirectory = Split-Path -Parent ([System.Drawing.Bitmap].Assembly.Location)
            $drawingReferences += @(Get-ChildItem $drawingAssemblyDirectory -Filter "System.Private.Windows*.dll" -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName)
            Add-Type -TypeDefinition $winOpsNativeSource -ReferencedAssemblies $drawingReferences -OutputAssembly $temporaryAssembly -ErrorAction Stop
            try { Move-Item -LiteralPath $temporaryAssembly -Destination $cachedAssembly -ErrorAction Stop }
            catch {
                if (-not (Test-Path -LiteralPath $cachedAssembly)) { throw }
                Remove-Item -LiteralPath $temporaryAssembly -Force -ErrorAction SilentlyContinue
            }
        }
        finally {
            if (Test-Path -LiteralPath $temporaryAssembly) { Remove-Item -LiteralPath $temporaryAssembly -Force -ErrorAction SilentlyContinue }
        }
    }
    Add-Type -Path $cachedAssembly -ErrorAction Stop
    $script:WinOpsNativeAssemblyPath = $cachedAssembly
}
elseif (-not $script:WinOpsNativeAssemblyPath) {
    $script:WinOpsNativeAssemblyPath = [WinOpsNative].Assembly.Location
}

if (-not $script:WinOpsCaptureHostPath) {
    if (-not $cacheRoot) { $cacheRoot = Join-Path $env:LOCALAPPDATA "Codex\WindowOps" }
    $captureHostSource = @"
using System;
using System.Drawing;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;

internal static class WinOpsCaptureHost {
    [DllImport("user32.dll")] private static extern bool PrintWindow(IntPtr hWnd, IntPtr hdcBlt, uint flags);
    [DllImport("user32.dll")] private static extern IntPtr GetWindowDC(IntPtr hWnd);
    [DllImport("user32.dll")] private static extern int ReleaseDC(IntPtr hWnd, IntPtr hdc);
    [DllImport("gdi32.dll")] private static extern IntPtr CreateCompatibleDC(IntPtr hdc);
    [DllImport("gdi32.dll")] private static extern IntPtr CreateCompatibleBitmap(IntPtr hdc, int width, int height);
    [DllImport("gdi32.dll")] private static extern IntPtr SelectObject(IntPtr hdc, IntPtr obj);
    [DllImport("gdi32.dll")] private static extern bool DeleteObject(IntPtr obj);
    [DllImport("gdi32.dll")] private static extern bool DeleteDC(IntPtr hdc);

    [STAThread]
    private static int Main() {
        IntPtr window = new IntPtr(long.Parse(Environment.GetEnvironmentVariable("WINOPS_HANDLE")));
        string output = Environment.GetEnvironmentVariable("WINOPS_OUTPUT");
        int width = int.Parse(Environment.GetEnvironmentVariable("WINOPS_WIDTH"));
        int height = int.Parse(Environment.GetEnvironmentVariable("WINOPS_HEIGHT"));
        uint flags = uint.Parse(Environment.GetEnvironmentVariable("WINOPS_FLAGS"));
        IntPtr windowDc = GetWindowDC(window);
        if (windowDc == IntPtr.Zero) return 2;
        IntPtr memoryDc = IntPtr.Zero, bitmapHandle = IntPtr.Zero, oldObject = IntPtr.Zero;
        try {
            memoryDc = CreateCompatibleDC(windowDc);
            bitmapHandle = CreateCompatibleBitmap(windowDc, width, height);
            if (memoryDc == IntPtr.Zero || bitmapHandle == IntPtr.Zero) return 3;
            oldObject = SelectObject(memoryDc, bitmapHandle);
            if (!PrintWindow(window, memoryDc, flags)) return 4;
            using (Bitmap bitmap = Bitmap.FromHbitmap(bitmapHandle)) { bitmap.Save(output, ImageFormat.Png); }
            return 0;
        }
        finally {
            if (oldObject != IntPtr.Zero) SelectObject(memoryDc, oldObject);
            if (bitmapHandle != IntPtr.Zero) DeleteObject(bitmapHandle);
            if (memoryDc != IntPtr.Zero) DeleteDC(memoryDc);
            ReleaseDC(window, windowDc);
        }
    }
}
"@
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try {
        $captureHostHash = ([BitConverter]::ToString($sha256.ComputeHash([Text.Encoding]::UTF8.GetBytes($captureHostSource)))).Replace("-", "").Substring(0, 16)
    }
    finally { $sha256.Dispose() }
    $captureHostPath = Join-Path $cacheRoot "WinOpsCaptureHost-$captureHostHash.exe"
    if (-not (Test-Path -LiteralPath $captureHostPath)) {
        $cscPath = @(
            "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe",
            "$env:WINDIR\Microsoft.NET\Framework\v4.0.30319\csc.exe"
        ) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
        if ($cscPath) {
            $temporarySource = Join-Path $cacheRoot "WinOpsCaptureHost-$PID.cs"
            $temporaryHost = "$captureHostPath.$PID.tmp.exe"
            try {
                [IO.File]::WriteAllText($temporarySource, $captureHostSource, [Text.Encoding]::UTF8)
                $compilerOutput = & $cscPath /nologo /target:winexe /optimize+ /reference:System.Drawing.dll "/out:$temporaryHost" $temporarySource 2>&1
                if ($LASTEXITCODE -ne 0) { throw "Capture host compilation failed: $($compilerOutput -join ' ')" }
                try { Move-Item -LiteralPath $temporaryHost -Destination $captureHostPath -ErrorAction Stop }
                catch {
                    if (-not (Test-Path -LiteralPath $captureHostPath)) { throw }
                    Remove-Item -LiteralPath $temporaryHost -Force -ErrorAction SilentlyContinue
                }
            }
            catch { Write-Verbose "Native capture host unavailable; PowerShell isolation will be used. $($_.Exception.Message)" }
            finally {
                Remove-Item -LiteralPath $temporarySource -Force -ErrorAction SilentlyContinue
                Remove-Item -LiteralPath $temporaryHost -Force -ErrorAction SilentlyContinue
            }
        }
    }
    if (Test-Path -LiteralPath $captureHostPath) { $script:WinOpsCaptureHostPath = $captureHostPath }
}

$script:SW_HIDE = 0
$script:SW_SHOW = 5
$script:SW_MAXIMIZE = 3
$script:SW_MINIMIZE = 6
$script:SW_RESTORE = 9
$script:WM_CLOSE = 0x0010
$script:SM_XVIRTUALSCREEN = 76
$script:SM_YVIRTUALSCREEN = 77
$script:SM_CXVIRTUALSCREEN = 78
$script:SM_CYVIRTUALSCREEN = 79
$script:DWMWA_EXTENDED_FRAME_BOUNDS = 9
$script:DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = [IntPtr](-4)

function Set-WinOpsDpiContext {
    <# Internal: make every coordinate API use physical per-monitor pixels. #>
    try {
        [WinOpsNative]::SetThreadDpiAwarenessContext($script:DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2) | Out-Null
    }
    catch [EntryPointNotFoundException] {
        # Windows versions before Per-Monitor V2 support get the best legacy mode.
        [WinOpsNative]::SetProcessDPIAware() | Out-Null
    }
}

function Set-ForceForeground {
    <#
    .SYNOPSIS
    Internal helper: reliably foregrounds a window from a background process.

    .DESCRIPTION
    A plain SetForegroundWindow call frequently no-ops under Windows' foreground-lock
    heuristic when called from a process that isn't already foreground (which a
    PowerShell tool invocation launched by an agent always is) - it returns success
    but focus silently stays wherever it was. This attaches this thread's input queue
    to the current foreground window's thread first, which is the standard trick that
    actually gets the OS to honor the switch. Confirmed empirically: plain
    ShowWindow+BringWindowToTop+SetForegroundWindow left a background terminal
    focused (screenshot captured the wrong window) even though every call reported
    success; this version's SetForegroundWindow result was verified against
    GetForegroundWindow() actually matching the target handle afterward.
    #>
    param([Parameter(Mandatory)][IntPtr]$Handle)
    Set-WinOpsDpiContext
    if (-not [WinOpsNative]::IsWindow($Handle)) { throw "Window handle $Handle is invalid or no longer exists." }
    if ([WinOpsNative]::GetForegroundWindow() -eq $Handle) { return }

    # Restore only minimized windows. Calling SW_RESTORE unconditionally silently
    # unmaximizes a maximized window, which is surprising for a focus operation.
    if ([WinOpsNative]::IsIconic($Handle)) {
        [WinOpsNative]::ShowWindowAsync($Handle, $script:SW_RESTORE) | Out-Null
    }
    elseif (-not [WinOpsNative]::IsWindowVisible($Handle)) {
        [WinOpsNative]::ShowWindowAsync($Handle, $script:SW_SHOW) | Out-Null
    }

    for ($attempt = 0; $attempt -lt 3; $attempt++) {
        $fgWindow = [WinOpsNative]::GetForegroundWindow()
        [uint32]$fgProcessId = 0
        $fgThread = [WinOpsNative]::GetWindowThreadProcessId($fgWindow, [ref]$fgProcessId)
        $currentThread = [WinOpsNative]::GetCurrentThreadId()
        $attached = $false
        try {
            if ($fgThread -ne 0 -and $fgThread -ne $currentThread) {
                $attached = [WinOpsNative]::AttachThreadInput($currentThread, $fgThread, $true)
            }
            [WinOpsNative]::BringWindowToTop($Handle) | Out-Null
            [WinOpsNative]::SetForegroundWindow($Handle) | Out-Null
        }
        finally {
            if ($attached) { [WinOpsNative]::AttachThreadInput($currentThread, $fgThread, $false) | Out-Null }
        }
        if ([WinOpsNative]::GetForegroundWindow() -eq $Handle) { return }
        Start-Sleep -Milliseconds 40
    }
    throw "Windows did not grant foreground focus to handle $Handle after 3 attempts."
}

function Get-WinOpsWindowText {
    param([Parameter(Mandatory)][IntPtr]$Handle)
    $length = [WinOpsNative]::GetWindowTextLength($Handle)
    $builder = [Text.StringBuilder]::new([Math]::Max(2, $length + 1))
    [WinOpsNative]::GetWindowText($Handle, $builder, $builder.Capacity) | Out-Null
    return $builder.ToString()
}

function Get-WinOpsWindowClassName {
    param([Parameter(Mandatory)][IntPtr]$Handle)
    $builder = [Text.StringBuilder]::new(256)
    [WinOpsNative]::GetClassName($Handle, $builder, $builder.Capacity) | Out-Null
    return $builder.ToString()
}

function Get-AppWindows {
    <#
    .SYNOPSIS
    Enumerates all matching top-level windows in current z-order.

    .DESCRIPTION
    Unlike Process.MainWindowHandle, this finds secondary and owned top-level
    windows too. Filter by process name/id and title. Hidden windows are omitted
    unless -IncludeHidden is supplied.
    #>
    [CmdletBinding()]
    param(
        [string]$ProcessName,
        [int[]]$ProcessId,
        [string]$TitleLike,
        [switch]$IncludeHidden
    )
    Set-WinOpsDpiContext
    $processes = if (-not [string]::IsNullOrEmpty($ProcessName)) {
        @(Get-Process -Name $ProcessName -ErrorAction SilentlyContinue)
    }
    elseif ($ProcessId -and $ProcessId.Count -gt 0) {
        @(Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)
    }
    else { @(Get-Process -ErrorAction SilentlyContinue) }

    $processById = @{}
    foreach ($process in $processes) { $processById[[uint32]$process.Id] = $process }
    if ((-not [string]::IsNullOrEmpty($ProcessName) -or $ProcessId) -and $processById.Count -eq 0) { return }

    $results = [Collections.Generic.List[object]]::new()
    $zOrder = 0
    $callback = [WinOpsEnumWindowsProc]{
        param([IntPtr]$windowHandle, [IntPtr]$state)
        [uint32]$windowProcessId = 0
        [WinOpsNative]::GetWindowThreadProcessId($windowHandle, [ref]$windowProcessId) | Out-Null
        if (-not $processById.ContainsKey($windowProcessId)) { return $true }
        $visible = [WinOpsNative]::IsWindowVisible($windowHandle)
        if (-not $IncludeHidden -and -not $visible) { return $true }
        $title = Get-WinOpsWindowText -Handle $windowHandle
        if (-not [string]::IsNullOrEmpty($TitleLike) -and $title -notlike $TitleLike) { return $true }
        $rect = Get-WinOpsWindowRect -Handle $windowHandle
        $visibleRect = Get-WinOpsWindowRect -Handle $windowHandle -VisibleFrame
        $process = $processById[$windowProcessId]
        $results.Add([PSCustomObject]@{
            ProcessId   = [int]$windowProcessId
            ProcessName = $process.ProcessName
            Handle      = $windowHandle
            Title       = $title
            ClassName   = Get-WinOpsWindowClassName -Handle $windowHandle
            ZOrder      = $zOrder
            Left        = $rect.Left
            Top         = $rect.Top
            Width       = $rect.Right - $rect.Left
            Height      = $rect.Bottom - $rect.Top
            VisibleLeft = $visibleRect.Left
            VisibleTop  = $visibleRect.Top
            VisibleWidth = $visibleRect.Right - $visibleRect.Left
            VisibleHeight = $visibleRect.Bottom - $visibleRect.Top
            Dpi         = [WinOpsNative]::GetDpiForWindow($windowHandle)
            IsVisible   = $visible
            IsMinimized = [WinOpsNative]::IsIconic($windowHandle)
            IsMaximized = [WinOpsNative]::IsZoomed($windowHandle)
            IsHung      = [WinOpsNative]::IsHungAppWindow($windowHandle)
        })
        $zOrder++
        return $true
    }
    if (-not [WinOpsNative]::EnumWindows($callback, [IntPtr]::Zero)) { throw "EnumWindows failed." }
    return $results
}

function Get-AppWindow {
    <#
    .SYNOPSIS
    Finds a top-level window owned by a process, optionally waiting for it to appear.

    .PARAMETER ProcessName
    Process image name without .exe, as used by Get-Process -Name.

    .PARAMETER TitleLike
    Optional -like pattern (e.g. "*Crush*") to filter by window title.

    .PARAMETER TimeoutSeconds
    If > 0, poll until a matching window appears or the timeout elapses. Use this
    right after starting a dev server/app — MainWindowHandle is often zero for the
    first second or two while the window is still being created.

    .EXAMPLE
    Get-AppWindow -ProcessName "crush-gui-dev" -TimeoutSeconds 15
    #>
    [CmdletBinding()]
    param(
        [string]$ProcessName,
        [int[]]$ProcessId,
        [string]$TitleLike,
        [int]$TimeoutSeconds = 0,
        [ValidateRange(0.05, 60)]
        [double]$PollIntervalSeconds = 0.25,
        [switch]$IncludeHidden
    )
    $stopwatch = [Diagnostics.Stopwatch]::StartNew()
    do {
        $match = Get-AppWindows -ProcessName $ProcessName -ProcessId $ProcessId -TitleLike $TitleLike -IncludeHidden:$IncludeHidden | Select-Object -First 1
        if ($match) {
            $stopwatch.Stop()
            return $match
        }
        $remainingMilliseconds = $TimeoutSeconds * 1000 - $stopwatch.ElapsedMilliseconds
        if ($remainingMilliseconds -gt 0) {
            Start-Sleep -Milliseconds ([Math]::Min([int]($PollIntervalSeconds * 1000), $remainingMilliseconds))
        }
    } while ($stopwatch.ElapsedMilliseconds -lt $TimeoutSeconds * 1000)
    $stopwatch.Stop()
    return $null
}

function Get-WindowInfo {
    <#
    .SYNOPSIS
    Returns position, size, and minimized/maximized/visible state for a window handle.
    #>
    param([Parameter(Mandatory)][IntPtr]$Handle)
    Set-WinOpsDpiContext
    if (-not [WinOpsNative]::IsWindow($Handle)) {
        return [PSCustomObject]@{ Handle = $Handle; Exists = $false }
    }
    $rect = Get-WinOpsWindowRect -Handle $Handle
    $visibleRect = Get-WinOpsWindowRect -Handle $Handle -VisibleFrame
    [uint32]$windowProcessId = 0
    [WinOpsNative]::GetWindowThreadProcessId($Handle, [ref]$windowProcessId) | Out-Null
    [PSCustomObject]@{
        Handle      = $Handle
        ProcessId   = [int]$windowProcessId
        Title       = Get-WinOpsWindowText -Handle $Handle
        ClassName   = Get-WinOpsWindowClassName -Handle $Handle
        Left        = $rect.Left
        Top         = $rect.Top
        Right       = $rect.Right
        Bottom      = $rect.Bottom
        Width       = $rect.Right - $rect.Left
        Height      = $rect.Bottom - $rect.Top
        VisibleLeft = $visibleRect.Left
        VisibleTop  = $visibleRect.Top
        VisibleWidth = $visibleRect.Right - $visibleRect.Left
        VisibleHeight = $visibleRect.Bottom - $visibleRect.Top
        Dpi         = [WinOpsNative]::GetDpiForWindow($Handle)
        IsMinimized = [WinOpsNative]::IsIconic($Handle)
        IsMaximized = [WinOpsNative]::IsZoomed($Handle)
        IsVisible   = [WinOpsNative]::IsWindowVisible($Handle)
        IsForeground = [WinOpsNative]::GetForegroundWindow() -eq $Handle
        IsHung      = [WinOpsNative]::IsHungAppWindow($Handle)
        Exists      = [WinOpsNative]::IsWindow($Handle)
    }
}

function Wait-AppWindowState {
    <#
    .SYNOPSIS
    Waits until a window reaches a requested state and returns its latest info.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][IntPtr]$Handle,
        [Parameter(Mandatory)]
        [ValidateSet("Visible", "Hidden", "Minimized", "Maximized", "Restored", "Foreground", "Closed")]
        [string]$State,
        [ValidateRange(0, 3600)][double]$TimeoutSeconds = 10,
        [ValidateRange(10, 5000)][int]$PollIntervalMilliseconds = 100
    )
    $stopwatch = [Diagnostics.Stopwatch]::StartNew()
    do {
        $info = Get-WindowInfo -Handle $Handle
        $matched = switch ($State) {
            "Visible" { $info.Exists -and $info.IsVisible }
            "Hidden" { $info.Exists -and -not $info.IsVisible }
            "Minimized" { $info.Exists -and $info.IsMinimized }
            "Maximized" { $info.Exists -and $info.IsMaximized }
            "Restored" { $info.Exists -and -not $info.IsMinimized -and -not $info.IsMaximized }
            "Foreground" { $info.Exists -and $info.IsForeground }
            "Closed" { -not $info.Exists }
        }
        if ($matched) { $stopwatch.Stop(); return $info }
        $remaining = $TimeoutSeconds * 1000 - $stopwatch.ElapsedMilliseconds
        if ($remaining -gt 0) { Start-Sleep -Milliseconds ([Math]::Min($PollIntervalMilliseconds, $remaining)) }
    } while ($stopwatch.ElapsedMilliseconds -lt $TimeoutSeconds * 1000)
    $stopwatch.Stop()
    throw "Window handle $Handle did not reach state '$State' within $TimeoutSeconds seconds."
}

function Show-AppWindow {
    <#
    .SYNOPSIS
    Foregrounds, minimizes, maximizes, restores, or hides a window.

    .PARAMETER Mode
    Foreground (default) restores-if-minimized, raises z-order, and focuses it —
    this is what you want when telling the user "look at the window now".
    #>
    param(
        [Parameter(Mandatory)][IntPtr]$Handle,
        [ValidateSet("Foreground", "Minimize", "Maximize", "Restore", "Hide")]
        [string]$Mode = "Foreground"
    )
    if (-not [WinOpsNative]::IsWindow($Handle)) { throw "Window handle $Handle is invalid or no longer exists." }
    switch ($Mode) {
        "Minimize" { [WinOpsNative]::ShowWindowAsync($Handle, $script:SW_MINIMIZE) | Out-Null }
        "Maximize" { [WinOpsNative]::ShowWindowAsync($Handle, $script:SW_MAXIMIZE) | Out-Null }
        "Restore" { [WinOpsNative]::ShowWindowAsync($Handle, $script:SW_RESTORE) | Out-Null }
        "Hide" { [WinOpsNative]::ShowWindowAsync($Handle, $script:SW_HIDE) | Out-Null }
        "Foreground" { Set-ForceForeground -Handle $Handle }
    }
}

function Move-AppWindow {
    <#
    .SYNOPSIS
    Repositions/resizes a window (outer window rect, in physical pixels).
    #>
    param(
        [Parameter(Mandatory)][IntPtr]$Handle,
        [Parameter(Mandatory)][int]$X,
        [Parameter(Mandatory)][int]$Y,
        [Parameter(Mandatory)][int]$Width,
        [Parameter(Mandatory)][int]$Height
    )
    Set-WinOpsDpiContext
    if ($Width -le 0 -or $Height -le 0) { throw "Width and Height must be greater than zero." }
    if (-not [WinOpsNative]::IsWindow($Handle)) { throw "Window handle $Handle is invalid or no longer exists." }
    if (-not [WinOpsNative]::MoveWindow($Handle, $X, $Y, $Width, $Height, $true)) {
        throw "MoveWindow failed for handle $Handle."
    }
}

function Close-AppWindow {
    <#
    .SYNOPSIS
    Closes a window. Default sends WM_CLOSE (graceful, app can prompt/cancel/save).
    -Force resolves the owning process and Stop-Process -Force's it instead.
    #>
    param(
        [Parameter(Mandatory)][IntPtr]$Handle,
        [switch]$Force,
        [switch]$Wait,
        [ValidateRange(0, 3600)][double]$TimeoutSeconds = 10
    )
    if ($Force) {
        [uint32]$procId = 0
        [WinOpsNative]::GetWindowThreadProcessId($Handle, [ref]$procId) | Out-Null
        if ($procId -gt 0) {
            Stop-Process -Id $procId -Force -Confirm:$false
            if ($Wait) { Wait-AppWindowState -Handle $Handle -State Closed -TimeoutSeconds $TimeoutSeconds | Out-Null }
            return
        }
        throw "Could not resolve a process id for this window handle."
    }
    if (-not [WinOpsNative]::PostMessage($Handle, $script:WM_CLOSE, [IntPtr]::Zero, [IntPtr]::Zero)) {
        throw "Failed to post WM_CLOSE to handle $Handle."
    }
    if ($Wait) { Wait-AppWindowState -Handle $Handle -State Closed -TimeoutSeconds $TimeoutSeconds | Out-Null }
}

function Get-WinOpsWindowPlacement {
    param([Parameter(Mandatory)][IntPtr]$Handle)
    $placement = New-Object WinOpsWindowPlacement
    $placement.Length = [Runtime.InteropServices.Marshal]::SizeOf([type][WinOpsWindowPlacement])
    if (-not [WinOpsNative]::GetWindowPlacement($Handle, [ref]$placement)) {
        throw "GetWindowPlacement failed for window handle $Handle."
    }
    return $placement
}

function Get-WinOpsWindowRect {
    param(
        [Parameter(Mandatory)][IntPtr]$Handle,
        [switch]$VisibleFrame
    )
    Set-WinOpsDpiContext
    $rect = New-Object WinOpsRect
    if ($VisibleFrame) {
        $rectSize = [Runtime.InteropServices.Marshal]::SizeOf([type][WinOpsRect])
        $dwmResult = [WinOpsNative]::DwmGetWindowAttribute(
            $Handle,
            $script:DWMWA_EXTENDED_FRAME_BOUNDS,
            [ref]$rect,
            $rectSize
        )
        if ($dwmResult -eq 0 -and $rect.Right -gt $rect.Left -and $rect.Bottom -gt $rect.Top) {
            return $rect
        }
    }
    if (-not [WinOpsNative]::GetWindowRect($Handle, [ref]$rect)) {
        throw "GetWindowRect failed for window handle $Handle."
    }
    return $rect
}

function Invoke-WinOpsPrintWindowCapture {
    param(
        [Parameter(Mandatory)][IntPtr]$Handle,
        [Parameter(Mandatory)][WinOpsRect]$Rect,
        [Parameter(Mandatory)][uint32]$Flags,
        [ValidateRange(100, 30000)][int]$TimeoutMilliseconds = 1500
    )
    $width = $Rect.Right - $Rect.Left
    $height = $Rect.Bottom - $Rect.Top
    $temporaryPng = Join-Path ([IO.Path]::GetTempPath()) ("windowops-printwindow-{0}.png" -f [guid]::NewGuid().ToString("N"))
    $childSource = @'
Add-Type -AssemblyName System.Drawing -ErrorAction Stop
Add-Type -Path $env:WINOPS_NATIVE_ASSEMBLY -ErrorAction Stop
$ok = [WinOpsNative]::CapturePrintWindowPng(
    [IntPtr]([int64]$env:WINOPS_HANDLE),
    $env:WINOPS_OUTPUT,
    [int]$env:WINOPS_WIDTH,
    [int]$env:WINOPS_HEIGHT,
    [uint32]$env:WINOPS_FLAGS
)
if (-not $ok) { exit 2 }
'@
    $encodedCommand = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($childSource))
    $processInfo = [Diagnostics.ProcessStartInfo]::new()
    if ($script:WinOpsCaptureHostPath) {
        $processInfo.FileName = $script:WinOpsCaptureHostPath
    }
    else {
        $processInfo.FileName = (Get-Process -Id $PID).Path
        $processInfo.Arguments = "-NoProfile -NonInteractive -EncodedCommand $encodedCommand"
    }
    $processInfo.UseShellExecute = $false
    $processInfo.CreateNoWindow = $true
    $processInfo.EnvironmentVariables["WINOPS_NATIVE_ASSEMBLY"] = $script:WinOpsNativeAssemblyPath
    $processInfo.EnvironmentVariables["WINOPS_HANDLE"] = $Handle.ToInt64().ToString([Globalization.CultureInfo]::InvariantCulture)
    $processInfo.EnvironmentVariables["WINOPS_OUTPUT"] = $temporaryPng
    $processInfo.EnvironmentVariables["WINOPS_WIDTH"] = $width.ToString([Globalization.CultureInfo]::InvariantCulture)
    $processInfo.EnvironmentVariables["WINOPS_HEIGHT"] = $height.ToString([Globalization.CultureInfo]::InvariantCulture)
    $processInfo.EnvironmentVariables["WINOPS_FLAGS"] = $Flags.ToString([Globalization.CultureInfo]::InvariantCulture)
    $process = $null
    try {
        $process = [Diagnostics.Process]::Start($processInfo)
        if (-not $process.WaitForExit($TimeoutMilliseconds)) {
            try { $process.Kill() } catch {}
            $process.WaitForExit()
            return [PSCustomObject]@{ Bitmap = $null; TimedOut = $true }
        }
        if ($process.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $temporaryPng)) {
            return [PSCustomObject]@{ Bitmap = $null; TimedOut = $false }
        }
        $source = [System.Drawing.Bitmap]::FromFile($temporaryPng)
        try {
            $copy = New-Object System.Drawing.Bitmap($width, $height, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
            $graphics = [System.Drawing.Graphics]::FromImage($copy)
            try { $graphics.DrawImageUnscaled($source, 0, 0) }
            finally { $graphics.Dispose() }
            return [PSCustomObject]@{ Bitmap = $copy; TimedOut = $false }
        }
        finally { $source.Dispose() }
    }
    finally {
        if ($process) { $process.Dispose() }
        Remove-Item -LiteralPath $temporaryPng -Force -ErrorAction SilentlyContinue
    }
}

function Test-WinOpsBitmapHasContent {
    param([Parameter(Mandatory)][System.Drawing.Bitmap]$Bitmap)

    # Reject only known PrintWindow failure signatures. Uniform white/black-ish
    # application content is legitimate and must not be classified by "visual
    # richness"; window chrome normally keeps a valid black UI from being 98%
    # near-black. DPI truncation typically creates black right AND bottom bands.
    $xStep = [Math]::Max(1, [int]($Bitmap.Width / 32))
    $yStep = [Math]::Max(1, [int]($Bitmap.Height / 32))
    $total = 0
    $opaque = 0
    $nearBlack = 0
    $rightOpaque = 0
    $rightBlack = 0
    $bottomOpaque = 0
    $bottomBlack = 0
    $centerOpaque = 0
    $centerNonBlack = 0
    for ($y = 0; $y -lt $Bitmap.Height; $y += $yStep) {
        for ($x = 0; $x -lt $Bitmap.Width; $x += $xStep) {
            $pixel = $Bitmap.GetPixel($x, $y)
            $total++
            if ($pixel.A -lt 8) { continue }
            $opaque++
            $luma = [int](0.2126 * $pixel.R + 0.7152 * $pixel.G + 0.0722 * $pixel.B)
            $isBlack = $luma -le 8
            if ($isBlack) { $nearBlack++ }
            if ($x -ge $Bitmap.Width * 0.82) {
                $rightOpaque++
                if ($isBlack) { $rightBlack++ }
            }
            if ($y -ge $Bitmap.Height * 0.82) {
                $bottomOpaque++
                if ($isBlack) { $bottomBlack++ }
            }
            if ($x -ge $Bitmap.Width * 0.15 -and $x -le $Bitmap.Width * 0.75 -and
                $y -ge $Bitmap.Height * 0.15 -and $y -le $Bitmap.Height * 0.75) {
                $centerOpaque++
                if (-not $isBlack) { $centerNonBlack++ }
            }
        }
    }
    if ($total -eq 0 -or $opaque / [double]$total -lt 0.02) { return $false }
    if ($nearBlack / [double]$opaque -ge 0.98) { return $false }
    $hasBlackPadding = $rightOpaque -gt 0 -and $bottomOpaque -gt 0 -and $centerOpaque -gt 0 -and
        $rightBlack / [double]$rightOpaque -ge 0.92 -and
        $bottomBlack / [double]$bottomOpaque -ge 0.92 -and
        $centerNonBlack / [double]$centerOpaque -ge 0.15
    return -not $hasBlackPadding
}

function Convert-WinOpsBitmapToVisibleFrame {
    param(
        [Parameter(Mandatory)][System.Drawing.Bitmap]$Bitmap,
        [Parameter(Mandatory)][WinOpsRect]$OuterRect,
        [Parameter(Mandatory)][WinOpsRect]$VisibleRect
    )
    $x = [Math]::Max(0, $VisibleRect.Left - $OuterRect.Left)
    $y = [Math]::Max(0, $VisibleRect.Top - $OuterRect.Top)
    $width = [Math]::Min($VisibleRect.Right - $VisibleRect.Left, $Bitmap.Width - $x)
    $height = [Math]::Min($VisibleRect.Bottom - $VisibleRect.Top, $Bitmap.Height - $y)
    if ($width -le 0 -or $height -le 0) { return $null }
    $crop = [System.Drawing.Rectangle]::new($x, $y, $width, $height)
    return $Bitmap.Clone($crop, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
}

function Invoke-WinOpsScreenCapture {
    param(
        [Parameter(Mandatory)][IntPtr]$Handle,
        [int]$RenderWaitMilliseconds = 300
    )
    Set-WinOpsDpiContext
    Set-ForceForeground -Handle $Handle
    if ($RenderWaitMilliseconds -gt 0) { Start-Sleep -Milliseconds $RenderWaitMilliseconds }
    [WinOpsNative]::DwmFlush() | Out-Null

    $outerRect = Get-WinOpsWindowRect -Handle $Handle
    $visibleRect = Get-WinOpsWindowRect -Handle $Handle -VisibleFrame
    $outerWidth = $outerRect.Right - $outerRect.Left
    $outerHeight = $outerRect.Bottom - $outerRect.Top
    $width = $visibleRect.Right - $visibleRect.Left
    $height = $visibleRect.Bottom - $visibleRect.Top
    $virtualLeft = [WinOpsNative]::GetSystemMetrics($script:SM_XVIRTUALSCREEN)
    $virtualTop = [WinOpsNative]::GetSystemMetrics($script:SM_YVIRTUALSCREEN)
    $virtualWidth = [WinOpsNative]::GetSystemMetrics($script:SM_CXVIRTUALSCREEN)
    $virtualHeight = [WinOpsNative]::GetSystemMetrics($script:SM_CYVIRTUALSCREEN)
    if ($width -gt $virtualWidth -or $height -gt $virtualHeight) {
        throw "Window (${width}x${height}) is larger than the virtual screen (${virtualWidth}x${virtualHeight})."
    }

    $newVisibleX = [Math]::Min([Math]::Max($visibleRect.Left, $virtualLeft), $virtualLeft + $virtualWidth - $width)
    $newVisibleY = [Math]::Min([Math]::Max($visibleRect.Top, $virtualTop), $virtualTop + $virtualHeight - $height)
    $newOuterX = $outerRect.Left + ($newVisibleX - $visibleRect.Left)
    $newOuterY = $outerRect.Top + ($newVisibleY - $visibleRect.Top)
    $moved = -not [WinOpsNative]::IsZoomed($Handle) -and
        ($newOuterX -ne $outerRect.Left -or $newOuterY -ne $outerRect.Top)
    if ($moved) {
        if (-not [WinOpsNative]::MoveWindow($Handle, $newOuterX, $newOuterY, $outerWidth, $outerHeight, $true)) {
            throw "Could not move the window on-screen for capture."
        }
        if ($RenderWaitMilliseconds -gt 0) { Start-Sleep -Milliseconds $RenderWaitMilliseconds }
        [WinOpsNative]::DwmFlush() | Out-Null
        $visibleRect = Get-WinOpsWindowRect -Handle $Handle -VisibleFrame
        $width = $visibleRect.Right - $visibleRect.Left
        $height = $visibleRect.Bottom - $visibleRect.Top
    }

    $bitmap = New-Object System.Drawing.Bitmap($width, $height, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    try {
        $graphics.CopyFromScreen($visibleRect.Left, $visibleRect.Top, 0, 0, $bitmap.Size, [System.Drawing.CopyPixelOperation]::SourceCopy)
    }
    finally { $graphics.Dispose() }
    return [PSCustomObject]@{ Bitmap = $bitmap; Moved = $moved }
}

function Save-AppWindowScreenshot {
    <#
    .SYNOPSIS
    DPI-correct screenshot of an entire window, including when covered or minimized.

    .DESCRIPTION
    Auto mode first uses PrintWindow, which can capture an obscured window without
    changing its state. It checks both the API result and the returned pixels because
    GPU-backed windows often return a blank bitmap while claiming success. If that
    path is unavailable or the window is minimized, the function temporarily restores,
    foregrounds, and if necessary moves the window on-screen before copying pixels.
    Unless -Foreground is specified, the original placement/minimized state and the
    previous foreground window are restored afterward.

    .PARAMETER Foreground
    Leaves the target restored and in the foreground. Without this switch, any
    temporary state/focus changes made by the fallback path are undone.

    .PARAMETER CaptureMode
    Auto (default) selects the least intrusive usable method. PrintWindow forbids
    screen fallback; Screen always restores/foregrounds and copies screen pixels.

    .PARAMETER RenderWaitMilliseconds
    Delay after restoring, foregrounding, or moving before screen capture. Increase
    this for applications that redraw slowly after being restored.
    #>
    param(
        [Parameter(Mandatory)][IntPtr]$Handle,
        [Parameter(Mandatory)][string]$Path,
        [switch]$Foreground,
        [ValidateSet("Auto", "PrintWindow", "Screen")]
        [string]$CaptureMode = "Auto",
        [ValidateRange(0, 5000)]
        [int]$RenderWaitMilliseconds = 300,
        [ValidateRange(100, 30000)]
        [int]$PrintWindowTimeoutMilliseconds = 1500,
        [switch]$PassThru
    )
    $stopwatch = [Diagnostics.Stopwatch]::StartNew()
    Set-WinOpsDpiContext
    Add-Type -AssemblyName System.Drawing -ErrorAction SilentlyContinue
    if (-not [WinOpsNative]::IsWindow($Handle)) { throw "Window handle $Handle is invalid or no longer exists." }

    $originalForeground = [WinOpsNative]::GetForegroundWindow()
    $originalPlacement = Get-WinOpsWindowPlacement -Handle $Handle
    $originalRect = Get-WinOpsWindowRect -Handle $Handle
    $wasMinimized = [WinOpsNative]::IsIconic($Handle)
    $wasMaximized = [WinOpsNative]::IsZoomed($Handle)
    $wasVisible = [WinOpsNative]::IsWindowVisible($Handle)
    $bitmap = $null
    $captureMethod = $null
    $capturedWidth = 0
    $capturedHeight = 0
    $printWindowTimedOut = $false
    $restorationErrors = [Collections.Generic.List[string]]::new()
    $stateRestored = $null
    $foregroundRestored = $null
    $stateChanged = $false
    try {
        if ($Foreground) {
            Set-ForceForeground -Handle $Handle
            $stateChanged = $wasMinimized -or -not $wasVisible
            if ($RenderWaitMilliseconds -gt 0) { Start-Sleep -Milliseconds $RenderWaitMilliseconds }
            [WinOpsNative]::DwmFlush() | Out-Null
        }

        $tryPrintWindow = $CaptureMode -ne "Screen" -and
            -not [WinOpsNative]::IsIconic($Handle) -and
            -not [WinOpsNative]::IsHungAppWindow($Handle)
        if ($tryPrintWindow) {
            $rect = Get-WinOpsWindowRect -Handle $Handle
            $visibleRect = Get-WinOpsWindowRect -Handle $Handle -VisibleFrame
            $width = $rect.Right - $rect.Left
            $height = $rect.Bottom - $rect.Top
            if ($width -gt 0 -and $height -gt 0) {
                foreach ($flags in [uint32[]]@(2, 0)) {
                    $captureResult = Invoke-WinOpsPrintWindowCapture -Handle $Handle -Rect $rect -Flags $flags -TimeoutMilliseconds $PrintWindowTimeoutMilliseconds
                    if ($captureResult.TimedOut) {
                        $printWindowTimedOut = $true
                        Write-Verbose "PrintWindow timed out after $PrintWindowTimeoutMilliseconds ms."
                        break
                    }
                    $candidate = $captureResult.Bitmap
                    $visibleCandidate = $null
                    if ($candidate) {
                        $visibleCandidate = Convert-WinOpsBitmapToVisibleFrame -Bitmap $candidate -OuterRect $rect -VisibleRect $visibleRect
                        $candidate.Dispose()
                    }
                    if ($visibleCandidate -and (Test-WinOpsBitmapHasContent -Bitmap $visibleCandidate)) {
                        $bitmap = $visibleCandidate
                        $captureMethod = "PrintWindow:$flags"
                        Write-Verbose "Captured with PrintWindow flags=$flags."
                        break
                    }
                    if ($visibleCandidate) { $visibleCandidate.Dispose() }
                }
            }
        }

        if (-not $bitmap) {
            if ($CaptureMode -eq "PrintWindow") {
                $reason = if ($printWindowTimedOut) { "timed out" } else { "did not return a usable image" }
                throw "PrintWindow $reason. Retry with -CaptureMode Auto or Screen."
            }
            $stateChanged = $true
            $screenCapture = Invoke-WinOpsScreenCapture -Handle $Handle -RenderWaitMilliseconds $RenderWaitMilliseconds
            $bitmap = $screenCapture.Bitmap
            $captureMethod = "Screen"
            Write-Verbose "Captured from the screen after foregrounding the window."
        }

        $capturedWidth = $bitmap.Width
        $capturedHeight = $bitmap.Height
        $dir = Split-Path -Parent $Path
        if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
        $bitmap.Save($Path, [System.Drawing.Imaging.ImageFormat]::Png)
    }
    finally {
        if ($bitmap) { $bitmap.Dispose() }
        if (-not $Foreground) {
            $stateRestored = $true
            $foregroundRestored = $true
            if ($stateChanged) {
                if (-not [WinOpsNative]::IsWindow($Handle)) {
                    $stateRestored = $false
                    $restorationErrors.Add("Target window closed before its state could be restored.")
                }
                else {
                    try {
                        if ($wasMinimized -or $wasMaximized) {
                            if (-not [WinOpsNative]::SetWindowPlacement($Handle, [ref]$originalPlacement)) {
                                throw "SetWindowPlacement failed."
                            }
                        }
                        else {
                            if (-not [WinOpsNative]::MoveWindow(
                                $Handle,
                                $originalRect.Left,
                                $originalRect.Top,
                                $originalRect.Right - $originalRect.Left,
                                $originalRect.Bottom - $originalRect.Top,
                                $true
                            )) { throw "MoveWindow failed." }
                        }
                        if (-not $wasVisible) { [WinOpsNative]::ShowWindow($Handle, $script:SW_HIDE) | Out-Null }
                        Start-Sleep -Milliseconds 40
                        $restoredRect = Get-WinOpsWindowRect -Handle $Handle
                        $rectMatches = [Math]::Abs($restoredRect.Left - $originalRect.Left) -le 2 -and
                            [Math]::Abs($restoredRect.Top - $originalRect.Top) -le 2 -and
                            [Math]::Abs(($restoredRect.Right - $restoredRect.Left) - ($originalRect.Right - $originalRect.Left)) -le 2 -and
                            [Math]::Abs(($restoredRect.Bottom - $restoredRect.Top) - ($originalRect.Bottom - $originalRect.Top)) -le 2
                        $stateMatches = [WinOpsNative]::IsIconic($Handle) -eq $wasMinimized -and
                            [WinOpsNative]::IsZoomed($Handle) -eq $wasMaximized -and
                            [WinOpsNative]::IsWindowVisible($Handle) -eq $wasVisible
                        if (-not $rectMatches -or -not $stateMatches) { throw "Window state verification did not match the original snapshot." }
                    }
                    catch {
                        $stateRestored = $false
                        $restorationErrors.Add($_.Exception.Message)
                    }
                }
                if ($originalForeground -ne [IntPtr]::Zero -and $originalForeground -ne $Handle -and [WinOpsNative]::IsWindow($originalForeground)) {
                    try {
                        Set-ForceForeground -Handle $originalForeground
                        $foregroundRestored = [WinOpsNative]::GetForegroundWindow() -eq $originalForeground
                        if (-not $foregroundRestored) { throw "Foreground verification failed." }
                    }
                    catch {
                        $foregroundRestored = $false
                        $restorationErrors.Add("Could not restore the previous foreground window: $($_.Exception.Message)")
                    }
                }
            }
        }
    }
    $stopwatch.Stop()
    if ($restorationErrors.Count -gt 0) {
        Write-Warning ("Screenshot was saved, but restoration was incomplete: " + ($restorationErrors -join " "))
    }
    if ($PassThru) {
        return [PSCustomObject]@{
            Path                = $Path
            CaptureMethod       = $captureMethod
            Width               = $capturedWidth
            Height              = $capturedHeight
            ElapsedMilliseconds = $stopwatch.ElapsedMilliseconds
            OriginalState       = if ($wasMinimized) { "Minimized" } elseif ($wasMaximized) { "Maximized" } elseif (-not $wasVisible) { "Hidden" } else { "Restored" }
            PrintWindowTimedOut = $printWindowTimedOut
            StateRestored       = $stateRestored
            ForegroundRestored  = $foregroundRestored
            RestorationErrors   = @($restorationErrors)
        }
    }
    return $Path
}

function Send-AppKeys {
    <#
    .SYNOPSIS
    Foregrounds a window and sends keystrokes to it via SendKeys.

    .DESCRIPTION
    Uses classic SendKeys syntax: "^k" = Ctrl+K, "%{F4}" = Alt+F4, "{ENTER}",
    "{ESC}", plain text is typed literally. This is a blunt instrument (no
    verification the keys landed where intended) - prefer it only for native-app
    smoke checks where no other automation surface exists (e.g. a Wails app has
    no Playwright/webdriver hook of its own). For anything rendered in a normal
    browser tab, use Playwright instead.
    #>
    param(
        [Parameter(Mandatory)][IntPtr]$Handle,
        [Parameter(Mandatory)][string]$Keys,
        [ValidateRange(0, 5000)][int]$FocusWaitMilliseconds = 100
    )
    Add-Type -AssemblyName System.Windows.Forms -ErrorAction SilentlyContinue
    Set-ForceForeground -Handle $Handle
    if ($FocusWaitMilliseconds -gt 0) { Start-Sleep -Milliseconds $FocusWaitMilliseconds }
    if ([WinOpsNative]::GetForegroundWindow() -ne $Handle) {
        throw "Refusing to send keys because handle $Handle is not the foreground window."
    }
    [System.Windows.Forms.SendKeys]::SendWait($Keys)
}
