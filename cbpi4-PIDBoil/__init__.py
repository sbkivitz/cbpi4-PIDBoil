"""PID control for a boil kettle, with a fixed-power mode once boiling starts.

Below the boil threshold the PID holds a target temperature. Above it, holding a
temperature is meaningless - boiling wort stays at its boiling point and the only
thing that changes is how hard it boils - so the element switches to a fixed
percentage the brewer chooses.
"""

import asyncio
import logging
import time

from cbpi.api import *
from cbpi.api.dataclasses import NotificationType

try:
    # Present on forks with a pluggable clock, which lets a whole brew day be
    # rehearsed faster than real time. Optional on purpose: this plugin has to
    # keep working on an unmodified CraftBeerPi, where an unconditional import
    # would be an ImportError during plugin discovery rather than a missing
    # feature.
    from cbpi.api import clock
except ImportError:  # pragma: no cover - depends on the host installation
    class _RealClock:
        @staticmethod
        def now():
            return time.time()

        @staticmethod
        def monotonic():
            return time.monotonic()

        @staticmethod
        async def sleep(seconds):
            await asyncio.sleep(max(0.0, seconds))

        @staticmethod
        async def sleep_until(deadline):
            await asyncio.sleep(max(0.0, deadline - time.time()))

    clock = _RealClock()


@parameters([Property.Number(label = "P", configurable = True, description="P Value of PID"),
             Property.Number(label = "I", configurable = True, description="I Value of PID"),
             Property.Number(label = "D", configurable = True, description="D Value of PID"),
             Property.Select(label="SampleTime", options=[2,5], description="PID Sample time in seconds. Default: 5 (How often is the output calculation done)"),
             Property.Number(label = "Max_Output", configurable = True, description="Power before Boil threshold is reached."),
             Property.Number(label = "Boil_Threshold", configurable = True, description="When this temperature is reached, power will be set to Max Boil Output (default: 98 °C/208 F)"),
             Property.Number(label = "Max_Boil_Output", configurable = True, default_value = 85, description="Power when Boil Threshold is reached.")])

