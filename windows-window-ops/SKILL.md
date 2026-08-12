---
name: windows-window-ops
description: Find, enumerate, inspect, wait for, foreground, move, close, screenshot, or send keys to native Win32 desktop windows from PowerShell. Use for Wails, Electron, WinForms, and other desktop apps when Codex needs to confirm launch/state, handle secondary or hidden windows, capture covered/minimized/off-screen windows, visually verify UI, or drive a native app without Playwright/WebDriver. Do not use for normal browser tabs.
---

# Windows window operations

Version 0.6.1

A tested PowerShell module (`scripts/WindowOps.ps1`) wrapping the Win32 APIs for finding
and controlling native windows — desktop apps (Wails/Electron/etc.), not browser tabs.
Always use the `PowerShell` tool for this, not `Bash` — `Add-Type`/Win32 interop needs
real PowerShell.

**Don't hand-roll `Add-Type` P/Invoke blocks inline.** Dot-source the module and call its
functions instead — the signatures (especially the DPI-screenshot ordering and the
`GetWindowThreadProcessId` force-close path) are easy to get subtly wrong from memory, and
this module has already been verified against a live window.

Resolve the installed module once per PowerShell tool call, then dot-source it:

```powershell
$roots = @(
    $env:CLAUDE_SKILL_DIR,
    (Join-Path (Get-Location) ".agents\skills\windows-window-ops"),
    (Join-Path (Get-Location) "windows-window-ops"),
    (Join-Path $env:USERPROFILE ".agents\skills\windows-window-ops"),
    (Join-Path $env:USERPROFILE ".claude\skills\windows-window-ops"),
    (Join-Path $env:USERPROFILE ".codex\skills\windows-window-ops"),
    (Join-Path $env:USERPROFILE ".config\opencode\skills\windows-window-ops")
) | Where-Object { -not [string]::IsNullOrEmpty($_) }
$module = $roots | ForEach-Object { Join-Path $_ "scripts\WindowOps.ps1" } |
    Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $module) { throw "Could not locate windows-window-ops/scripts/WindowOps.ps1" }
. $module
```

Dot-sourcing is idempotent. Each PowerShell tool invocation is a fresh process, so source it
at the top of every call. Native interop is compiled once per source/runtime version and cached
under `%LOCALAPPDATA%\Codex\WindowOps`; later processes load the cached assembly.

## Functions

| Function | Purpose |
|---|---|
| `Get-AppWindows [-ProcessName <name>] [-ProcessId <pid>] [-TitleLike <pattern>] [-IncludeHidden]` | Enumerates every matching top-level window in z-order, including secondary windows that `Process.MainWindowHandle` misses. |
| `Get-AppWindow ... [-TimeoutSeconds <n>]` | Returns the first matching window, optionally polling for app startup. Default polling interval is 250 ms. |
| `Get-WindowInfo -Handle <h>` | Returns physical outer/visible-frame coordinates, DPI, title/class/process, visibility, minimized/maximized, foreground, hung, and existence state. |
| `Wait-AppWindowState -Handle <h> -State Visible\|Hidden\|Minimized\|Maximized\|Restored\|Foreground\|Closed` | Waits deterministically for an asynchronous window transition; throws on timeout. |
| `Show-AppWindow -Handle <h> -Mode Foreground\|Minimize\|Maximize\|Restore\|Hide` | `Foreground` (default) restores-if-minimized, raises z-order, and focuses — use this to "show the user the window now". |
| `Move-AppWindow -Handle <h> -X <x> -Y <y> -Width <w> -Height <h>` | Reposition/resize (physical pixels). |
| `Close-AppWindow -Handle <h> [-Wait] [-TimeoutSeconds <n>] [-Force]` | Sends `WM_CLOSE`, optionally waits for closure. `-Force` kills the owning process; use only for a stuck/leftover process. |
| `Save-AppWindowScreenshot -Handle <h> -Path <path> [-Foreground] [-CaptureMode Auto\|PrintWindow\|Screen] [-PassThru]` | Captures covered/minimized/off-screen windows with an isolated PrintWindow timeout and verified state restoration. `-PassThru` returns method, dimensions, duration, timeout, and restoration diagnostics. |
| `Send-AppKeys -Handle <h> -Keys <sendkeys-string>` | Reliably foregrounds, verifies focus, then sends classic SendKeys syntax. Screenshot afterward to verify UI effect. |

Prefer `Get-AppWindow -ProcessId $process.Id` after launching an app. Use `Get-AppWindows`
when the process can own dialogs, splash screens, secondary windows, or hidden windows.

## Recipe: "I just started a dev server, show me the window and confirm it rendered"

```powershell
# Resolve and dot-source $module as shown above.
$win = Get-AppWindow -ProcessName "<processName>" -TimeoutSeconds 15
if (-not $win) { throw "Window never appeared for process <processName>" }
Show-AppWindow -Handle $win.Handle -Mode Foreground
$shot = Save-AppWindowScreenshot -Handle $win.Handle -Path "<scratchpad-dir>\window.png" -Foreground
$shot
```

