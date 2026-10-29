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
   - Relaxing this for an administrator's account (letting its non-elevated Beamer drive UAC)
     would let any program of that user approve its own UAC prompt, which is one Yes click there.
   - **A standard account's own Beamer, unelevated.** A standard user's UAC prompt always asks for
     an administrator's password, so a program pressing keys through the pipe approves nothing
     without it. When an administrator runs the installer for a signed-in standard user, it writes
     that user's SID as `StandardUser`; the helper then also takes that account's unelevated
     Beamer (same session and path checks), but only while its token holds no Administrators group
     in any state (an administrator's filtered token keeps it, deny-only). Running Beamer as that
     user, rather than elevated with an administrator's credentials, keeps its settings, folders
     and theme that user's own. What any program of that user then gains is typing on that
     session's lock screen and UAC prompts as a keyboard would, never an approval or a password.
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

## Administrator windows for a standard account (opt-in, `-ElevatedWindows`)

Windows refuses an unelevated Beamer's input while an administrator window (an elevated PowerShell,
Task Manager) is in front. Installed with `-ElevatedWindows`, the helper also takes the standard
account's Beamer's input on the normal desktop: Beamer passes it what Windows refused there with
such a window in front, as it does for the secure desktop. Without the switch the helper answers
that administrator windows are not allowed, and nothing changes.

- **What it costs.** The administrator password guards opening an administrator window, not the
  window once open: with this on, any program of that account could type into an administrator
  window while one is open. ShareMouse's service makes the same trade. UAC prompts themselves still
  ask for the password.
- An elevated Beamer's input always may go there, as it could reach those windows anyway.

## Before anyone signs in (the sign-in screen)

Beamer is a per-user app, started when someone signs in, so after a restart or a sign-out nothing
listened for the paired machines. As ShareMouse and Input Director do, the service now also starts
Beamer itself in the right place: while nobody is signed in to the console session it runs
`BeamerSecure.exe --logon-receiver` (logon_receiver.py) there as SYSTEM, on the Winlogon desktop,
and ends it as soon as a user name appears on that session; that user's own Beamer then takes the
port (its listener retries every 5 s). Nothing signs in for the user: the password is typed from
the driving machine as at the PC's own keyboard, and no credentials are stored anywhere.

- **Only machines already paired.** The installer copies the signed-in user's settings.json (or
  the one beside `-ClientPath`) to `C:\Program Files\Beamer\logon\settings.json`, in a folder whose
  ACL is SYSTEM and Administrators only, as it holds the pairing keys. The receiver links under
  those pairings alone, as the same machine id, so the driving machines see the same PC. It never
  pairs (the pairing port is not opened) and never writes the copy; run the installer again after
  pairing.
- **Found where its address changed.** Like the app with no pairing code up, it broadcasts its
  name and link port every 2 s on the pairing port (no id), which paired machines dial by name.
- **Keys and the pointer, nothing else.** Its `welcome` lists `layout` alone, so paired machines
  cross to it by the shared picture as they do to its app; pictures travel only with `settings`, so
  no clipboard, files, settings, picture or monitors messages reach it.
- **Firewall**: `Beamer Sign-in Screen (TCP-In)`, inbound TCP on that port, Private profile, for
  BeamerSecure.exe only.
- **What it adds to the attack surface**: the link's handshake and input parsing, now also as
  SYSTEM. It is the same authenticated, encrypted link (ChaCha20-Poly1305, per-pairing keys) the
  app uses, so an unpaired host gets no further than the handshake.
- **Ended as a whole.** Each process the service starts is in its own kill-on-close job, and
  stopping it ends the job: the exe is PyInstaller's launcher with the program as its child, and
  ending the launcher alone once left a receiver holding the port after sign-in. The receiver also
  ends itself within a second of a user name appearing on its session.
- **Then the user's own Beamer.** For the standard account the installer named, the service starts
  that account's Beamer.exe once per sign-in, as the account (WTSQueryUserToken), unelevated, once
  Explorer runs and unless Beamer already does: a Beamer copied in by hand has no sign-in task.
- `-NoSignInScreen` leaves it out; `-Remove` takes away the folder and the firewall rule.

## Testing plan (needs the user, as administrator)

1. Install; check the service runs and a helper runs as SYSTEM in the console session.
2. From the Mac, on this PC: lock (Win+L) and unlock by typing the password; open an elevated
   app and approve its UAC prompt, typing a password on a standard account; send Ctrl+Alt+Del.
3. With Beamer not elevated: the pipe refuses it (logged), and nothing else changes.
4. Uninstall leaves no service, file, or registry value behind.
