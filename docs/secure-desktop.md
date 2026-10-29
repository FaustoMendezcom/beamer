# Secure desktop input (Windows): design

Status: in progress on `feature/secure-desktop`. Not installed anywhere yet.

## Goal

Let a machine driving a Windows PC type and click on that PC's **secure desktop**: UAC prompts
(including typing an administrator password), the lock screen, the Ctrl+Alt+Del screen, and send
Ctrl+Alt+Del itself. Today Beamer cannot: the secure desktop ("Winlogon") admits input only
from SYSTEM, so `SendInput` from Beamer fails there, elevated or not (README, Known limits).

## How others do it

ShareMouse, Mouse Without Borders and VNC servers (TightVNC, UltraVNC) all run a Windows service
as LocalSystem. The service starts a helper process **as SYSTEM inside the signed-in user's
session**; that helper attaches to whichever desktop has the input (Default or Winlogon) and
calls `SendInput` there. Ctrl+Alt+Del is `SendSAS(FALSE)` from sas.dll, allowed for services when
the `SoftwareSASGeneration` policy permits it.

## Design

```
 owner (Mac / PC) ──link──▶ Beamer.exe (user's app, elevated or not)
                               │ input_injector._send_input
                               │   input desktop is Default ─▶ SendInput (as today)
                               │   input desktop is Winlogon ─▶ secure_input client
                               ▼
                  \\.\pipe\BeamerSecureInput  (local, per session)
                               ▼
        BeamerSecure.exe --secure-helper   (SYSTEM, in the user's session)
            follows the input desktop (OpenInputDesktop + SetThreadDesktop), SendInput, SendSAS
                               ▲ started and restarted per console session by
        service "BeamerSecureDesktop"      (LocalSystem, session 0)
```

- **One binary.** The service, the helper and the app are the same Beamer build, selected by
  `--service` / `--secure-helper`.
- **The pipe carries raw `INPUT` records**, exactly what `_send_input` would have passed to
  `SendInput`, so every key and mouse path keeps working unchanged; plus `sas` and `ping`.
- **Ctrl+Alt+Del** from a driving machine (Ctrl and Alt held, Delete pressed) is sent as `sas`
  rather than typed, as Windows ignores a typed Ctrl+Alt+Del.

## Security model (the part that matters)

A SYSTEM component that types into UAC prompts must not let ordinary programs approve their own
elevation, nor run code as SYSTEM.

1. **The service binary lives where only administrators can write**: the installer (already
   elevated, Install-Maintenance.ps1) copies Beamer.exe to `C:\Program Files\Beamer\BeamerSecure.exe`
   and registers the service there. The service never runs the per-user copy in
   `%LOCALAPPDATA%`, which any program of the user could replace.
2. **Only an elevated Beamer may use the pipe.** The helper checks every client
   (`GetNamedPipeClientProcessId`): same session; image path equal to the allowed client path the
   installer wrote under `HKLM\SOFTWARE\Beamer\SecureDesktop`; and its token elevated
   (`TokenElevation`). A program running with normal rights therefore gains nothing it could not
   already do: to drive a UAC prompt through the pipe it must already be elevated.
   - Consequence: secure desktop input works while Beamer runs as administrator (this PC's
     setting today). A non-elevated Beamer keeps today's behaviour on the secure desktop.
   - Relaxing this (letting a non-elevated Beamer drive UAC) would let any program of the user
     approve its own UAC prompt. Not done without a separate decision.
3. **Pipe DACL**: SYSTEM and the session's interactive user only; remote clients rejected
   (`PIPE_REJECT_REMOTE_CLIENTS`).
4. **Only input, SAS and ping** are accepted; each message is length-checked; the helper never
   runs anything it is sent.
5. **Opt-in at install**, with an uninstall path that removes the service, the protected copy and
   the policy value it set.

## Install / uninstall (administrator, once)

Install-Maintenance.ps1 gains a `-SecureDesktop` step:

- copy the exe to `C:\Program Files\Beamer\BeamerSecure.exe` (ACL: Administrators/SYSTEM write);
- `HKLM\SOFTWARE\Beamer\SecureDesktop\AllowedClient` = the user's Beamer.exe path;
- service `BeamerSecureDesktop`, LocalSystem, automatic start, binPath `"...\BeamerSecure.exe" --service`;
- `HKLM\...\Policies\System\SoftwareSASGeneration` = 1 (services may send Ctrl+Alt+Del) if unset.

`-Remove` stops and deletes the service, the protected copy, the HKLM key, and the policy value if
Beamer set it.

## Testing plan (needs the user, as administrator)

1. Install; check the service runs and a helper runs as SYSTEM in the console session.
2. From the Mac, on this PC: lock (Win+L) and unlock by typing the password; open an elevated
   app and approve its UAC prompt, typing a password on a standard account; send Ctrl+Alt+Del.
3. With Beamer not elevated: the pipe refuses it (logged), and nothing else changes.
4. Uninstall leaves no service, file, or registry value behind.