Then `Read` the PNG to visually verify before telling the user it rendered correctly — don't
claim success from the PowerShell exit code alone.

## Known pitfalls (already fixed in the module — know them so you don't reintroduce them)

- **DPI virtualization silently changes coordinates.** All public coordinate operations enter
  Per-Monitor V2 awareness and report physical pixels. Do not call `GetWindowRect` directly or
  mix module coordinates with values captured before sourcing/calling the module.
- **`GetWindowRect` includes invisible resize borders.** Screen fallback uses DWM extended
  frame bounds, so maximized screenshots do not include adjacent-monitor pixels or black edges.
- **Minimized, DPI-unaware, and GPU-backed windows cannot be trusted to render through
  `PrintWindow`.** The API can return success with a tiny title-bar image, an all-black client
  area, or a logical-resolution image padded by black pixels. Auto mode validates the rendered
  pixels and, when necessary, temporarily restores and foregrounds the
  window for a screen capture. Without `-Foreground`, it restores the original placement,
  minimized/hidden state, and previous foreground window afterward. Use `-CaptureMode Screen`
  to force that reliable path when an app returns a plausible-looking but stale backing layer.
- **`PrintWindow` can block inside another process.** It runs in a small disposable capture
  host with a 1500 ms default timeout. Adjust `-PrintWindowTimeoutMilliseconds` for unusually
  slow applications; Auto mode falls back to screen capture after a timeout. The capture host
  adopts the target window's DPI-awareness context before rendering; forcing one global DPI
  mode truncates either modern Per-Monitor V2 windows or legacy DPI-unaware windows.
- **Foreground focus doesn't survive across separate tool calls.** Each `PowerShell` tool
  invocation can itself steal focus back (e.g. this terminal). `Save-AppWindowScreenshot
  -Foreground` and `Send-AppKeys` foreground-then-act in one call for this reason — if you
  split foregrounding and the next action across two tool calls, the action may land on the
  wrong window.
- **`[string]` parameters default to `""`, not `$null`, when unbound.** If you extend this
  module, check optional string params with `[string]::IsNullOrEmpty($x)`, not `$null -eq $x`
  — the latter silently filters out everything (this bug shipped in v0.1.0 of
  `Get-AppWindow`'s `-TitleLike` handling and was caught by testing against a live window,
  not by reading the code).
- **A plain `SetForegroundWindow` call frequently no-ops.** Windows' foreground-lock heuristic
  lets it silently do nothing (still returns success) when called from a process that wasn't
  already foreground — which a PowerShell tool invocation from an agent always is. Confirmed
  empirically: `ShowWindow`+`BringWindowToTop`+`SetForegroundWindow` left a background terminal
  focused and a screenshot captured the wrong window, with every call reporting success. Fixed
  in `Set-ForceForeground` (used internally by `Show-AppWindow -Mode Foreground`,
  `Save-AppWindowScreenshot -Foreground`, and `Send-AppKeys`) via the standard
  `AttachThreadInput` trick: attach this thread's input queue to the current foreground
  window's thread before calling `SetForegroundWindow`, then detach. If you ever call
  `SetForegroundWindow` directly instead of going through `Show-AppWindow`, verify it actually
  worked by comparing `GetForegroundWindow()` to your target handle afterward — don't trust the
  boolean return value.
- **Do not call `SW_RESTORE` merely to focus a window.** It unmaximizes maximized windows.
  `Set-ForceForeground` restores only minimized windows, preserves maximized state, retries,
  and verifies the actual foreground handle before returning.
- **`Process.MainWindowHandle` exposes only one window.** It can select a console/splash window
  or omit secondary dialogs. Use `Get-AppWindows`/`Get-AppWindow`, which enumerate real top-level
  windows and support process-id, title, visibility, z-order, class, and hung-state inspection.
- **`GetWindowThreadProcessId`'s `out uint` parameter needs `[ref]`, not `[IntPtr]::Zero`.**
  Passing the wrong type throws a `MethodException` at the call site — PowerShell reports it as
  a non-terminating error by default, so a script can appear to keep running successfully past
  a broken P/Invoke call. Always pass `[ref]$someUint32Variable`, never a placeholder value.

## Notes

- Save scratch screenshots to the session scratchpad directory, not the repo, unless the user
  asks for a committed asset.
- These are OS-level window operations, unrelated to browser/Playwright automation — don't
  reach for this skill when the target is a web page in a normal browser tab.
- If you add a function, test it live against a real window in the same turn (this module's
  functions all were) — Win32 interop bugs (wrong struct layout, wrong default value, wrong
  message constant) are easy to write confidently and wrong.
- Run `scripts/Test-WindowOps.ps1` after module changes. It launches disposable WinForms
  fixtures and validates DPI, multi-window discovery, screenshots, restoration, and input.
- Windows security boundaries still apply: a non-elevated process cannot reliably control an
  elevated window, and UAC secure desktop, lock screen, another user session, or a disconnected
  RDP desktop cannot be captured or driven by this module.
