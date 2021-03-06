import datetime
from dateutil.parser import parse
from operator import add
import math

from models import Unit


class Schedule(object):
    def __init__(self, entries):
        """

        :param entries:
        :type entries: list(dict)
        :return:
        :rtype:
        """
        self.entries = entries

    def at(self, time):
        """

        :param time:
        :type time: datetime.time
        :return:
        :rtype: dict
        """
        result = {}

        for entry in self.entries:
            if parse(entry['start']).time() > time:
                break
            result = entry

        return result


def floor_datetime_at_minute_interval(timestamp, minute):
    return timestamp - datetime.timedelta(
        minutes=timestamp.minute % minute,
        seconds=timestamp.second,
        microseconds=timestamp.microsecond
    )


def ceil_datetime_at_minute_interval(timestamp, minute):
    """
    From http://stackoverflow.com/questions/13071384/python-ceil-a-datetime-to-next-quarter-of-an-hour

    :param timestamp:
    :type timestamp: datetime.datetime
    :param minute:
    :type minute: int
    :return:
    :rtype: datetime.datetime
    """
    # how many secs have passed this hour
    nsecs = timestamp.minute * 60 + timestamp.second + timestamp.microsecond * 1e-6

    # number of seconds to next minute mark
    seconds = minute * 60
    delta = (nsecs // seconds) * seconds + seconds - nsecs

    if delta < seconds:
        return timestamp + datetime.timedelta(seconds=delta)
    else:
        return timestamp


def glucose_data_tuple(glucose_entry):
    return (
        parse(glucose_entry.get('date') or glucose_entry['display_time']),
        glucose_entry.get('sgv') or glucose_entry.get('amount') or glucose_entry['glucose']
    )


def carb_effect_curve(t, absorption_time):
    """Returns the fraction of total carbohydrate effect with a given absorption time on blood
    glucose at the specified number of minutes after eating.

    This is the integral of Carbs on Board (COB), defined by a Scheiner GI curve from Think Link a
    Pancreas, fig 7-8. This is based on an algorithm that first appeared in GlucoDyn

    See: https://github.com/kenstack/GlucoDyn

    :param t: The time in t since the carbs were eaten
    :type t: float
    :param absorption_time: The total absorption time of the carbohydrates
    :type absorption_time: int
    :return: A percentage of the initial carb intake, from 0 to 1
    :rtype: float
    """

    if t <= 0:
        return 0.0
    elif t <= absorption_time / 2.0:
        return 2.0 / (absorption_time ** 2) * (t ** 2)
    elif t < absorption_time:
        return -1.0 + 4.0 / absorption_time * (t - t ** 2 / (2.0 * absorption_time))
    else:
        return 1.0


def walsh_iob_curve(t, insulin_action_duration):
    """Returns the fraction of a single insulin dosage remaining at the specified number of minutes
    after delivery; also known as Insulin On Board (IOB).

    This is a Walsh IOB curve, and is based on an algorithm that first appeared in GlucoDyn

    See: https://github.com/kenstack/GlucoDyn

    :param t: time in minutes since the dose began
    :type t: float
    :param insulin_action_duration: The duration of insulin action (DIA) of the patient, in minutes
    :type insulin_action_duration: int
    :return: The fraction of a insulin dosage remaining at the specified time
    :rtype: float
    """
    assert insulin_action_duration in (3 * 60, 4 * 60, 5 * 60, 6 * 60)
    iob = 0

    if t >= insulin_action_duration:
        iob = 0.0
    elif t <= 0:
        iob = 1.0
    elif insulin_action_duration == 3 * 60:
        iob = -3.2030e-9 * (t**4) + 1.354e-6 * (t**3) - 1.759e-4 * (t**2) + 9.255e-4 * t + 0.99951
    elif insulin_action_duration == 4 * 60:
        iob = -3.310e-10 * (t**4) + 2.530e-7 * (t**3) - 5.510e-5 * (t**2) - 9.086e-4 * t + 0.99950
    elif insulin_action_duration == 5 * 60:
        iob = -2.950e-10 * (t**4) + 2.320e-7 * (t**3) - 5.550e-5 * (t**2) + 4.490e-4 * t + 0.99300
    elif insulin_action_duration == 6 * 60:
        iob = -1.493e-10 * (t**4) + 1.413e-7 * (t**3) - 4.095e-5 * (t**2) + 6.365e-4 * t + 0.99700

    return iob


def integrate_iob(t0, t1, insulin_action_duration, t):
    """Integrates IOB using Simpson's rule for spread-out (basal-like) doses

    TODO: Clean this up and use scipy.integrate.simps

    :param t0: The start time in minutes of the dose
    :type t0: float
    :param t1: The end time in minutes of the dose
    :type t1: float
    :param insulin_action_duration: The duration of insulin action (DIA) of the patient, in minutes
    :type insulin_action_duration: int
    :param t: The current time in minutes
    :type t: float
    :return:
    :rtype: float
    """
    nn = 50  # nn needs to be even

    # initialize with first and last terms of simpson series
    dx = (t1 - t0) / nn
    integral = walsh_iob_curve(t - t0, insulin_action_duration) + walsh_iob_curve(t - t1, insulin_action_duration)

    for i in range(1, nn - 1, 2):
        integral += 4 * walsh_iob_curve(
            t - (t0 + i * dx), insulin_action_duration
        ) + 2 * walsh_iob_curve(
            t - (t0 + (i + 1) * dx), insulin_action_duration
        )

    return integral * dx / 3.0


def sum_iob(t0, t1, insulin_action_duration, t, dt):
    """Sums the percent IOB activity at a given time for a temp basal dose

    :param t0: The start time in minutes of the dose
    :type t0: int
    :param t1: The end time in minutes of the dose
    :type t1: int
    :param insulin_action_duration: The duration of insulin action (DIA) of the patient, in minutes
    :type insulin_action_duration: int
    :param t: The current time in minutes
    :type t: float
    :param dt: The segment size over which to sum
    :return: The sum of IOB at time t, in percent
    """
    return reduce(
        add,
        [walsh_iob_curve(t - i, insulin_action_duration) for i in range(t0, t1 + dt, dt) if t - i >= 0],
        0
    )


def cumulative_bolus_effect_at_time(event, t, insulin_sensitivity, insulin_action_duration):
    """

    :param event: The bolus history event, describing a value in Units of insulin
    :type event: dict
    :param t: The time in minutes from the beginning of the dose
    :type t: int
    :param insulin_sensitivity: The insulin sensitivity at time t, in mg/dL/U
    :type insulin_sensitivity: float
    :param insulin_action_duration: The duration of insulin action at time t, in hours
    :type insulin_action_duration: int
    :return: The cumulative effect of the bolus on blood glucose at time t, in mg/dL
    :rtype: float
    """
    return -event['amount'] * insulin_sensitivity * (1 - walsh_iob_curve(t, insulin_action_duration * 60.0))


def carb_effect_at_datetime(event, t, insulin_sensitivity, carb_ratio, absorption_rate):
    return insulin_sensitivity / carb_ratio * event['amount'] * carb_effect_curve(t, absorption_rate)


def cumulative_temp_basal_effect_at_time(event, t, t0, t1, insulin_sensitivity, insulin_action_duration):
    int_iob = integrate_iob(t0, t1, insulin_action_duration * 60, t)

    return event['amount'] / 60.0 * -insulin_sensitivity * ((t1 - t0) - int_iob)


def calculate_iob(
    normalized_history,
    insulin_action_curve,
    dt=5,
    absorption_delay=10,
    basal_dosing_end=None
):
    """Calculates insulin on board degradation according to Walsh's algorithm, from the latest history entry until 0

    :param normalized_history: History data in reverse-chronological order, normalized by openapscontrib.mmhistorytools
    :type normalized_history: list(dict)
    :param insulin_action_curve: Duration of insulin action for the patient in hours
    :type insulin_action_curve: int
    :param dt: The time differential for calculation and return value spacing in minutes
    :type dt: int
    :param absorption_delay: The delay time before a dose begins absorption in minutes
    :type absorption_delay: int
    :param basal_dosing_end: A datetime at which continuing doses should be assumed to be cancelled
    :type basal_dosing_end: datetime.datetime
    :return: A list of IOB values and their timestamps
    :rtype: list(dict)
    """
    if len(normalized_history) == 0:
        return []

    first_history_event = sorted(normalized_history, key=lambda e: e['start_at'])[0]
    last_history_event = sorted(normalized_history, key=lambda e: e['end_at'])[-1]
    last_history_datetime = ceil_datetime_at_minute_interval(parse(last_history_event['end_at']), dt)
    simulation_start = floor_datetime_at_minute_interval(parse(first_history_event['start_at']), dt)
    simulation_end = last_history_datetime + datetime.timedelta(minutes=(insulin_action_curve * 60 + absorption_delay))

    insulin_duration_minutes = insulin_action_curve * 60.0

    # For each incremental minute from the simulation start time, calculate the effect values
    simulation_minutes = range(0, int(math.ceil((simulation_end - simulation_start).total_seconds() / 60.0)) + dt, dt)
    simulation_timestamps = [simulation_start + datetime.timedelta(minutes=m) for m in simulation_minutes]
    simulation_count = len(simulation_minutes)

    iob = [0.0] * simulation_count

    for history_event in normalized_history:
        start_at = parse(history_event['start_at'])
        end_at = parse(history_event['end_at'])

        for i, timestamp in enumerate(simulation_timestamps):
            t = (timestamp - start_at).total_seconds() / 60.0 - absorption_delay

            if t < 0 - absorption_delay:
                continue
            elif history_event['unit'] == Unit.units:
                effect = history_event['amount'] * walsh_iob_curve(t, insulin_duration_minutes)
            elif history_event['unit'] == Unit.units_per_hour:
                if history_event['type'] == 'TempBasal' and basal_dosing_end and end_at > basal_dosing_end:
                    end_at = basal_dosing_end

                t0 = 0
                t1 = int(math.ceil((end_at - start_at).total_seconds() / 60.0))

                effect = history_event['amount'] / (60.0 / dt) * sum_iob(t0, t1, insulin_duration_minutes, t, dt)
            else:
                continue

            iob[i] += effect

    return [{
        'date': timestamp.isoformat(),
        'amount': iob[i],
        'unit': Unit.units
    } for i, timestamp in enumerate(simulation_timestamps)]


def future_glucose(
    normalized_history,
    recent_glucose,
    insulin_action_curve,
    insulin_sensitivity_schedule,
    carb_ratio_schedule,
    dt=5,
    sensor_delay=10,
    basal_dosing_end=None
):
    """

    :param normalized_history: History data in reverse-chronological order, normalized by openapscontrib.mmhistorytools
    :type normalized_history: list(dict)
    :param recent_glucose: Historical glucose, cleaned by openapscontrib.glucosetools
    :type recent_glucose: list(dict)
    :param insulin_action_curve: Duration of insulin action for the patient
    :type insulin_action_curve: int
    :param insulin_sensitivity_schedule: Daily schedule of insulin sensitivity in mg/dL/U
    :type insulin_sensitivity_schedule: Schedule
    :param carb_ratio_schedule: Daily schedule of carb sensitivity in g/U
    :type carb_ratio_schedule: Schedule
    :param dt: The time differential for calculation and return value spacing in minutes
    :type dt: int
    :param sensor_delay: The delay to expect between input effects and sensor glucose readings
    :type sensor_delay: int
    :param basal_dosing_end: A datetime at which continuing doses should be assumed to be cancelled
    :type basal_dosing_end: datetime.datetime
    :return: A list of predicted glucose values
    :rtype: list(dict)
    """
    if len(recent_glucose) == 0:
        return []

    last_glucose_datetime, last_glucose_value = glucose_data_tuple(recent_glucose[0])

    # Determine our simulation time.
    simulation_start = last_glucose_datetime
    simulation_end = last_glucose_datetime

    if len(normalized_history) > 0:
        last_history_event = sorted(normalized_history, key=lambda e: e['end_at'])[-1]
        last_history_datetime = parse(last_history_event['end_at'])
        simulation_end = max(simulation_end, last_history_datetime)

    simulation_end += datetime.timedelta(minutes=(insulin_action_curve * 60 + sensor_delay))

    # For each incremental minute from the simulation start time, calculate the effect values
    simulation_minutes = range(0, int(math.ceil((simulation_end - simulation_start).total_seconds() / 60.0)) + dt, dt)
    simulation_timestamps = [simulation_start + datetime.timedelta(minutes=m) for m in simulation_minutes]
    simulation_count = len(simulation_minutes)

    carb_effect = [0.0] * simulation_count
    insulin_effect = [0.0] * simulation_count

    for history_event in normalized_history:
        initial_effect = 0
        start_at = parse(history_event['start_at'])
        end_at = parse(history_event['end_at'])

        insulin_end_datetime = end_at + datetime.timedelta(hours=insulin_action_curve)
        absorption_rate = 180
        absorption_end_datetime = end_at + datetime.timedelta(minutes=absorption_rate)

        for i, timestamp in enumerate(simulation_timestamps):
            t = (timestamp - start_at).total_seconds() / 60.0 - sensor_delay

            # Cap the time used to determine the sensitivity so it doesn't fluctuate
            # after completion
            sensitivity_time = min(insulin_end_datetime, timestamp)
            insulin_sensitivity = insulin_sensitivity_schedule.at(sensitivity_time.time())['sensitivity']

            if history_event['unit'] == Unit.grams:
                # Cap the time used to determine the carb ratio to absorption end so it doesn't
                # fluctuate after completion
                ratio_time = min(absorption_end_datetime, timestamp)
                carb_ratio = carb_ratio_schedule.at(ratio_time.time())['ratio']

                effect = carb_effect_at_datetime(history_event, t, insulin_sensitivity, carb_ratio, absorption_rate)
                apply_to = carb_effect
            elif history_event['unit'] == Unit.units:
                effect = cumulative_bolus_effect_at_time(history_event, t, insulin_sensitivity, insulin_action_curve)
                apply_to = insulin_effect
            elif history_event['unit'] == Unit.units_per_hour:
                if history_event['type'] == 'TempBasal' and basal_dosing_end and end_at > basal_dosing_end:
                    end_at = basal_dosing_end

                t1 = (end_at - start_at).total_seconds() / 60.0

                effect = cumulative_temp_basal_effect_at_time(
                    history_event,
                    t,
                    0,
                    t1,
                    insulin_sensitivity,
                    insulin_action_curve
                )
                apply_to = insulin_effect
            elif history_event['unit'] == Unit.event:
                # effect added through use of exercise marker (JournalEntryExerciseMarker) in x23 models
                break
            else:
                raise ValueError('Unknown event %s', history_event)

            if i == 0:
                initial_effect = effect

            effect -= initial_effect
            apply_to[i] += effect

    return [{
        'date': timestamp.isoformat(),
        'glucose': last_glucose_value + carb_effect[i] + insulin_effect[i]
    } for i, timestamp in enumerate(simulation_timestamps)]