class PIDBoil(CBPiKettleLogic):

    # Consecutive unreadable samples tolerated before the brewer is told. At one
    # sample every few seconds this rides out a brief 1-wire glitch without
    # nagging about it.
    MAX_SENSOR_FAILURES = 3

    # A reading older than this is treated as no reading at all. Several sensor
    # implementations - the bundled OneWire one among them - catch a read error
    # and keep publishing their last value, so an unplugged probe reports a
    # plausible temperature forever. Driving a 5kW element in a full kettle
    # against a number that stopped changing is how a boilover starts.
    MAX_SENSOR_AGE = 30

    # Default power once boiling. Matches the default_value declared on the
    # property above, which the code used to contradict by defaulting to 100 -
    # so a brewer who left the field alone saw 85 in the interface and got a
    # full-power boil.
    DEFAULT_BOIL_OUTPUT = 85

    def _read_temp(self, sensor_id):
        """Current temperature, or None if it cannot be trusted.

        get_sensor_value() returns None for a missing or failing sensor, so the
        bare `.get("value")` raised AttributeError - which the broad handler in
        run() turned into one log line and a dead control loop for the rest of
        the brew.
        """
        try:
            state = self.get_sensor_value(sensor_id)
            value = float(state.get("value"))
        except (AttributeError, TypeError, ValueError):
            return None

        age = state.get("age")
        if age is not None and age > self.MAX_SENSOR_AGE:
            logging.warning(
                "PIDBoil: ignoring sensor %s, last updated %.0fs ago", sensor_id, age
            )
            return None

        return value

    async def on_stop(self):
        # Guarded: run() may have raised before self.heater was ever assigned,
        # and an AttributeError here would mask whatever actually went wrong.
        heater = getattr(self, "heater", None)
        if heater is not None:
            await self.actor_off(heater)

    async def run(self):
        try:
            self.TEMP_UNIT = self.get_config_value("TEMP_UNIT", "C")
            sampleTime = int(self.props.get("SampleTime",5))
            boilthreshold = 98 if self.TEMP_UNIT == "C" else 208

            p = float(self.props.get("P", 117.0795))
            i = float(self.props.get("I", 0.2747))
            d = float(self.props.get("D", 41.58))
            maxout = int(self.props.get("Max_Output", 100))
            maxtempboil = float(self.props.get("Boil_Threshold", boilthreshold))
            maxboilout = int(self.props.get("Max_Boil_Output", self.DEFAULT_BOIL_OUTPUT))
            self.kettle = self.get_kettle(self.id)
            self.heater = self.kettle.heater
            self.heater_actor = self.cbpi.actor.find_by_id(self.heater)

            # Deliberately not actor_on(heater, maxout) here.
            #
            # That energized the element at full power before a single
            # temperature had been read, so a kettle started with a failed probe
            # - or one already at boiling - went to full power immediately and
            # stayed there until the first sample came back. A plain GPIOActor's
            # on() also ignores the power argument and simply drives the pin
            # high, so "on at maxout" was really "on at 100%".
            #
            # Switching off instead establishes a known state without the pulse,
            # and matters because actor state survives a restart: an element left
            # on by a previous run would otherwise stay on while this loop
            # believed it was off.
            await self.actor_off(self.heater)
            heater_is_on = False
            heat_percent_old = None

            pid = PIDArduino(
                sampleTime, p, i, d, 0, maxout, getTimeMs=lambda: clock.now() * 1000
            )

            sensor_failures = 0
            fault_notified = False
            in_boil = False

            while self.running == True:
                current_temp = self._read_temp(self.kettle.sensor)
                target_temp = self.get_kettle_target_temp(self.id)
                try:
                    target_temp = float(target_temp)
                except (TypeError, ValueError):
                    target_temp = None

                if current_temp is None or target_temp is None:
                    # Cannot know the temperature, so do not heat - but stay
                    # alive. Returning or raising here ended temperature control
                    # for the rest of the brew after a single bad read, with
                    # nothing said to the brewer.
                    sensor_failures += 1
                    if heater_is_on:
                        await self.actor_off(self.heater)
                        heater_is_on = False
                    heat_percent_old = None
                    if sensor_failures >= self.MAX_SENSOR_FAILURES and not fault_notified:
                        fault_notified = True
                        self.cbpi.notify(
                            "{} sensor".format(getattr(self.kettle, "name", "Kettle")),
                            "No temperature reading - heating suspended until it returns",
                            NotificationType.ERROR,
                        )
                    await clock.sleep(sampleTime)
                    continue

                if fault_notified:
                    self.cbpi.notify(
                        "{} sensor".format(getattr(self.kettle, "name", "Kettle")),
                        "Temperature reading restored - heating resumed",
                        NotificationType.INFO,
                    )
                sensor_failures = 0
                fault_notified = False

                if current_temp >= maxtempboil:
                    # Boiling: hold a fixed power rather than a temperature.
                    heat_percent = maxboilout
                    in_boil = True
                else:
                    if in_boil:
                        # Coming back out of fixed-power mode after the PID has
                        # been idle. Without this its last remembered input is
                        # from before the boil, and the derivative term sees the
                        # whole gap as one sample's worth of change - a kick that
                        # slams the output to a rail on the first calculation.
                        pid.resync(current_temp)
                        in_boil = False
                    heat_percent = pid.calc(current_temp, target_temp)

                # Drive the actor's on/off state, not just its power level.
                # set_power() only forwards a number to the instance; it never
                # changes state, so a demand of 0% left a plain GPIOActor
                # nominally on at 0% duty rather than genuinely off.
                if heat_percent > 0:
                    if not heater_is_on:
                        await self.actor_on(self.heater, heat_percent)
                        heater_is_on = True
                        heat_percent_old = heat_percent
                    elif heat_percent != heat_percent_old:
                        await self.actor_set_power(self.heater, heat_percent)
                        heat_percent_old = heat_percent
                else:
                    if heater_is_on:
                        await self.actor_off(self.heater)
                        heater_is_on = False
                    heat_percent_old = 0

                await clock.sleep(sampleTime)

        except asyncio.CancelledError as e:
            pass
        except Exception as e:
            # Named for the plugin it is actually in. This said
            # "BM_PIDSmartBoilWithPump", copied from another plugin, which sends
            # anyone reading the log to the wrong file.
            logging.exception("PIDBoil error: %s", e)
        finally:
            self.running = False
            heater = getattr(self, "heater", None)
            if heater is not None:
                await self.actor_off(heater)

