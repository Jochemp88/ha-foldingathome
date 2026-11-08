# Preparing the Folding@home client

By default the Folding@home v8 client only listens on `127.0.0.1:7396`, so only
the computer itself can talk to it. These steps let **only your Home Assistant
host** connect as well. Anyone who can connect can also control the client, so
do not open it up any wider than that.

In the examples below, replace `HA_ADDRESS` with the IP address of your Home
Assistant host (for example `192.0.2.10`).

## Windows

Run everything in **PowerShell as administrator** (Start → type `PowerShell` →
right-click → *Run as administrator*).

### 1. Check for an existing config.xml

```powershell
Test-Path C:\ProgramData\FAHClient\config.xml
```

- `False`: continue with step 2.
- `True`: open the file (`notepad C:\ProgramData\FAHClient\config.xml`) and add
  the three lines from step 3 inside the existing `<config>` element instead of
  overwriting the file.

### 2. Firewall rule (only Home Assistant, only port 7396)

Do this before restarting the client, so Windows does not ask to allow it.

```powershell
New-NetFirewallRule -DisplayName "Folding@home for Home Assistant" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 7396 -RemoteAddress HA_ADDRESS -Program "C:\Program Files\FAHClient\FAHClient.exe" -Profile Any
```

If Folding@home is installed elsewhere, adjust the `-Program` path.

### 3. Write config.xml

```powershell
@'
<config>
  <http-addresses v='0.0.0.0:7396'/>
  <allow v='127.0.0.1 HA_ADDRESS'/>
  <deny v='0/0'/>
</config>
'@ | Set-Content -Encoding ascii C:\ProgramData\FAHClient\config.xml
```

Keep `127.0.0.1` in the `allow` list, otherwise the web control on the computer
itself stops working.

### 4. Restart the client

Right-click the Folding@home icon in the notification area → **Quit**, then
start **Folding@home** again from the Start menu.

### 5. Check

```powershell
Get-Content (Get-ChildItem C:\ProgramData\FAHClient\logs | Sort-Object LastWriteTime | Select-Object -Last 1).FullName | Select-String "Listening for HTTP"
```

You should see `Listening for HTTP on 0.0.0.0:7396`.

Look up the computer's IP address (line *IPv4 Address*) with `ipconfig`. That
is the address you enter in Home Assistant. A DHCP reservation in your router
keeps it from changing; if it changes anyway, use *Reconfigure* on the
integration.

### Undo

```powershell
Remove-Item C:\ProgramData\FAHClient\config.xml
Remove-NetFirewallRule -DisplayName "Folding@home for Home Assistant"
```

Then restart the client as in step 4.

## Linux and macOS

The same three settings go into the client's `config.xml`. Its location depends
on how the client was installed; common locations are:

- Linux (package install): `/etc/fah-client/config.xml`
- macOS: `/Library/Application Support/FAHClient/config.xml`

```xml
<config>
  <http-addresses v='0.0.0.0:7396'/>
  <allow v='127.0.0.1 HA_ADDRESS'/>
  <deny v='0/0'/>
</config>
```

Restart the client afterwards (for example `sudo systemctl restart fah-client`
on Linux) and allow incoming TCP port 7396 from `HA_ADDRESS` in your firewall,
if one is active.

## Optional (Windows): stay awake until the work unit is saved

Folding@home's *Keep Awake* setting only keeps the computer awake while it is
folding. When you pause (for example from Home Assistant), it releases the
computer at once, but the folding core (FahCore) can need half a minute or more
to save its work and exit. If Windows goes to sleep during that time, the work
unit can be lost.

A hidden scheduled task can keep the computer awake while a FahCore process is
running, plus 10 minutes afterwards (so the result can be uploaded and a
shutdown triggered right after pausing can complete). Manual sleep and shutdown
keep working; only automatic sleep is held off.

Run in PowerShell as administrator. First write the script:

```powershell
New-Item -ItemType Directory -Force C:\ProgramData\FahKeepAwake | Out-Null; Set-Content -Encoding ASCII -Path C:\ProgramData\FahKeepAwake\keepawake.ps1 -Value 'Add-Type -Name K -Namespace FahKeepAwake -MemberDefinition "[DllImport(""kernel32.dll"")] public static extern uint SetThreadExecutionState(uint f);"; $last = [datetime]::MinValue; while ($true) { if (Get-Process -Name FahCore* -ErrorAction SilentlyContinue) { $last = Get-Date }; if (((Get-Date) - $last).TotalMinutes -lt 10) { $null = [FahKeepAwake.K]::SetThreadExecutionState(2147483649) } else { $null = [FahKeepAwake.K]::SetThreadExecutionState(2147483648) }; Start-Sleep -Seconds 15 }'; Get-Content C:\ProgramData\FahKeepAwake\keepawake.ps1
```

Then create and start the task:

```powershell
Register-ScheduledTask -TaskName 'Folding@home keep awake' -Description 'Keeps the PC awake while a FahCore process runs' -User SYSTEM -RunLevel Highest -Force -Trigger (New-ScheduledTaskTrigger -AtStartup) -Settings (New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)) -Action (New-ScheduledTaskAction -Execute powershell.exe -Argument '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File C:\ProgramData\FahKeepAwake\keepawake.ps1') | Out-Null; Start-ScheduledTask -TaskName 'Folding@home keep awake'; Start-Sleep 20; (Get-ScheduledTask -TaskName 'Folding@home keep awake').State; powercfg /requests
```

The state should be `Running`. While folding, `powercfg /requests` lists
`powershell.exe` under `SYSTEM:`.

To remove it:

```powershell
Stop-ScheduledTask -TaskName 'Folding@home keep awake'; Unregister-ScheduledTask -TaskName 'Folding@home keep awake' -Confirm:$false; Remove-Item -Recurse C:\ProgramData\FahKeepAwake
```
