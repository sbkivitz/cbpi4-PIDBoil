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

try:
    # Same reasoning as the clock import: only forks carry this, and a plugin
    # that cannot load is worse than one without dry-fire protection.
    from cbpi.api.dryfire import DryFireWatch
except ImportError:  # pragma: no cover - depends on the host installation
    DryFireWatch = None


@parameters([Property.Number(label = "P", configurable = True, description="P Value of PID"),
             Property.Number(label = "I", configurable = True, description="I Value of PID"),
             Property.Number(label = "D", configurable = True, description="D Value of PID"),
             Property.Select(label="SampleTime", options=[2,5], description="PID Sample time in seconds. Default: 5 (How often is the output calculation done)"),
             Property.Number(label = "Max_Output", configurable = True, description="Power before Boil threshold is reached."),
             Property.Number(label = "Boil_Threshold", configurable = True, description="When this temperature is reached, power will be set to Max Boil Output (default: 98 °C/208 F)"),
             Property.Number(label = "Max_Boil_Output", configurable = True, default_value = 85, description="Power when Boil Threshold is reached."),
             Property.Number(label = "Boil_Plateau_Minutes", configurable = True, default_value = 3, description="Also treat a stalled temperature at full power as boiling, after this many minutes. 0 disables."),
             Property.Number(label = "Volume_Litres", configurable = True, default_value = 0, description="Litres in the vessel. With Element_Watts, enables dry-fire protection. 0 disables."),
             Property.Number(label = "Element_Watts", configurable = True, default_value = 0, description="Element rating in watts. With Volume_Litres, enables dry-fire protection. 0 disables.")])

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

    # PID defaults were tuned in Celsius. They are output-percent-per-degree
    # gains, so a Fahrenheit server needs smaller numeric defaults because the
    # same physical error is 1.8 times as many degrees.
    DEFAULT_P_C = 117.0795
    DEFAULT_I_C = 0.2747
    DEFAULT_D_C = 41.58
    DEFAULT_GAIN_TOLERANCE = 0.000001

    # How little a vessel must rise to count as having stopped, in degrees.
    # A 5.5kW element in 40 litres climbs about 2 degrees a minute, so anything
    # this small over several minutes is not a heat-up.
    PLATEAU_MIN_RISE = 0.5

    # How close to full the demand must be for a plateau to mean anything. Below
    # this the controller is easing off on purpose and a flat temperature is it
    # working, not the wort boiling.
    PLATEAU_MIN_DEMAND = 0.9

    # How far the temperature must fall below the plateau before boiling is
    # considered over - enough that noise cannot rattle it in and out.
    PLATEAU_EXIT_DROP = 2.0

    def _degree_ratio(self):
        return 1.0 if self.TEMP_UNIT == "C" else 1.8

    def _pid_gain(self, label, celsius_default, degree_ratio):
        configured = self.props.get(label)
        if configured is None or configured == "":
            return celsius_default / degree_ratio, True
        return float(configured), False

    def _has_legacy_celsius_default_gains(self):
        if self.TEMP_UNIT == "C":
            return False
        try:
            configured = {
                "P": float(self.props.get("P")),
                "I": float(self.props.get("I")),
                "D": float(self.props.get("D")),
            }
        except (TypeError, ValueError):
            return False
        return (
            abs(configured["P"] - self.DEFAULT_P_C) <= self.DEFAULT_GAIN_TOLERANCE
            and abs(configured["I"] - self.DEFAULT_I_C) <= self.DEFAULT_GAIN_TOLERANCE
            and abs(configured["D"] - self.DEFAULT_D_C) <= self.DEFAULT_GAIN_TOLERANCE
        )

    def _detect_boil_by_plateau(self, value, demand, maxout, window):
        """Is the wort boiling, judged by behaviour rather than by a number?

        Boil_Threshold is an absolute temperature, and absolute temperatures are
        the least reliable thing on a brew rig. A probe reading two degrees low,
        a brewer at altitude whose wort boils at 94 C, or simply a threshold
        typed in above the real boiling point, all produce the same outcome: the
        step never dials back and the kettle runs at full power into a rolling
        boil. That is how a boilover happens.

        Boiling has a signature that does not depend on calibration. At the
        boiling point energy stops raising temperature - it goes into vapour
        instead - so a vessel that has stopped climbing while the controller is
        still asking for everything it has is boiling, whatever the number says.

        Getting this wrong the other way is harmless: a dead element also stops
        rising, and deciding "boiling" then simply drops demand on an element
        that is not heating anyway.
        """
        if window <= 0:
            return False
        if maxout <= 0 or demand < maxout * self.PLATEAU_MIN_DEMAND:
            # Not asking for everything, so a flat temperature says nothing.
            self._plateau_since = None
            self._plateau_anchor = None
            return False

        now = clock.now()
        if self._plateau_anchor is None or value > self._plateau_anchor + self.PLATEAU_MIN_RISE:
            self._plateau_anchor = value
            self._plateau_since = now
            return False
        if value < self._plateau_anchor:
            self._plateau_anchor = value
            self._plateau_since = now
            return False
        return (now - self._plateau_since) >= window

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
            logging.warning(
                "PIDBoil: ignoring sensor %s, reading is missing or non-numeric",
                sensor_id,
            )
            return None

        age = state.get("age")
        # The sensor's own cadence, not a fixed number of seconds. A OneWire
        # probe on its default 60s interval is legitimately 59s old, and
        # cutting heat on that cycles the element every minute of a brew day
        # with nothing wrong. See SensorController.expected_max_age().
        limit = state.get("max_age") or self.MAX_SENSOR_AGE
        if age is not None and age > limit:
            logging.warning(
                "PIDBoil: ignoring sensor %s, last updated %.0fs ago (limit %.0fs)",
                sensor_id,
                age,
                limit,
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

            degree_ratio = self._degree_ratio()
            p, p_defaulted = self._pid_gain("P", self.DEFAULT_P_C, degree_ratio)
            i, i_defaulted = self._pid_gain("I", self.DEFAULT_I_C, degree_ratio)
            d, d_defaulted = self._pid_gain("D", self.DEFAULT_D_C, degree_ratio)
            legacy_default_gains = self._has_legacy_celsius_default_gains()
            if legacy_default_gains:
                p = self.DEFAULT_P_C / degree_ratio
                i = self.DEFAULT_I_C / degree_ratio
                d = self.DEFAULT_D_C / degree_ratio
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
            logging.info(
                "PIDBoil effective gains for %s: P=%.6g%s I=%.6g%s D=%.6g%s",
                self.TEMP_UNIT, p,
                " legacy-C-default converted" if legacy_default_gains
                else " default" if p_defaulted else " configured",
                i,
                " legacy-C-default converted" if legacy_default_gains
                else " default" if i_defaulted else " configured",
                d,
                " legacy-C-default converted" if legacy_default_gains
                else " default" if d_defaulted else " configured",
            )
            if legacy_default_gains:
                message = (
                    "PIDBoil found persisted Celsius default gains on a Fahrenheit "
                    "server and is running converted effective gains: "
                    "P={:.4f}, I={:.4f}, D={:.4f}. The saved props were not "
                    "rewritten; edit P/I/D if these are not intended."
                ).format(p, i, d)
                logging.warning(message)
                self.cbpi.notify("PIDBoil gain units", message, NotificationType.WARNING)

            sensor_failures = 0
            fault_notified = False
            in_boil = False
            boiling_by_plateau = False
            boil_latched = False
            boil_entry_temp = None
            plateau_window = max(0.0, float(
                self.props.get("Boil_Plateau_Minutes", 3) or 0
            )) * 60.0
            self._plateau_since = None
            self._plateau_anchor = None

            # Dry-fire protection. Needs two facts no kettle carries - how much
            # liquid is in it and how big the element is - so it does nothing at
            # all until both are configured.
            dry_watch = DryFireWatch() if DryFireWatch else None
            dry_litres = max(0.0, float(self.props.get("Volume_Litres", 0) or 0))
            dry_watts = max(0.0, float(self.props.get("Element_Watts", 0) or 0))
            # How far below the boil it must fall before fixed-power mode is
            # released. In degrees of the configured unit, so the band means the
            # same amount of physics either way.
            exit_drop = self.PLATEAU_EXIT_DROP * degree_ratio

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

                # Rising faster than the configured liquid allows means there is
                # no liquid. Judged against the power actually being delivered -
                # the previous demand, which is what produced the rise now being
                # measured. Zero demand passes zero watts, which switches the
                # guard off rather than on: a vessel warming with its element
                # idle is being heated by something else and is not this loop's
                # business.
                #
                # Acted on rather than merely reported. This is the one fault
                # where continuing to control is worse than stopping.
                delivered_watts = dry_watts * float(heat_percent_old or 0) / 100.0
                if dry_watch is not None and dry_watch.note(
                    current_temp, delivered_watts, dry_litres, degree_ratio
                ):
                    await self.actor_off(self.heater)
                    heater_is_on = False
                    heat_percent_old = 0
                    self.cbpi.notify(
                        "Dry fire",
                        dry_watch.describe(
                            getattr(self.kettle, "name", "Kettle"), dry_litres
                        ),
                        NotificationType.ERROR,
                    )
                    self.running = False
                    break

                if current_temp >= maxtempboil or boiling_by_plateau or boil_latched:
                    # Boiling: hold a fixed power rather than a temperature.
                    heat_percent = maxboilout
                    if not boil_latched:
                        boil_latched = True
                        boil_entry_temp = current_temp
                        if boiling_by_plateau and current_temp < maxtempboil:
                            self.cbpi.notify(
                                "{}".format(getattr(self.kettle, "name", "Kettle")),
                                "Boiling at {:.1f}, below the {:.1f} threshold - "
                                "holding {}% anyway. Check the Boil_Threshold and "
                                "the probe calibration.".format(
                                    current_temp, maxtempboil, maxboilout
                                ),
                                NotificationType.WARNING,
                            )
                    in_boil = True
                    # Latched on purpose, and detection is NOT re-run here.
                    #
                    # Fixed-power mode commands Max_Boil_Output, which is below
                    # the near-full demand the plateau detector requires. Asking
                    # it again at that reduced demand cleared the state, dropped
                    # the loop back to full power, and produced a saw-tooth
                    # between 100% and 85% - worse than never having noticed the
                    # boil. It leaves only when the wort has genuinely come off
                    # the boil, measured from where it started boiling.
                    if current_temp < boil_entry_temp - exit_drop:
                        boil_latched = False
                        boiling_by_plateau = False
                        self._plateau_since = None
                        self._plateau_anchor = None
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
                    # Judged on what was just asked for: if the controller wants
                    # everything it has and the wort has stopped responding, it
                    # is boiling regardless of what the threshold says.
                    boiling_by_plateau = self._detect_boil_by_plateau(
                        current_temp, heat_percent, maxout, plateau_window
                    )

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
