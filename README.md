# Craftbeerpi4 Kettle Logic Plugin

## PID Logic with boil threshold.

If the target Temperature is above a configurable threshold the PID will be ignored and heater is switched on to the max output value. This is helpful if you use the same kettle for mashing and boiling.

Once the boil threshold temperature is reached, the boil will be done with the max boil output power


### Installation:

Please have a look at the [Craftbeerpi4 Documentation](https://openbrewing.gitbook.io/craftbeerpi4_support/readme/plugin-installation)

- Package name: cbpi4-PIDBoil
- Package link: https://github.com/PiBrewing/cbpi4-pidboil/archive/main.zip


### Parameters

![PIDBoil Settings](https://github.com/avollkopf/cbpi4-PIDBoil/blob/main/cbpi4-PIDBoil-logic.png?raw=true)

- P - proportional value (empty: 117.0795 per °C, 65.04 per °F)
- I - integral value (empty: 0.2747 per °C, 0.1526 per °F)
- D - derivative value (empty: 41.58 per °C, 23.1 per °F)
- SampleTime - 2 or 5 seconds -> how often the logic calculates the power setting (empty: 5). Anything from 1 to 60 is used as set; anything else is 5, and the brewer is told
- Max Output - the most power the PID uses before the boil threshold is reached (empty: 100%)
- Boil Threshold - Above this temperature the heater will be set to Max Boil Output Power (empty: 98°C / 208F)
- Max Boil Output - Power (%) that is used above Boil Threshold Temperature (empty: 85%)
- Boil Plateau Minutes - a temperature that stops rising at full power for this many minutes counts as boiling too; 0 turns it off (empty: 3)

The plugin tells CraftBeerPi what it uses for each setting left empty (`blank_defaults`), in the server's temperature unit, so the Hardware page can show it and offer to fill it in.

### Changelog

- 18.11.26: (0.0.9.post5, this fork) A SampleTime from 1 to 60 seconds is used as set; anything else is 5, and the brewer is told, as BoilPower and PIDHerms do. Before, any whole number was used: at 3600 the element stayed as the first decision left it for an hour, through a failed probe. Text such as "2.5" or "inf" stopped the logic starting.
- 16.11.26: (0.0.9.post4, this fork) The plugin tells CraftBeerPi what it uses for each setting left empty (`blank_defaults`), for CraftBeerPi 4.7.7.post41 or later to show and fill in. The empty values are the ones it always used, now read from one table by the plugin and by CraftBeerPi alike. A setting that is only spaces, or null, counts as empty too; it used to stop the logic starting.
- 13.11.26: (0.0.9.post3, this fork) Comments describe the code. No change in behaviour.
- 29.10.26: (0.0.9.post2, this fork) A temperature reading - or a target - that is not a finite number is treated as no reading: the element off, the brewer told, heating back when a real reading returns. Before, +inf chose the fixed boil power (85% by default) for as long as it lasted, and one NaN stuck in the PID's integral, so the element never heated again that brew.
- 29.12.22: (0.0.9) Fixed Bug related to Boil Threshold Parameter
- 10.05.22: (0.0.8) Updated README (removed cbpi add)
- 10.05.22: (0.0.7) Removed cbpi dependency
- 01.12.21: (0.0.6) If current kettle power is different from kettle logic. logic will overrule kettle power setting
- 21.11.21: (0.0.5) Adapted to cbpi4 4.0.0.45 to accomodate actor power settings incl. the PWM Actor.
- 13.10.21: (0.0.4) Added Power setting for Boil
- 13.10.21: (0.0.3) Improvement of actor toggling in case of 0% or 100% heating
- 12.10.21: (0.0.2) Bug fixing MashStep Automode Issue
- 19.08.21: (0.0.1) Initial commit
