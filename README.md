# Becker cover support for Home Assistant

> [!WARNING]
> Version `0.4.1-beta.6` is a development build for Home Assistant 2026.8.
> Keep a backup of `centronic-stick.db` and ensure only one controller (the
> Raspberry Pi MQTT bridge or Home Assistant) can access the Becker sender
> counters at a time.

A native Home Assistant component to control Becker RF shutters with a Becker Centronic USB stick.
It works with the Becker ***Centronic USB Stick*** with the Becker order number ***4035 200 041 0*** and ***4035 000 041 0***.
It works for the Becker ***Centronic*** roller shutters, blinds and sun protection as well as for Roto roof windows with RF remotes.  
It is based on the work of [ole](https://github.com/ole1986) and [Nicolas Berthel](https://github.com/nicolasberthel).

The Becker integration currently supports the following cover operations:
- Open
- Close
- Stop
- Open tilt
- Close tilt
- Set cover position
- Move to a configured sun-protection position

There are three ways to track position of the cover:
- Add the travel time to configuration
- Provide a value template e.g. to use sensors to set the current position
- Track the cover commands from Becker remotes

# Installation

1. Add [this](https://github.com/tkarle/hass-becker-component-plus-pybecker) repository to HACS custom
   repositories (preferred).  
   Alternatively copy `custom_components/becker` from this repository into the
   `custom_components` folder of your HA configuration directory.
2. Plug the Becker USB stick into any free USB port. It is a good practice to add a short USB extension cable
   and place the Becker USB stick away from other RF sources.  
3. Add the Becker integration under **Settings → Devices & services**. Existing
   YAML installations can be imported without re-entering their covers.
4. Restart Home Assistant when the migration dialog asks you to do so.

# Configuration
## UI configuration and safe YAML migration

The UI setup always requires an existing `centronic-stick.db`. It validates the
database read-only and never creates, replaces or resets rolling counters. The
database must be inside the Home Assistant configuration folder. Only sender
units already paired in that database can be selected.

When an active Becker YAML platform is detected, **Add hub** offers to copy its
USB path, database path and all cover definitions. The resulting UI hub remains
passive while YAML is loaded, so the serial port and database cannot be opened
twice. Complete the handover in this order:

1. Back up `centronic-stick.db`.
2. Add the Becker hub in the UI and confirm the detected YAML import.
3. Remove the legacy Becker YAML platform and restart Home Assistant.
4. Open the imported Becker hub, choose **Configure**, and activate it.

Existing entity IDs are retained through their Becker channel unique IDs. Do
not run a Raspberry Pi MQTT bridge or another copy of pybecker at the same time.

Each UI-configured cover is registered as its own Home Assistant device in the
Becker USB hub entry; the hub is not duplicated as a seventh device. Open the
hub's **Configure** dialog and choose **Configure a
cover** to edit its name, travel times, optional position template, physical
remote IDs, an optional sun-protection position and tilt behavior. Providing at
least one travel time enables position tracking and the position slider; a
value template can additionally correct the tracked position.

Select a native cover type for every device:

- **Roller shutter** uses Home Assistant's shutter device class and never
  exposes slat controls or the programmed intermediate-position fields.
- **Venetian blind with slats** uses Home Assistant's blind device class and
  opens a separate slat configuration step. There you can enable the
  programmed intermediate/ventilation positions (with their UP/DOWN
  percentages) and choose between intermediate-position control or short
  UP/DOWN tilt pulses.

The programmed intermediate-position toggle and its UP/DOWN percentages only
ever take effect for venetian blinds, since roller shutters force
tilt/intermediate handling off regardless; that is why those fields only
appear in the blind's slat step. Existing entries keep their legacy behavior
until a type is explicitly saved. Changing a type never changes the RF
channel, entity unique ID, or rolling counter database.

For SWC545 venetian-blind remotes, short UP/DOWN presses are tracked as slat
movements without starting a full-position timer. The three-second hold stage
confirms vertical travel. Because the receiver can enter maintained travel
before that stage is received, an unreleased press is promoted to vertical
tracking after one second. Double presses use the configured intermediate or
turning position. A newly received physical-remote command also supersedes any
obsolete timed stop left by an earlier Home Assistant command.

For observed SWC545 blind behavior, received double-UP (`2C`) does not change
the vertical position. Received double-DOWN (`4C`) is tracked as fully closed;
the receiver then turns the slats horizontally. These physical-remote actions
are intentionally separate from Home Assistant's configurable intermediate
position commands.

Older master remotes for venetian blinds (e.g. SWC445) send the legacy
double-tap packets (`24`/`44`) for a slat-only pulse instead. For a blind
where neither **Tilt intermediate** nor **Tilt blind** is enabled, receiving
one of these packets no longer jumps the tracked vertical position to the
configured intermediate percentage; the vertical position is left unchanged,
matching the fact that only the slats actually move.

If another controller moves a cover without a packet being received, use the
`becker.set_known_position` action to correct the estimated position without
sending another radio command. Select the Becker cover and enter the physically
observed value from 0 (closed) to 100 (open).

## Sun protection

Every cover has an optional **Sun-protection position** field (percent, `0`
closed to `100` open). Use it together with the `becker.move_to_sun_protection`
action to drive a cover to a fixed shading position with one call:

- For a **roller shutter**, the action moves to the configured percentage. A
  shutter without a configured value raises an error instead of sending an
  unsuitable radio command, since a shutter has no other native sun-protection
  behavior to fall back to.
- For a **venetian blind**, the action uses the configured percentage if set;
  otherwise it falls back to sending the receiver's own programmed `DOWN2`
  command (lower the blind and turn the slats horizontal).

The `becker.move_down_intermediate` action sends that same `DOWN2` command
directly to any Becker cover, independent of the sun-protection position. If
the cover has a programmed DOWN intermediate position configured, this action
also updates the tracked Home Assistant position to that value. This is the
native replacement for the old approach of triggering `cover.close_cover_tilt`
in tilt mode "intermediate" to reach a learned sun-protection stop.

```yaml
service: becker.move_to_sun_protection
target:
  entity_id: cover.living_room
```

```yaml
service: becker.move_down_intermediate
target:
  entity_id: cover.living_room
```

The YAML format below remains available for compatibility and advanced options.

## Basic configuration
```yaml
cover:
  - platform: becker
    covers:
      # Use unique names for each cover like kitchen, bedroom or living_room
      kitchen:
        friendly_name: "Kitchen Cover"
        # Becker Centronic USB stick provides up to five units (1-5) with up to seven (1-7) channels
        # Unit 1 - Channel 1
        channel: "1"
      bedroom:
        friendly_name: "Bedroom Cover"
        # Unit 1 - Channel 2
        channel: "2"
      living_room:
        friendly_name: "Living room Cover"
        # Use Unit 2 - Channel 1
        channel: "2:1"
```
Note: The channel needs to be a string!

## Platform configuration
If you use the Becker integration with the ***Home Assistant Operating System***, 
the default device path for the USB Stick should work. The default path is:  
`/dev/serial/by-id/usb-BECKER-ANTRIEBE_GmbH_CDC_RS232_v125_Centronic-if00`  
If you run Home Assistant within a Virtual Machine or a Docker container, it 
might be useful use a different device path.  
Note: The serial port is opened using 
the [pySerial serial_for_url handler](https://pyserial.readthedocs.io/en/latest/url_handlers.html).
Therefore connections e.g.  over a TCP/IP socket are supported as well.

The Becker integration uses a database file `centronic-stick.db` located in the 
Home Assistant configuration folder to store an incremental number for each unit.
You can change the filename if needed. The rolling counters cannot be reconstructed
safely if this file is lost. Back it up before migration and never restore an older
copy after newer commands have been transmitted.
```yaml
cover:
  - platform: becker
    device: "/dev/my-becker-centronic-usb"
    filename: "my-centronic-stick.db"
```

## Position by Travel Time
There is no feedback from the covers available! In order to track the position of
the cover, it is recommended to add the travel time for each cover. Determine the
movement time in ***seconds*** for each cover from closed to open position. To
improve the precision, add the movement time from open to closed position as well.  
This will also enable the ability to set the cover position from Home Assistant user 
interface or through the service `cover.set_cover_position`.
```yaml
cover:
  - platform: becker
    covers:
      living_room:
        friendly_name: "Living room Cover"
        channel: "2:1"
        # The travel time for direction up is sufficient if travel time for up and down are equal
        travelling_time_up: 30
        # Optional travel time for direction down
        travelling_time_down: 26.5
```

## Position by value template
In some cases it might be useful to add a value template to determine the position
of your cover. For example for a roof window with rain sensor. In case of rain, 
the roof window will close, but you cannot determine this without an additional 
sensor.  
Every time the template generates a new result, the position of the cover is overwritten 
by the result of the template.  
The following results are valid:
- any number between `0` and `100` where `0` is `closed` and `100` is `open`
- logic values where
  - `'closed'`, `'false'`, `False` are `closed`
  - `'open'`, `'true'`, `True` are `open`
- unknown values `'unknown'`, `'unavailable'`, `'none'`, `None`  
The unknown values are useful to set the position only to confirm one specific 
position, like closed in the example below. For any other values the position 
will not changed. This allows to use the value template in conjunction with the position 
by travel time.
```yaml
cover:
  - platform: becker
    covers:
      roof_window:
        friendly_name: "Roof window"
        channel: "3:1"
        travelling_time_up: 15
        # Set position to closed (0) if sensor.roof_window is closed, otherwise keep value
        value_template: "{{ 0 if is_state('sensor.roof_window', 'closed') else None }}"
```

## Position tracking for cover commands from Becker remotes
Usually there is at least one remote, the master remote, used to control the cover.
The remote communicates directly with the cover. It is possible to receive and track 
all remote commands within home assistant. Therefore the position of the cover
is updated whenever a remote command is received.  
In order to determine the remote ID, it es necessary to enable debug log messages
(see troubleshooting). The debug message will look as follows:  
`... DEBUG ... \[custom_components.becker.pybecker.becker_helper]` Received packet: 
unit_id: `12345`, channel: `2`, command: HALT, argument: 0, packet: ...
```yaml
cover:
  - platform: becker
    covers:
      living_room:
        friendly_name: "Living room Cover"
        channel: "2:1"
        travelling_time_up: 30
        travelling_time_down: 26.5
        # The remote ID consists of the unit_id and the channel separated by a colon
        # Multiple ID's separated by comma are possible
        remote_id: "12345:2"
```

## Intermediate cover position
Becker covers supports two intermediate positions. One when opening the cover 
and one when closing the cover. Please see the manual of your cover to see how
to program these intermediate positions in your cover.  
Your cover will travel to the corresponding intermediate position if your double
tab the UP or DOWN button on your remote.  
The default intermediate positions in the Becker integration are `25` for UP 
direction and `75` for DOWN direction, where `0` is `closed` and `100` is `open`.
This behavior is imitated by the Becker integration in Home Assistant. To imitate
the cover movement properly in Home Assistant it is required to set the positions properly.  
You can calculate the `intermediate_position_up`. You need to measure the runtime from 
closed position to the intermediate position in direction UP (double tap UP 
on your remote). Divide the measured time by the `travelling_time_up` and 
multiply the result by `100`.  
You can do the same for the `intermediate_position_up`. Measure the runtime from 
closed position to the intermediate position in direction DOWN (double tap DOWN on 
your remote). Divide the measured time by the `travelling_time_up` and multiply 
the result by `100`.
```yaml
  - platform: becker
    covers:
      kitchen:
        friendly_name: "Kitchen Cover"
        channel: "1"
        intermediate_position_up: 70
        intermediate_position_down: 40
```
If you have not programmed any intermediate positions in your cover, you should 
disable the intermediate cover position.
```yaml
  - platform: becker
    covers:
      kitchen:
        friendly_name: "Kitchen Cover"
        channel: "1"
        intermediate_position: off
```

## Tilt intermediate
The Becker integration provide the ability to control the intermediate position from 
Home Assistant user interface. Therefore the tilt functionality of Home Assistant is used 
to issue the commands to drive to intermediate positions.
If you don't want to control the intermediate positions from Home Assistant, you can 
disable the tilt functionality for each cover.  
This will also disable the service `cover.close_cover_tilt` and `cover.open_cover_tilt`.
```yaml
  - platform: becker
    covers:
      kitchen:
        friendly_name: "Kitchen Cover"
        channel: "1"
        tilt_intermediate: off
```
Note: You still need to set the intermediate cover position appropriately!

## Tilt blind
The Becker integration provides support for blinds. The Becker blinds allow to control
their tilt position by short press of the UP or DOWN button on their master remote. 
A long press of the UP or DOWN button fully open or closes the blinds.  
To control the tilt position of your blind and for proper tracking of your blind position, 
you need to enable `tilt_blind`. This changes the tilt functionality of Home Assistant 
from intermediate position to tilt blind. The default tilt time is 0.3 seconds. 
This time can be adapted to your needs.
```yaml
  - platform: becker
    covers:
      Living_room_blind:
        friendly_name: "Living room blind"
        channel: "2:2"
        tilt_blind: on
        tilt_time_blind: 0.5
```

# Pairing the Becker USB Stick with your covers
To use your cover in HA you need to pair it first with the Becker USB stick. The
pairing is always between your remote and the shutter. The shutter will react on 
the commands of all paired remotes.  
Usually you already have programmed your original remote as the master remote. It 
is not recommended to program the USB stick as the master remote! The USB stick is 
like an additional remote. Therefore the pairing procedure for the USB stick is 
the same as with additional remotes. Please refer to you manual for more details.  

You have to put your shutter in pairing mode before. This is done by pressing the 
program button of your master remote until you hear a "clac" noise

To pair your shutter run the action `becker.pair` once (see HA Developer Tools -> Actions).
The action sends the proven legacy programming sequence `PAIR2`, `RELEASE`,
`PAIR2`, with a short delay between its three telegrams. All three rolling
counters are reserved durably before transmission. TRAIN is reserved for
pairing and is never used for normal travel commands.

The shutter first acknowledges programming mode once and then confirms a
successful pair with a double "clac" or nod. Do not test travel commands unless
that final double acknowledgement occurred. If it is missing, put the intended
receiver into programming mode again before retrying the action.

Example data for service becker.pair:

```yaml
service: becker.pair
data:
  # Example data to pair your cover with USB stick unit 1 - channel 1
  channel: 1
  unit: 1
```

# Events for Remote Commands
In addition to processing remote commands to update cover states, the
integration also fires explicit events of type
`becker_remote_packet_received` for each command it receives from a remote.
Those events can be used to trigger automations when remote buttons are pressed
or for other custom purposes.

Each event contains the remote unit, channel, broad command, exact action,
argument nibble and complete command byte. The broad `command` remains
backward compatible: both `20` and the intermediate/double-tap variant `24`
report `command: up`. Use `action` or `command_code` when an automation must
distinguish those variants.

```yaml
event_type: becker_remote_packet_received
data:
  unit: "12345"
  channel: "1"
  command: "up"
  action: "up_intermediate"
  argument: "4"
  command_code: "24"
```

The corresponding down variants are `40` (`down`) and `44`
(`down_intermediate`). Unknown command bytes are still emitted with their raw
`argument` and `command_code`, so they can be diagnosed without changing the
integration first.

# Units and Channels
The USB stick acts like a remote control
The remote control protocol is able to access up to 7 devices like shutters, these are addressed as "channels" (1-7). There's also a broadcast channel (15) which addresses all of the devices at the same time. With this you are able to send a "UP" or "DOWN" command to all the covers at the same time.

Units are used for devices like shutters. Also, remote controls get an own unit id.
The software is able to "emulate" up to 5 remote controls, named "units".
For these units 1-5, the stick is configured internally to use unit numbers 1737b, 1737c, 1737d, 1737e and 1737f.
You can also see these unit id's in the database.
If you leave out the unit number, the unit number 1 will be used, so the USB stick uses 1737b as a unit id.
I think that's all the magic behind the Becker protocol.

# Troubleshooting
If you have any trouble follow these steps:
- Restart Home Assistant after you have plugged in the USB stick
- Enable debug log for becker.  
Add the following lines to your configuration.yaml to enable debug log:

```yaml
logger:
  default: info
  logs:
    # This must correspond to the folder name of your /config/custom_components/becker folder
    custom_components.becker: debug
```

You can also change the log configuration dynamically by calling the `logger.set_level` service. 
This method allows you to enable debug logging only for a limited time:

```yaml
service: logger.set_level
data:
  custom_components.becker: debug
```

All messages are logged to the home-assistant.log file in your config folder.  
It is also helpful to find out the Remote ID of your Becker Remote. The message 
will be something like below every time you press a key on your Remote:  
`... DEBUG ... \[custom_components.becker.pybecker.becker_helper]` Received packet: 
unit_id: `12345`, channel: `2`, command: HALT, argument: 0, packet: ...

In case of any errors related to the Becker integration try to fix them.  
If you require additional help have a look at the 
[Home Assistant Community](https://community.home-assistant.io). There is already one thread about the 
Becker integration: 
[Integrating Becker Motors](https://community.home-assistant.io/t/integrating-becker-motors-in-to-hassio/151705)
Another way is to open a new issue on 
[GitHub](https://github.com/RainerStaude/hass-becker-component-plus-pybecker/issues).

To disable debug log for becker set the level back from `debug` to `info`.