# Based on Arduino PID Library
# See https://github.com/br3ttb/Arduino-PID-Library
class PIDArduino(object):

    def __init__(self, sampleTimeSec, kp, ki, kd, outputMin=float('-inf'),
                 outputMax=float('inf'), getTimeMs=None):
        if kp is None:
            raise ValueError('kp must be specified')
        if ki is None:
            raise ValueError('ki must be specified')
        if kd is None:
            raise ValueError('kd must be specified')
        if float(sampleTimeSec) <= float(0):
            raise ValueError('sampleTimeSec must be greater than 0')
        if outputMin >= outputMax:
            raise ValueError('outputMin must be less than outputMax')

        self._logger = logging.getLogger(type(self).__name__)
        self._Kp = kp
        self._Ki = ki * sampleTimeSec
        self._Kd = kd / sampleTimeSec
        self._sampleTime = sampleTimeSec * 1000
        self._outputMin = outputMin
        self._outputMax = outputMax
        self._iTerm = 0
        # None, not 0. Starting at zero means the first calculation sees the
        # entire current temperature as one sample's worth of change - at a Kd
        # of 41.58 and a kettle at 20 C that is a derivative term of several
        # hundred percent, which pins the output to a rail on the very first
        # calculation. The first reading seeds this instead.
        self._lastInput = None
        self._lastOutput = 0
        self._lastCalc = 0

        if getTimeMs is None:
            self._getTimeMs = self._currentTimeMs
        else:
            self._getTimeMs = getTimeMs

    def resync(self, inputValue):
        """Forget the last input, so the next call does not see a false jump.

        For use when the controller has been bypassed for a while - the boil
        threshold path does exactly that - and the temperature has moved without
        the PID watching.
        """
        self._lastInput = None

    def calc(self, inputValue, setpoint):
        now = self._getTimeMs()

        if (now - self._lastCalc) < self._sampleTime:
            return self._lastOutput

        # Compute all the working error variables
        error = setpoint - inputValue
        if self._lastInput is None:
            # First calculation: no previous reading, so no rate of change is
            # knowable yet. Treating it as zero is the honest answer.
            dInput = 0.0
        else:
            dInput = inputValue - self._lastInput

        # Anti-windup: stop integrating only when the integral is pushing
        # further into the rail the output is already against.
        #
        # Freezing whenever the output was saturated - regardless of direction -
        # meant a controller sitting at 0% could not begin recovering until the
        # proportional term alone lifted it off the rail. On a kettle heating
        # from cold the output is saturated for most of the ramp, so in practice
        # the integral never contributed at all.
        at_ceiling = self._lastOutput >= self._outputMax
        at_floor = self._lastOutput <= self._outputMin
        winding_up = (at_ceiling and error > 0) or (at_floor and error < 0)
        if not winding_up:
            self._iTerm += self._Ki * error
            self._iTerm = min(self._iTerm, self._outputMax)
            self._iTerm = max(self._iTerm, self._outputMin)

        p = self._Kp * error
        i = self._iTerm
        d = -(self._Kd * dInput)

        # Compute PID Output
        self._lastOutput = p + i + d
        self._lastOutput = min(self._lastOutput, self._outputMax)
        self._lastOutput = max(self._lastOutput, self._outputMin)

        # Log some debug info
        self._logger.debug('P: {0}'.format(p))
        self._logger.debug('I: {0}'.format(i))
        self._logger.debug('D: {0}'.format(d))
        self._logger.debug('output: {0}'.format(self._lastOutput))

        # Remember some variables for next time
        self._lastInput = inputValue
        self._lastCalc = now
        return self._lastOutput

    def _currentTimeMs(self):
        return clock.now() * 1000

def setup(cbpi):

    '''
    This method is called by the server during startup 
    Here you need to register your plugins at the server
    
    :param cbpi: the cbpi core 
    :return: 
    '''

    cbpi.plugin.register("PIDBoil", PIDBoil)
