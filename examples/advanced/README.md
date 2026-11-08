# Advanced examples (optional)

These examples come from a real setup that has been running since October 2026,
made generic. They show what you can build on top of the integration. None of
this is needed to use it.

| File | What it does |
| --- | --- |
| [`folding_package.yaml`](folding_package.yaml) | Helpers, template sensors, scripts and automations (a Home Assistant [package](https://www.home-assistant.io/docs/configuration/packages/)). |
| [`dashboard.yaml`](dashboard.yaml) | A dashboard that goes with it. |

## What you get

**Fold on solar power.** A switch on the dashboard. When there is a solar
surplus for 5 minutes, Home Assistant switches the PC on with Wake on LAN and
starts folding. When the surplus has been gone for 30 minutes it presses
*Finish*, so the running work unit is completed instead of paused halfway. When
that work unit is done, folding stops and the PC shuts down. At the end of the
afternoon it pauses and shuts down, unless the work unit is done within an hour.
If the sun comes back while finishing, it simply continues. There is a safety
stop at 22:00.

- The surplus is measured *without the PC itself* (PC power minus net grid
  power). Otherwise the PC's own consumption would eat the surplus and it would
  switch itself off again.
- The grid power is averaged over 10 minutes, so a kettle or oven does not stop
  folding right away.
- Start and stop thresholds (default 450 W and 200 W) are sliders on the
  dashboard.

**Fold before the timeout.** The *Work unit timeout* sensor keeps working while
the PC is off. When a work unit would not make its timeout on solar power alone
(remaining compute time plus 6 hours margin), the PC is switched on and the
work unit is finished anyway. You get a notification, and it happens only once
per work unit, so pausing or switching off by hand afterwards is respected.

**Switch the PC off after Finish.** Press *Finish* and walk away: when the work
unit is done and uploaded, the PC shuts down. Folding@home is always paused
first, so the folding core can save its work.

**Never in your way.** The PC is only shut down when nobody used it in the last
10 minutes (keyboard/mouse). If you just used it, you get a notification and
the PC stays on. The solar automation only shuts down a PC it switched on
itself.

**Dashboard.** A status card with progress bar, project, PPD and expected
finish. Only the buttons that make sense right now are shown (*Switch on*,
*Fold*, *Pause*, *Finish*, *Continue*, *Switch off*). Further: the PC's power
and energy today, the solar surplus with its thresholds and a graph, and donor
statistics with points earned per day.

## What you need besides this integration

| Purpose | Example integration | Entity in the examples |
| --- | --- | --- |
| Switch the PC on | [Wake on LAN](https://www.home-assistant.io/integrations/wake_on_lan/) (enable WoL in the BIOS and network adapter) | `button.my_pc_wake` |
| Switch the PC off and see when it was last used | [HASS.Agent](https://github.com/hass-agent/HASS.Agent) or a similar agent on the PC | `button.my_pc_shutdown`, `sensor.my_pc_last_active` |
| Power of the PC | A smart plug with power metering | `sensor.my_pc_power` |
| Energy today (dashboard only) | A [utility meter](https://www.home-assistant.io/integrations/utility_meter/) on the plug's energy sensor | `sensor.my_pc_energy_today` |
| Net grid power (import positive) | A P1 / smart meter integration | `sensor.grid_power` |
| Solar production | Your inverter integration | `sensor.solar_power` |
| Notifications | The Home Assistant companion app | `notify.mobile_app_my_phone` |
| Dashboard cards | [button-card](https://github.com/custom-cards/button-card), [Mushroom](https://github.com/piitaya/lovelace-mushroom), [card-mod](https://github.com/thomasloven/lovelace-card-mod) | |

The Folding@home entities assume a machine called **My PC**
(`sensor.my_pc_status`, `button.my_pc_fold`, ...). Replace `my_pc` with the
prefix of your machine, and the other entity IDs with your own.

## Installation

1. Copy `folding_package.yaml` to `<config>/packages/` and add this to
   `configuration.yaml` (if you do not use packages yet):

   ```yaml
   homeassistant:
     packages: !include_dir_named packages
   ```

2. Replace the entity IDs, check the configuration (**Developer tools → YAML →
   Check configuration**) and restart Home Assistant.
3. Create a new dashboard, open ⋮ → **Edit dashboard** → ⋮ → **Raw
   configuration editor** and paste `dashboard.yaml`.
4. Turn on **Fold on solar power** on the dashboard.

## Tip: keep Windows awake while the core saves

Folding@home's *Keep awake* setting stops as soon as you pause. The folding core
can still need half a minute or more to save and exit, and if Windows goes to
sleep in that time the work unit can be lost. See the optional
[keep-awake task](../../docs/client-setup.md#optional-windows-stay-awake-until-the-work-unit-is-saved)
in the client setup guide.
