# Becker Centronic USB for Home Assistant

> [!WARNING]
> **Current version: `0.4.1-beta.9`.** This is a beta build for the Home Assistant 2026.8 development line.
> Back up the sender-counter database before changing installations. Only one controller may use the Becker stick and its database at a time.

This integration controls Becker Centronic roller shutters and venetian blinds through a Becker Centronic USB stick (order numbers 4035 200 041 0 and 4035 000 041 0). It also supports compatible Roto roof-window receivers.

The integration sends radio commands through the USB stick. Receivers do not report movement back to the stick, so Home Assistant estimates position from travel time and received remote commands. A successful Home Assistant action does not confirm that a motor received the radio command.

## Features

- Open, close and stop covers.
- Set a cover position when travel times are configured.
- Estimate position from travel times, physical remote commands and an optional value template.
- Configure venetian-blind slat control using programmed intermediate positions or short tilt pulses.
- Move to a configured sun-protection position.
- Correct the estimated position without sending a radio command.
- Pair a receiver with a sender unit, inspect configured sender units and receive remote-command events.
- Configure the integration and individual covers through Home Assistant's integration settings.

This integration creates one Becker USB hub entry. Each configured cover appears as its own Home Assistant device under that hub.

## Install

### HACS

1. In HACS, open the menu and choose **Custom repositories**.
2. Add [this repository](https://github.com/tkarle/hass-becker-component-plus-pybecker) as an Integration.
3. Install Becker and restart Home Assistant when prompted.
4. Connect the Becker USB stick to the Home Assistant host. A short USB extension cable can help keep it away from other RF devices.
5. Continue with [Add the integration](#add-the-integration).

### Manual install

Copy the folder `custom_components/becker` from this repository into the `custom_components` folder in your Home Assistant configuration directory, then restart Home Assistant.

## Add the integration

Before starting, locate the existing Becker sender-counter database. The default path is `/config/centronic-stick.db`. The UI setup validates this file and reuses it; it does not create, reset or replace it. The file must be inside the Home Assistant configuration directory.

1. In Home Assistant, go to **Settings → Devices & services → Add integration** and search for **Becker**.
2. Choose the USB serial device. Prefer the stable `/dev/serial/by-id` path when it is available.
3. Enter the path to the existing sender-counter database. The default is `/config/centronic-stick.db`.
4. Add a cover:
   - **Name**: the name Home Assistant will show.
   - **Sender unit and channel**: choose one of the offered unit:channel pairs. Only sender units already present in the database are offered.
   - **Cover type**: choose **Roller shutter** or **Venetian blind with slats**.
   - **Enable programmed intermediate position**: leave this enabled only when the receiver has programmed intermediate positions. It is used by blinds; roller shutters do not use the blind controls.
   - Select **Add another cover** to enter another cover before finishing.
5. Finish the flow. The Becker hub and its cover devices appear under **Settings → Devices & services**.

The same sender unit can address multiple channels. Do not add a channel twice. The integration supports sender units 1–5 and channels 1–7.

### Position tracking

The integration has no receiver feedback. Configure at least one full travel time to enable the position estimate and the Home Assistant position slider.

- **Full travel time up** is the time from fully closed to fully open.
- **Full travel time down** is the time from fully open to fully closed.
- If only one direction is entered, that time is used for both directions.
- Home Assistant restores the last estimated position after a restart. This estimate can drift if a command is missed or another controller moves the cover without the integration receiving its remote packet.

Use a physically observed position to correct the estimate with [Set known position](#actions-and-services). That correction sends no radio command.

## Edit the hub or a cover

1. Go to **Settings → Devices & services**.
2. Open the Becker integration entry and choose **Configure**.
3. Choose **USB hub and counter database** to change the serial device or select another existing, valid database.
4. Choose **Configure a cover**, select the cover, and edit its options.

The cover's sender unit and channel stay fixed when editing. Its entity ID also stays tied to that channel. The current settings flow edits covers already in the hub; adding covers is available during initial setup.

### Cover options

- **Name** changes the displayed cover name.
- **Cover type** selects roller shutter or venetian blind.
- **Full travel time up/down** controls position estimation and enables the position slider.
- **Position value template** is an advanced option that can set the estimate from another Home Assistant entity.
- **Physical remote IDs** filters which received remote commands update this cover's estimate. Enter IDs such as `12345:2`; separate multiple IDs with commas.
- **Sun-protection position** is a Home Assistant position from 0 (closed) to 100 (open).

For blinds, saving the first form opens a second **Configure slats** form:

- **Enable programmed intermediate positions** tells the integration to track the receiver's configured UP and DOWN intermediate targets.
- **Tracked position after intermediate UP/DOWN** records the corresponding target position in Home Assistant.
- **Slat controls** can be **Disabled**, **Programmed intermediate positions**, or **Blind tilt with short pulse**.
- **Short pulse duration for slats** applies only in Blind tilt mode. The default is 0.3 seconds.

Programmed intermediate controls require the receiver positions to have been programmed first. Choose Disabled if the receiver has no such positions. Roller shutters do not expose the slat options.

Changing these options reloads the integration. The channel and sender-counter database are not changed by editing a cover.

### Position value template

The optional template can return a number from 0 to 100, open/closed, true/false, or an unknown value. Unknown values leave the current estimate unchanged. For example, a rain sensor could force a roof-window estimate to closed while travel-time tracking handles other movement:

```yaml
cover:
  - platform: becker
    covers:
      roof_window:
        channel: "3:1"
        travelling_time_up: 15
        value_template: "{{ 0 if is_state('sensor.roof_window', 'closed') else None }}"
```

Templates can override the travel-time estimate when they produce a known value.

## Actions and services

Find these actions in **Developer tools → Actions**. Select the Becker action and enter its fields.

### Move to sun protection

Use `becker.move_to_sun_protection` on the target cover.

```yaml
action: becker.move_to_sun_protection
target:
  entity_id: cover.living_room
```

For a roller shutter, configure a sun-protection percentage first. A venetian blind uses its configured percentage, or sends the receiver's programmed DOWN intermediate command when no percentage is configured.

### Move to programmed DOWN intermediate

Use `becker.move_down_intermediate` to send the receiver's programmed DOWN2 command. When intermediate tracking is enabled, Home Assistant also updates the estimated position to the configured DOWN target.

```yaml
action: becker.move_down_intermediate
target:
  entity_id: cover.living_room
```

### Set known position

Use `becker.set_known_position` after checking the cover's physical position. This only corrects Home Assistant's estimate and does not send a radio command.

```yaml
action: becker.set_known_position
target:
  entity_id: cover.living_room
data:
  position: 50
```

Position is a percentage: 0 is closed and 100 is open.

### Pair a receiver

Pairing sends radio commands and requires the intended receiver to be put into programming mode with its existing master remote first. Follow the receiver and remote manuals. Do not run pairing as a diagnostic or when another Becker controller is using the sender-counter database.

In Developer tools → Actions, choose `becker.pair` and enter the receiver channel and sender unit:

```yaml
action: becker.pair
data:
  unit: 1
  channel: 1
```

The unit is 1–5 and defaults to 1; the channel is 1–7. The receiver should acknowledge programming mode and then confirm pairing. If the final confirmation does not happen, put the intended receiver back into programming mode before retrying. Do not send ordinary travel commands until pairing has been confirmed.

### Log configured sender units

`becker.log_units` writes the configured sender unit numbers and their counter values to the Home Assistant log. It sends no radio command.

### Remote command event

The integration fires `becker_remote_packet_received` when it receives a Becker remote packet. Automations can use the broad command and the more specific `action` or `command_code` fields.

```yaml
event_type: becker_remote_packet_received
data:
  unit: "12345"
  channel: "2"
  command: "up"
  action: "up_intermediate"
  argument: "4"
  command_code: "24"
```

The action and command_code distinguish normal travel, intermediate commands and blind remote actions. Unknown command bytes are also emitted with their raw code.

## Migrate an existing YAML setup to the UI

The integration can copy an active Becker YAML platform into a passive UI hub. The passive hub does not open the serial port or database while the YAML platform is still active.

1. Back up the Home Assistant configuration and `centronic-stick.db`.
2. Add the Becker integration. If an active Becker YAML setup is detected, Home Assistant offers **Import existing Becker setup**. Confirm to copy the current device path, database path and cover settings.
3. Remove the Becker YAML platform from the Home Assistant configuration. Keep the backup until the migration is complete.
4. Restart Home Assistant. Do not leave both the YAML platform and UI integration active.
5. Open the imported Becker hub, choose **Configure**, and complete **Activate the imported UI hub**.
6. Check that the expected cover entities are present before using them.

The import keeps each cover's channel and therefore its entity identity. It reuses the existing database. Never run this integration, the old MQTT bridge or another pybecker process against the same database at the same time.

## YAML compatibility

The legacy YAML platform remains available for existing installations and advanced setups. New installations should use the UI flow. The main YAML options are:

- device: serial device path
- filename: existing Becker sender-counter database
- channel: sender unit and channel, such as "2:1"
- travelling_time_up and travelling_time_down
- value_template
- remote_id
- cover_type: shutter or blind
- intermediate_position, intermediate_position_up and intermediate_position_down
- tilt_intermediate or tilt_blind, with tilt_time_blind
- sun_protection_position

Back up the YAML and database before changing an existing installation. Follow the migration steps above when moving from YAML to the UI.

## Counter database and radio safety

The database stores the rolling sender counters used in radio packets. Losing it or restoring an older copy after commands have been sent can desynchronize the sender from paired receivers. Keep a current backup and let only one controller own the USB stick and database at a time.

Pairing and cover actions transmit radio commands. The integration cannot confirm radio reception. Verify important changes at the physical cover before relying on its Home Assistant estimate.

## After a Home Assistant host reboot

A cold host reboot can leave the USB stick visible while its radio connection no longer works. If the integration displays **Becker USB stick: check required after host reboot**, follow its instruction: reconnect the USB stick and verify one cover physically before relying on normal operation. A Home Assistant Core restart alone does not normally require this check.

## Troubleshooting

1. Check **Settings → Devices & services → Becker** to see whether the hub and cover entities are available.
2. Verify that the selected serial device is present and that the existing database path points inside the Home Assistant configuration directory.
3. To inspect Becker details, enable debug logging temporarily:

```yaml
logger:
  logs:
    custom_components.becker: debug
```

4. Review `home-assistant.log` for Becker messages. Use `becker.log_units` to inspect the sender units recognized by the database.
5. If a host reboot occurred and the USB stick is unresponsive, reconnect the stick and check one cover physically before issuing other commands.

For help, open an issue in the [tkarle project repository](https://github.com/tkarle/hass-becker-component-plus-pybecker/issues).

## Credits

Based on the work of [ole1986](https://github.com/ole1986) and [Nicolas Berthel](https://github.com/nicolasberthel).
