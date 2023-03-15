#!/usr/bin/env python3

__version__ = "1.2.0"
__author__ = "Yclept Nemo"
__title__ = "DMVision"


import wave
import random
import tempfile
import math
import struct
import itertools
import functools
import time
import datetime
import json
import re
import dataclasses
import collections
import collections.abc
import inspect
import os
import os.path
import sys
import types
import typing
import enum
import contextlib
import threading
import queue
import asyncio
import concurrent.futures

import bs4
import numpy
import scipy

import vosk
import ffmpeg

import pyaudio
import aiohttp_basicauth

import requests
import requests.exceptions

import aiohttp.web
import twilio.rest
import dateutil.parser
import selenium.webdriver

from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys as WebDriverKeys
from selenium.webdriver.support.ui import Select
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import TimeoutException
from selenium.common.exceptions import WebDriverException
from selenium.common.exceptions import StaleElementReferenceException

from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse


class AppointmentStatus(enum.Enum):
    # Some error happened and the appointment status is undefined
    Undefined = enum.auto()
    Cancelled = enum.auto()
    Scheduled = enum.auto()


@dataclasses.dataclass
class AppointmentInfo:
    first_name: str
    last_name: str
    email: str
    phone: str
    birth_date: str
    driver_license: str

    # Determines if an appointment should be scheduled
    check_calendar: typing.Callable[..., bool]

    # Required only for the road test and so optional
    permit_validation: str = ""


@dataclasses.dataclass
class TwilioInfo:
    account_sid: str
    auth_token: str
    from_: str
    to: str

    def __post_init__(self):
        self.client = twilio.rest.Client(self.account_sid, self.auth_token)


@dataclasses.dataclass
class SeleniumInfo:
    autofill: typing.Callable[..., typing.Optional[bool]]
    geckodriver_path: typing.Optional[str] = None
    _driver: typing.Optional[selenium.webdriver.Firefox] =\
        dataclasses.field(init=False, default=None)

    @property
    def driver(self):
        if self._driver is None:
            self._driver = self.start_driver()
        return self._driver

    def start_driver(self):
        args = {}
        if self.geckodriver_path:
            args["executable_path"] = self.geckodriver_path
        return selenium.webdriver.Firefox(**args)


@dataclasses.dataclass
class VoskInfo:
    model_path: str
    log_level: int = -1

    # Loading the model takes time, so do it immediately rather than lazily.
    def __post_init__(self):
        vosk.SetLogLevel(self.log_level)
        self.model = vosk.Model(self.model_path)


@dataclasses.dataclass
class OtherInfo:
    # Time between loops when checking appointments, accounting for loop
    # duration.
    timeout_standard: int = 20

    # Time between loops when valid appointments are available.
    timeout_scheduling: int = 10

    # Minimum time between loops. If a loop exceeds its time allotment, the
    # next loop will still be delayed by this timeout.
    timeout_min: int = 5


@dataclasses.dataclass
class ThreadInfo:
    appt: typing.Optional[tuple[int, datetime.datetime, str]]

    # Move a cancelled appointment to the blocklist so it is not immediately
    # re-scheduled.
    appt_blocklist: set[tuple[int, datetime.datetime]]

    appt_history: collections.deque[tuple[int, datetime.datetime, str]]
    sched_avail: bool
    sched_tries: int
    id_state: dict[int, tuple
        [ typing.Optional[bool]
        , typing.Optional[int]
        , typing.Optional[datetime.datetime]
        ]]

    # For locking everything above
    appt_lock: asyncio.Lock

    # The event loop will be closed and synchronization primitives will no
    # longer work
    event_shutdown_program: asyncio.Event

    # All components should shut down but the event loop will keep running.
    event_shutdown_asyncio: asyncio.Event

    loop: asyncio.AbstractEventLoop
    queue: queue.Queue

    # The main thread does more locking but must synchronize with the async
    # thread, a possibly expensive operation. Think of the appt_lock as a
    # coarse lock and the print_lock as a finer lock. The main thread must
    # only ever hold one. The async thread may need to hold both.
    print_lock: asyncio.Lock


@dataclasses.dataclass
class ServerInfo:
    username: str
    password: str
    debug: bool = False


class NotificationResource(enum.Enum):
    Terminal = enum.auto()
    Sound = enum.auto()
    TextMessage = enum.auto()


class NotificationCategory(enum.Enum):
    Appointment = enum.auto()
    Scheduling = enum.auto()
    Cancelling = enum.auto()
    Logging = enum.auto()


class NotificationPriority(enum.Enum):
    Standard = enum.auto()
    Important = enum.auto()


class ResultStatus:
    def __init__(self, result):
        self.status = type(self)
        self.result = result


# Collection of all possible notification statuses. Not to be instantiated.
class NotificationStatus:
    class Successful(ResultStatus):
        pass

    @classmethod
    def register_name(cls, name, conflict=True):
        if conflict and hasattr(cls, name):
            raise ValueError(f"Name '{name}' already taken")
        status = type(name, (ResultStatus,), {})
        setattr(cls, name, status)
        return status

    @classmethod
    def register_class(cls, target_cls):
        status = cls.register_name(target_cls.__name__)
        target_cls.status = status
        return target_cls


# A notification handler (and notification status) that drops all
# notifications between a certain time. The decorator assigns the
# generated notification status class to the notification handler
# class.
@NotificationStatus.register_class
class Timelock:
    def __init__(self, start, stop, locked=None, notifier=None):
        self.start = start
        self.stop = stop
        self.locked = self.is_locked() if locked is None else locked
        self.notifier = notifier

    def is_locked(self):
        now = datetime.datetime.now().time()
        return self.start <= now <= self.stop

    def __call__(self, handler, request):
        locked = self.is_locked()
        if self.locked != locked:
            self.locked = locked
            if self.notifier:
                self.notifier(self, request)
        if locked:
            return self.status(self)
        return handler(request)

# A notification handler (and notification status) implementing the leaky
# bucket algorithm for rate-limiting. Drop any notifications that exceed
# the capacity.
@NotificationStatus.register_class
class Ratelimit:
    def __init__(self, capacity, rate, notifier=None):
        self.capacity = capacity
        self.rate = rate
        self.current = capacity
        self.prev = time.monotonic()
        self.notifier = notifier

    def __call__(self, handler, request):
        now = time.monotonic()
        self.current = min\
            ( self.current + (now-self.prev)*self.rate
            , self.capacity
            )
        self.prev = now
        if self.notifier:
            self.notifier(self, request)
        if self.current < 1:
            return self.status(self)
        self.current -= 1
        return handler(request)


# Manages the mapping between notification types and notification handlers
class NotificationManager:
    def __init__(self):
        self.handlers = {}

    def lookup(self, resource, category, priority):
        return self.handlers.get((resource, category, priority), [])

    def register(self, resource, category, priority, handlers):
        key = (resource, category, priority)
        self.handlers[key] = handlers
        return self

    def register_many(self, resources, categories, priorities, handlers):
        keys = (resources, categories, priorities)
        for key in itertools.product(keys):
            self.register(*key, handlers)
        return self

    @classmethod
    def register_notifier(cls, resource, conflict=True):
        def decorator(f):
            def lookup_handlers(self, category, priority, static=True):
                def wrapper(*args, **kwargs):
                    def apply_handlers(request):
                        return next(c)(apply_handlers, request)
                    request = NotificationRequest\
                        ( resource, category, priority
                        , self, args, kwargs
                        )
                    c = self.lookup(*request.info())
                    c = iter(c + [wrapped])
                    return apply_handlers(request)
                def wrapped(handler, request):
                    r = f(*request.args_pos, **request.args_key)
                    return NotificationStatus.Successful(r)
                # A small optimization: if there are no handlers and there
                # are not expected to be any handlers in the future, then
                # bypass the lookup chain and call the notifier directly
                c = self.lookup(resource, category, priority)
                if static and not c:
                    return f
                return wrapper
                # For example:
                # c == [timelock, ratelimit, someother, f]
                # handler(request) => timelock(handler, request)
                #     handler(request) => ratelimit(handler, request)
                #         handler(request) => someother(handler, request)
                #             handler(request) => wrapped(handler, request)
                #                 f(*request.args_pos, **request.args_key)
            name = f.__name__
            if conflict and hasattr(cls, name):
                raise ValueError(f"Name '{name}' already taken")
            setattr(cls, name, lookup_handlers)
            return lookup_handlers
        return decorator


@dataclasses.dataclass
class NotificationRequest:
    resource: NotificationResource
    category: NotificationCategory
    priority: NotificationPriority
    manager: NotificationManager
    args_pos: tuple[typing.Any]
    args_key: dict[str, typing.Any]

    def info(self):
        return (self.resource, self.category, self.priority)


@dataclasses.dataclass
class AliasRegistryEntry:
    entry: typing.Any
    name: typing.Hashable
    aliases: set
    # Use this to determine if any aliases have been subsequently overridden.
    aliases_original: set = dataclasses.field(init=False)

    def __post_init__(self):
        self.aliases_original = self.aliases.copy()


# A mapping of names and aliases to entries. Names take precedence and cannot
# be masked by aliases. Entries are wrapped using AliasRegistryEntry ensuring
# all data is available on lookup. Duplicate aliases may either raise an
# error, or be reassigned to the new entry in both the AliasRegistry and the
# corresponding AliasRegistryEntry.
class AliasRegistry(collections.abc.MutableMapping):
    def __init__(self):
        self.by_name = {}
        self.by_alias = {}

    def remove_name(self, name):
        data_out = self.by_name.pop(name)
        for alias in data_out.aliases:
            self.by_alias.pop(alias)
        return data_out

    def remove_alias(self, alias):
        data_out = self.by_alias.pop(alias)
        data_out.aliases.remove(alias)
        return data_out

    def add_name\
        ( self, entry, name, aliases=()
        , conflict_name=True, conflict_alias=False
        ):
        if name in self.by_name:
            if conflict_name:
                raise KeyError(f"name '{name}' already exists")
            self.remove_name(name)
        data_in = AliasRegistryEntry(entry, name, set(aliases))
        # Resolve conflicts before committing:
        self.add_aliases_prep(data_in.aliases, conflict_alias=conflict_alias)
        for alias in data_in.aliases:
            self.by_alias[alias] = data_in
        self.by_name[name] = data_in

    def add_aliases(self, name, aliases, conflict_alias=False):
        data_in = self[name]
        aliases = data_in.aliases - aliases
        self.add_aliases_prep(aliases, conflict_alias=conflict_alias)
        for alias in aliases:
            self.by_alias[alias] = data_in
        data_in.aliases_original += aliases

    def add_aliases_prep(self, aliases, conflict_alias=False):
        for alias in aliases:
            try:
                data_out = self.by_alias[alias]
            except KeyError:
                continue
            if conflict_alias:
                msg = f"alias '{alias}' already exists for '{data_out[1]}'"
                raise KeyError(msg)
            self.remove_alias(alias)

    def __getitem__(self, key):
        try:
            return self.by_name[key]
        except KeyError:
            pass
        try:
            return self.by_alias[key]
        except KeyError:
            pass
        raise KeyError(key)

    def __delitem__(self, key):
        try:
            return self.remove_name(key)
        except KeyError:
            pass
        try:
            return self.remove_alias(key)
        except KeyError:
            pass
        raise KeyError(key)

    def __setitem__(self, key, value):
        if isinstance(key, slice):
            if key.start is None:
                raise ValueError("name is required")
            name = key.start
            aliases = () if key.stop is None else key.stop
        else:
            name = key
            aliases = ()
        return self.add_name\
            ( value, name, aliases
            , conflict_name=False
            , conflict_alias=False
            )

    def __iter__(self):
        yield from self.by_name
        for i in self.by_alias:
            if i in self.by_name:
                continue
            yield i

    def __len__(self):
        return len(set(self.by_name) | set(self.by_alias))


# Represent a sine tone in a timeline. Each note has a linear fade-in and
# fade-out. Times are intended to be absolute among other Notes. Given the
# start time of the note and duration of each feature (fade-in, note itself,
# fade-out), provide the start time of each.
class Note:
    def __init__(self, freq, volume, start_fade, fade_in, duration, fade_out):
        self.freq = freq
        self.volume = volume
        # start_fade, start, stop, stop_fade
        self.start_fade = start_fade
        self.fade_in = fade_in
        self.duration = duration
        self.fade_out = fade_out

    @property
    def start(self):
        return self.start_fade + self.fade_in

    @property
    def stop(self):
        return self.start + self.duration

    @property
    def stop_fade(self):
        return self.stop + self.fade_out

    @property
    def total_duration(self):
        return self.fade_in + self.duration + self.fade_out

    def sample(self, samplerate):
        c_ss = int(samplerate * self.total_duration)
        c_fi = int(samplerate * self.fade_in)
        c_fo = int(samplerate * self.fade_out)
        r_ss = range(c_ss)
        r_fi = range(0, c_fi)
        r_fo = range(c_ss - c_fo, c_ss)
        for t in r_ss:
            s = self.volume * math.sin(2 * math.pi * self.freq * t / samplerate)
            if t in r_fi:
                s *= t / len(r_fi)
            elif t in r_fo:
                s *= 1 - ((r_fo.index(t) + 1) / len(r_fo))
            yield s


# A separate thread which initializes sound output and then pulls groups
# of frequency sequences from the queue. Each sequence is converted to a
# note in a timeline, with possible crossfade, and played until complete
# or interrupted. The replace method interrupts playback and replaces the
# queue with a new group of frequencies.
class Sound(threading.Thread):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.queue = []

        # A wait condition which agglomerates several underlying conditions.
        self.condition = threading.Condition()

        # Events can probably be replaced with booleans

        # Interrupt playback to either quit or pull the next item from the
        # queue.
        self.interrupt = threading.Event()

        # Stops the thread.
        self.stop = threading.Event()

    # From an external thread
    def replace(self, ns):
        with self.condition:
            self.queue.clear()
            self.queue.append(ns)
            self.interrupt.set()
            self.condition.notify()

    # From an external thread
    def shutdown(self):
        with self.condition:
            self.stop.set()
            self.condition.notify()
        self.join()

    def run(self):
        pa = pyaudio.PyAudio()
        try:
            self.consume(pa)
        finally:
            pa.terminate()

    def consume(self, pa):
        stream = pa.open\
            ( format=pyaudio.paFloat32
            , channels=1
            , rate=48000
            , output=True
            )
        while not self.stop.is_set():
            with self.condition:
                while not (self.queue or self.stop.is_set()):
                    self.condition.wait()
                if self.stop.is_set():
                    break
                self.interrupt.clear()
                ns = self.queue.pop()
            ss = self.notes_samples(stream, ns, 1, 0.1)
            # Too large a group and the audio crackles. Related to threading?
            for b in grouper(ss, int(stream._rate/100)):
                if self.interrupt.is_set() or self.stop.is_set():
                    break
                stream.write(b"".join(b))
        stream.stop_stream()
        stream.close()

    @staticmethod
    def notes_samples(stream, ns, volume, xfade=0):
        # The stream properties are fixed by the class but it does not hurt to
        # check
        if stream._format != pyaudio.paFloat32 or stream._channels != 1:
            raise ValueError("incompatible stream")

        notes = collections.deque()
        rate = stream._rate

        # Convert from relative to absolute time. Result must be ordered.
        t = 0
        for n in ns:
            n = Note(n[0], volume, t, xfade, n[1], xfade)
            notes.append(n)
            t = n.stop

        # Mix independent notes and convert to bytes.
        playing = set()
        t = 0
        while notes or playing:
            while notes and (notes[0].start_fade * rate <= t):
                playing.add(notes.popleft().sample(rate))
            sample = 0
            remove = set()
            for source in playing:
                try:
                    sample += next(source)
                except StopIteration:
                    remove.add(source)
            playing -= remove
            yield struct.pack("=f", sample)
            t += 1


# Sleep (at least) until the given seconds since the marked time have passed,
# or since the current time without a mark. Unlike sleep, negative time is
# ignored and zero time does not sleep. By default the current time is marked
# during object initialization.
class MarkSleeper:
    def __init__(self, mark=True):
        if mark:
            self.mark()
        else:
            self.clear()

    def mark(self):
        self.since = time.monotonic()

    def clear(self):
        self.since = None

    def sleep(self, secs):
        since = self.since
        if since:
            secs = since + secs - time.monotonic()
        if secs > 0:
            time.sleep(secs)
        self.mark()
        return secs


class DecaptchaError(Exception):
    pass


class DecaptchaDOSError(DecaptchaError):
    """ recaptcha's service denial: "try again later"
    """


class DecaptchaFailError(DecaptchaError):
    """ decaptcha has exhausted all attempts and failed. An optional error.
    """


def decaptcha_get_iframe_entry(driver):
    return driver.find_element_by_css_selector(".g-recaptcha iframe")

def decaptcha_get_iframe_realm(driver):
    return driver.find_elements_by_css_selector\
        ("body > div > div > iframe[title*='recaptcha'i]")[-1]

def decaptcha_blocked(driver):
    # Determine if ReCAPTCHA is blocking further interaction.
    els = driver.find_elements_by_css_selector("[class*='doscaptcha'i]")
    return bool(els)

def decaptcha_v2_checked(e_check, timeout, frequency=None):
    # Determine if the element is checked (if the captcha passed).
    if not frequency:
        frequency = timeout / 10
    try:
        WebDriverWait(e_check.parent, timeout, frequency).until\
            (lambda d:
                "recaptcha-checkbox-checked" in
                e_check.get_attribute("class").split())
    except TimeoutException:
        return False
    else:
        return True

def decaptcha_v2i_passed(i_realm, from_visible, timeout, frequency=None):
    # Determine if the captcha has passed by checking if the realm iframe is
    # visible. To avoid race conditions the status is only updated in response
    # to changes, such as the element's visibility or validity of the DOM node.
    # A wait timeout implies the visibility remains unchanged.
    if not frequency:
        frequency = timeout / 10

    def on_display_change(driver):
        try:
            v = i_realm.is_displayed()
            c = v != from_visible
        except StaleElementReferenceException as e:
            raise TimeoutException(False) from e
        if c:
            raise TimeoutException(v)
        return False

    try:
        WebDriverWait(i_realm.parent, timeout, frequency).until\
            (on_display_change, message=from_visible)
    except TimeoutException as e:
        v = e.msg

    return not v

def decaptcha_entry(f):
    @functools.wraps(f)
    def wrapper(selenium_info, *args, timeout=1, attempts=5, error=False):
        driver = selenium_info.driver
        # It is not possible to save a reference to the current frame that
        # remains valid in other frames. Given the nested frames [a -> b -> c],
        # frame 'c' is only valid from frame 'b' and not from frames 'a' or
        # 'c'. It follows there can be no reference to the top-level frame,
        # which can only be focused via a selenium function. Decaptcha needs
        # access to the top-level frame and guarantees that on exit, that frame
        # will remain active.
        try:
            r = f(selenium_info, *args, timeout=timeout, attempts=attempts)
            if error and not r:
                raise DecaptchaFailError
            return r
        except WebDriverException as e:
            # Any exception caused by ReCAPTCHA blocking further interactions
            # will be raised when focus is switched to the popup frame. There
            # is no need to switch to any other frames.
            if decaptcha_blocked(driver):
                raise DecaptchaDOSError from e
            raise
        finally:
            # Unless an exception was raised the top-level frame should already
            # be focused. For simplicity and as a safeguard against future
            # modifications, switch to the top-level frame regardless.
            driver.switch_to.default_content()
    return wrapper

# Run this javascript to log the times needed to emulate human input:
#   cel = document.createElement("div");
#   document.body.appendChild(cel);
#   cel.id = "click-logger";
#   cel.style.position = "fixed";
#   cel.style.inset = "0px";
#   cel.style.height = "100%";
#   cel.style.width = "100%";
#   cel.style.zIndex = 10000000000;
#   cel.style.pointerEvents = "auto";
#   cel.addEventListener("click", (evt) => {
#       cel.style.pointerEvents = "none";
#       console.log(evt.timeStamp/1000);
#       setTimeout(() => {
#           cel.style.pointerEvents = "auto";
#       }, 1000*0.75);
#   });
#
# The double-click behavior is a hack to work around the cross-origin policy.
@decaptcha_entry
def decaptcha_v2(selenium_info, vosk_info, timeout, attempts):
    # Solve the ReCAPTCHA. This is the function responsible for program flow
    # and so manages the frame focus. All subroutines expect the correct frame
    # to be in focus before executing.
    driver = selenium_info.driver
    msleep = MarkSleeper()

    # Selenium by default waits for document.readyState to be "complete", ie
    # for the window.load event. The reCAPTCHA v2 widget, when automatically
    # rendered, will have created both its iframes before this so there is no
    # need for Selenium to wait further.
    #
    # I'm not sure if the iframes' documents are guaranteed to have loaded
    # before Selenium is ready and so use an explicit wait to play it safe.
    #
    # If an in-progress captcha times out neither iframe is reloaded, ensuring
    # that settings such as the audio/image toggle are preserved between runs.
    # If a successful captcha times out both iframes are reloaded, causing
    # settings such as the audio/image toggle to be forgotten.
    driver.switch_to.default_content()

    i_entry = decaptcha_get_iframe_entry(driver)
    i_realm = decaptcha_get_iframe_realm(driver)

    driver.switch_to.frame(i_entry)

    e_check = WebDriverWait(driver, timeout).until\
        (lambda d: d.find_element_by_id("recaptcha-anchor"))

    # Click the checkbox.
    msleep.mark()
    decaptcha_sample_click(e_check)

    # Check that the checkbox is clicked.
    if decaptcha_v2_checked(e_check, timeout):
        return True

    driver.switch_to.default_content()
    driver.switch_to.frame(i_realm)

    decaptcha_audio(driver, timeout, msleep)

    while True:
        # The elements here are recreated every loop and so must be found again
        # to avoid a 'StaleElementReferenceException' error.
        attempts -= 1

        decaptcha_solve(vosk_info, driver, timeout, msleep)

        driver.switch_to.default_content()
        driver.switch_to.frame(i_entry)

        # Check that the checkbox is clicked.
        if decaptcha_v2_checked(e_check, timeout):
            return True

        if attempts <= 0:
            return False

        driver.switch_to.default_content()
        driver.switch_to.frame(i_realm)

@decaptcha_entry
def decaptcha_v2i(selenium_info, vosk_info, i_realm, timeout, attempts):
    # See the documentation for 'decaptcha_v2'.
    driver = selenium_info.driver
    msleep = MarkSleeper()

    driver.switch_to.default_content()

    if decaptcha_v2i_passed(i_realm, False, timeout):
        return True

    driver.switch_to.frame(i_realm)

    # switch to audio
    decaptcha_audio(driver, timeout, msleep, sec_audio=0)

    while True:
        attempts -= 1

        # solve captcha
        decaptcha_solve(vosk_info, driver, timeout, msleep)

        driver.switch_to.default_content()

        if decaptcha_v2i_passed(i_realm, True, timeout):
            return True

        if attempts <= 0:
            return False

        driver.switch_to.frame(i_realm)

def decaptcha_audio(driver, timeout, mark_sleeper, sec_audio=0.875):
    # Switch to an audio captcha. The error message from a blocked captcha here
    # will obscure the audio button. Clicking the audio button will raise an
    # 'ElementClickInterceptedException' error.
    #
    # Possible states of the audio button:
    # Visible but covered by the blocked-captcha message:
    #   is_displayed -> True
    #   click -> ElementClickInterceptedException
    # Visible with image captcha active:
    #   is_displayed -> True
    #   click -> success
    # Invisible with audio captcha active:
    #   is_displayed -> False
    #   click -> ElementNotInteractableException
    #
    # We should never be in a state having the audio captcha as the default
    # interface.
    el = WebDriverWait(driver, timeout).until\
        (lambda d: d.find_element_by_id("recaptcha-audio-button"))
    mark_sleeper.sleep(sec_audio)
    decaptcha_sample_click(el)

def decaptcha_solve\
    ( vosk_info, driver, timeout, mark_sleeper
    , sec_play=0.75, sec_input=2.0
    ):
    # Mute the (only) audio element via synchronous javascript. A blocked
    # captcha does not load the audio element and so this will raise a
    # 'NoSuchElementException' error.
    el = WebDriverWait(driver, timeout).until\
        (lambda d: d.find_element_by_id("audio-source"))
    driver.execute_script("arguments[0].muted = true;", el)
    # Play the audio; only the click is synchronous. A blocked captcha here
    # will show an error message rather than the expected dialog and so the
    # click will raise a 'NoSuchElementException' error.
    el = WebDriverWait(driver, timeout).until\
        (lambda d: d.find_element_by_css_selector
            (".rc-audiochallenge-play-button > button"))
    mark_sleeper.sleep(sec_play)
    decaptcha_sample_click(el)

    # Get the audio download link.
    el = WebDriverWait(driver, timeout).until\
        (lambda d: d.find_element_by_class_name
            ("rc-audiochallenge-tdownload-link"))
    url = el.get_attribute("href")

    # Run speech recognition from a temporary directory.
    vosk_sr_m = lambda f: vosk_sr(f, vosk_info.model)
    with tempfile.TemporaryDirectory() as dirpath:
        txt = vosk_sr_m(vosk_convert(decaptcha_download(dirpath, url)))

    # Input the audio response text.
    el = WebDriverWait(driver, timeout).until\
        (lambda d: d.find_element_by_id("audio-response"))
    mark_sleeper.sleep(sec_input)
    decaptcha_sample_click(el)
    el.send_keys(txt + WebDriverKeys.ENTER)

def decaptcha_sample_click(el):
    # Click the element in a more natural fashion. Click duration is given by a
    # beta distribution with negative skew. Click position is given by
    # symmetric beta distribution - see 'decaptcha_sample_center'.
    s = decaptcha_sample_center(el)
    # based on data collected using 'automation.aid.html'
    d = random.betavariate(6, 14)*250 + 50
    ( ActionChains(el.parent)
    . move_to_element_with_offset(el, *s)
    . click_and_hold()
    . pause(d/1000)
    . release()
    . perform()
    )

def decaptcha_sample_center(el):
    # Sample a coordinate within the element's context box - excluding a small
    # margin - using a symmetric beta distribution. The margin is specified by
    # the element's font size and horizontal width, whichever is smaller.
    #
    # get element font size
    f = el.value_of_css_property("font-size")
    r = re.search(r"-?\d+(\.\d+)?", f)
    if r is None:
        raise ValueError(f"Cannot parse '{f}' as float")
    f = float(r.group())
    # element dimensions
    d = (el.size["width"], el.size["height"])
    # margins for the sample area
    m = tuple(min(f, x/10) for x in d)
    # sample area
    a = tuple(x - 2*y for x,y in zip(d,m))
    # sample a symmetric beta distribution
    s = tuple(random.betavariate(5,5) for _ in a)
    # scale and shift the sample into the area
    s = tuple(x*y+z for x,y,z in zip(s,a,m))
    return s

def decaptcha_download(dirpath, url):
    # Download a captcha to the given directory. The calling function is
    # responsible for cleanup.
    with requests.get(url, stream=True) as r:
        r.raise_for_status()
        filepath = os.path.join(dirpath, "audio.captcha.mp3")
        with open(filepath, "wb") as f:
            for c in r.iter_content(1024<<4):
                f.write(c)
    return filepath

def vosk_convert(filepath):
    # Convert the given file to a format suitable for vosk.
    filepath_out = os.path.splitext(filepath)[0] + ".wav"
    _ = ( ffmpeg.input(filepath)
        . output(filepath_out, acodec="pcm_s16le", ac=1, ar="16k")
        . overwrite_output()
        . run(quiet=True)
        )
    return filepath_out

def vosk_sr(file, model):
    # Run speech recognition using vosk.
    with wave.open(file) as w:
        assert ( w.getnchannels() == 1
             and w.getsampwidth() == 2
             and w.getcomptype() == "NONE"
             and w.getframerate() == 16000)
        r = vosk.KaldiRecognizer(model, w.getframerate())
        while True:
            d = w.readframes(8000)
            if not d:
                break
            r.AcceptWaveform(d)
        t = r.FinalResult()
        return json.loads(t)["text"]

# In case of a given set of exceptions, retry the decorated function with the
# given timeouts. If timeouts are exhausted, re-raise the exception. If the
# reset period is met, reset the timeouts from the beginning. Return the result
# of the decorated function.
#
# * The notifier parameters are not optional. Pass None to skip.
# * The remaining variable parameters are appended as arguments to the
#   notifiers
def retry_on_exception\
    ( timeouts, reset, exceptions
    , notifier_out, notifier_in
    , *n_args, **n_kwargs
    ):
    def retry_decorator(f):
        def retry_wrapper(*args, **kwargs):
            i = 0
            while True:
                try:
                    t1 = time.monotonic()
                    r = f(*args, **kwargs)
                except exceptions as e:
                    t2 = time.monotonic()
                    if t2-t1 >= reset:
                        i = 0
                    if i >= len(timeouts):
                        raise
                    if notifier_out is not None:
                        notifier_out(i, timeouts, e, f, *n_args, **n_kwargs)
                    time.sleep(timeouts[i])
                    if notifier_in is not None:
                        notifier_in(i, timeouts, e, f, *n_args, **n_kwargs)
                    i += 1
                else:
                    return r
        return retry_wrapper
    return retry_decorator

# Generate a window of radius 'r' around each element of 'i'. Extend
# the index to fill missing values via 'fill'. For example:
#
# >>> list(window([0,1,2], 1, fill=lambda i:i))
# [ [-1, 0, 1]
# , [0, 1, 2]
# , [1, 2, 3]
# ]
def window(i, r, fill=lambda i: None):
    def windowed(i, r, fill):
        yield from (fill(i) for i in range(-r,0))
        index = 0
        for v in i:
            yield v
            index += 1
        yield from (fill(i) for i in range(index,index+r))
    i = windowed(i, r, fill)
    m = r*2 + 1
    w = collections.deque(itertools.islice(i, m), m)
    if len(w) == m:
        yield list(w)
    for v in i:
        w.append(v)
        yield list(w)

# Group iterable 'i' into groups of size 'n'. The last group may be
# smaller if missing elements, but never empty. Example:
#
# >>> list(grouper(range(8), 3))
# [ [0, 1, 2]
# , [3, 4, 5]
# , [6, 7]
# ]
#
# Note: This iterates each group into a list. I tried several variations,
# some which never iterate the group leading to a possibly empty final
# group, and some which sample the group to filter the possible empty
# final group. Nonetheless this grouper was the most performant regardless
# of the size of 'i' or 'n'. See 'debug/grouper.timings.py'.
def grouper(i, n):
    i = iter(i)
    return iter(lambda: list(itertools.islice(i, n)), [])

def sine_tone(stream, frequency, duration, volume=1):
    if stream._format != pyaudio.paFloat32 or stream._channels != 1:
        raise ValueError("incompatible stream")

    n_samples = int(stream._rate * duration)

    s = lambda t: volume * math.sin(2 * math.pi * frequency * t / stream._rate)
    samples = (struct.pack("=f", s(t)) for t in range(n_samples))
    for buf in grouper(samples, int(stream._rate/2)):
        stream.write(b''.join(buf))

def timed(f, *args, **kwargs):
    t1 = time.monotonic()
    r = f(*args, **kwargs)
    t2 = time.monotonic()
    return (t2-t1, r)

# From another thread call into the asyncio thread to acquire the asyncio lock.
@contextlib.contextmanager
def async_acquire(lock, loop, timeout=None):
    future = asyncio.run_coroutine_threadsafe(lock.acquire(), loop)
    try:
        future.result(timeout=timeout)
    except:
        future.cancel()
        raise
    try:
        yield True
    finally:
        loop.call_soon_threadsafe(lock.release)

# Bring the challenge response created by the aiohttp_basicauth middleware in
# line with the standard aiohttp exception responses.
@aiohttp.web.middleware
async def body_401(request, handler):
    def modify(response):
        if response.status == 401 and not response.body:
            response.set_status(response.status)
            response.body = "{}: {}".format\
                ( response.status
                , response.reason
                ).encode()
            response.headers[aiohttp.hdrs.CONTENT_TYPE] =\
                "text/plain; charset=utf-8"
        return response
    try:
        return modify(await handler(request))
    except aiohttp.web.HTTPException as e:
        raise modify(e)

# Twilio signs each request (url including scheme, port, query string, and post
# parameters) with your AuthToken. If debug is not set, validate the signature.
# Return 401 if validation fails.
@aiohttp.web.middleware
async def verify_twilio(request, handler):
    twilio_info = request.app["twilio_info"]
    server_info = request.app["server_info"]
    if server_info.debug:
        return await handler(request)
    validator = RequestValidator(twilio_info.auth_token)
    scheme = request.headers.get("X-Forwarded-Proto", request.url.scheme)
    valid = validator.validate\
        ( uri = str(request.url.with_scheme(scheme))
        , params = await request.post()
        , signature = request.headers.get("X-TWILIO-SIGNATURE", "")
        )
    if not valid:
        raise aiohttp.web.HTTPUnauthorized
    return await handler(request)

async def run_server\
    ( id_type, ids_names
    , thread_info, twilio_info
    , server_info, other_info
    , notification_manager
    ):
    basic_auth = aiohttp_basicauth.BasicAuthMiddleware\
        ( username = server_info.username
        , password = server_info.password
        )
    app = aiohttp.web.Application(middlewares=[verify_twilio, body_401, basic_auth])
    app.add_routes([aiohttp.web.post("/sms", sms_reply)])
    app["id_type"] = id_type
    app["ids_names"] = ids_names
    app["thread_info"] = thread_info
    app["twilio_info"] = twilio_info
    app["server_info"] = server_info
    app["other_info"] = other_info
    app["notification_manager"] = notification_manager
    app["sms_commands"] = _sms_commands
    app["sms_response_prefix"] = "|\n\n"
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "localhost", 8080)
    await site.start()

    await thread_info.event_shutdown_asyncio.wait()
    await runner.cleanup()
    await thread_info.event_shutdown_program.wait()

_sms_commands = AliasRegistry()

def sms_register(*aliases):
    def _sms_register(f):
        p = "sms_"
        n = f.__name__
        if n.startswith(p) and len(n) > len(p):
            n = n[len(p):]
        _sms_commands.add_name(f, n, aliases)
        return f
    return _sms_register

@sms_register("ab")
async def sms_about(request):
    """
    About this app
    """
    m = f"{__title__} {__version__}\nby {__author__}"
    return m

@sms_register("co")
async def sms_commands(request):
    """
    List available commands
    """
    sms_commands = request.app["sms_commands"]
    msg = ["Commands"]
    for e in sms_commands.by_name.values():
        d = inspect.getdoc(e.entry) or ""
        d = " ".join(d.strip().split())
        msg.append("> {} ({}){}".format
            ( e.name
            , ", ".join(sorted(e.aliases))
            , f"\n{d}" if d else ""
            ))
    return "\n\n".join(msg)

@sms_register("sh")
async def sms_shutdown(request):
    """
    Shutdown program
    """
    thread_info = request.app["thread_info"]
    notification_manager = request.app["notification_manager"]
    async with thread_info.appt_lock, thread_info.print_lock:
        notify_shutdown("shutdown command", notification_manager)
    thread_info.event_shutdown_asyncio.set()
    return "Shutting down"

@sms_register("a")
async def sms_appt(request):
    """
    Show current appointment
    """
    thread_info = request.app["thread_info"]
    ids_names = request.app["ids_names"]
    async with thread_info.appt_lock:
        appt = thread_info.appt
        if appt is None:
            msg = "No appointment scheduled"
        else:
            msg = "{}\n{} => {}".format\
                ( ids_names[appt[0]]
                , appt[1].isoformat(" ", "minutes")
                , appt[2]
                )
        return msg

@sms_register("u")
async def sms_unschedule(request):
    """
    Unschedule current appointment, adding to blocklist and history
    """
    thread_info = request.app["thread_info"]
    ids_names = request.app["ids_names"]
    notification_manager = request.app["notification_manager"]
    async with thread_info.appt_lock:
        id_state = thread_info.id_state
        appt = thread_info.appt
        if appt is None:
            return "No appointment to cancel"
        cancelled = await thread_info.loop.run_in_executor(None, cancel, appt)
        if not cancelled:
            return "Unable to cancel appointment"
        thread_info.appt = None
        thread_info.appt_blocklist.add(appt[:2])
        thread_info.appt_history.appendleft(appt)
        thread_info.sched_avail = True
        thread_info.sched_tries = 0
        async with thread_info.print_lock:
            notify_cancel(appt, id_state, ids_names, notification_manager)
        return format_cancel_message_sms(appt, ids_names)


# verify
#     No current appointment to verify
#
# Status of current appt 'qIM98yxH0':
#     Unable to verify appointment
#     Appointment cancelled
#     Appointment confirmed
#
#
# verify 1..10
#     No such past appointment to verify
#
# Status of past appt 'qIM98yxH0':
#     Unable to verify appointment
#     Appointment cancelled
#     Appointment confirmed
#
#
# verify qIM98yxH0
#
# Status of given appt 'qIM98yxH0':
#     Unable to verify appointment
#     Appointment cancelled
#     Appointment confirmed


@sms_register("v")
async def sms_verify(request, target=None):
    """
    Verify appointment given confirmation code, history index, and defaulting
    to current appointment.
    """
    try:
        index = int(target) - 1
    except (ValueError, TypeError):
        is_index = False
    else:
        is_index = True

    thread_info = request.app["thread_info"]
    async with thread_info.appt_lock:

        appt = thread_info.appt
        appt_history = thread_info.appt_history

        if target is None:
            if appt is None:
                return "No current appointment to verify"
            confirmation = appt[2]
            msg = [f"Status of current appt '{confirmation}':\n"]
        elif is_index and index in range(appt_history.maxlen):
            if index >= len(appt_history):
                return "No such past appointment to verify"
            confirmation = appt_history[index][2]
            msg = [f"Status of past appt '{confirmation}':\n"]
        else:
            confirmation = target
            msg = [f"Status of given appt '{confirmation}':\n"]

    status = await thread_info.loop.run_in_executor\
        (None, get_status, confirmation)

    if status is AppointmentStatus.Undefined:
        msg.append("Unable to verify appointment")
    elif status is AppointmentStatus.Cancelled:
        msg.append("Appointment cancelled")
    elif status is AppointmentStatus.Scheduled:
        msg.append("Appointment verified")
    else:
        msg.append("Unknown error")

    return "\n".join(msg)

@sms_register("h")
async def sms_history(request):
    """
    Show appointment history
    """
    thread_info = request.app["thread_info"]
    ids_names = request.app["ids_names"]
    async with thread_info.appt_lock:
        appt_history = thread_info.appt_history
        if not appt_history:
            return "No appointment history"
        msg = ["Appt history, newest first:", ""]
        for i,v in enumerate(appt_history, start=1):
            msg.append("\n#{}:\n{} => {}\n{}".format
                ( i
                , v[1].isoformat(" ", "minutes")
                , v[2]
                , ids_names[v[0]]
                ))
        return "\n".join(msg)

@sms_register("cl")
async def sms_clear(request):
    """
    Clear appointment blocklist
    """
    thread_info = request.app["thread_info"]
    async with thread_info.appt_lock:
        thread_info.appt_blocklist.clear()
        return "Blocklist cleared"

@sms_register("b")
async def sms_blocklist(request):
    """
    Show appointment blocklist
    """
    thread_info = request.app["thread_info"]
    ids_names = request.app["ids_names"]
    async with thread_info.appt_lock:
        blocklist = thread_info.appt_blocklist
        if not blocklist:
            return "Blocklist empty"
        msg = ["Appt blocklist:", ""]
        for loc_id, d in blocklist:
            msg.append(f"\n{ids_names[loc_id]}\n{d}")
        return "\n".join(msg)

@sms_register("st")
async def sms_status(request):
    """
    Show appointment status across locations
    """
    thread_info = request.app["thread_info"]
    async with thread_info.appt_lock:
        return format_appt_message_sms\
            ( thread_info.id_state
            , request.app["ids_names"]
            , request.app["id_type"]
            , datetime.datetime.now().isoformat(" ", "seconds")
            , thread_info.appt
            )

async def sms_reply(request):
    sms_commands = request.app["sms_commands"]
    sms_prefix = request.app["sms_response_prefix"]
    data = await request.post()
    try:
        body = data["Body"].lower().split()
    except KeyError:
        raise aiohttp.web.HTTPBadRequest
    try:
        cmd = sms_commands[body[0]].entry(request, *body[1:])
    except IndexError:
        msg = "Missing command"
    except KeyError:
        msg = "Unknown command"
    except TypeError:
        msg = "Invalid command arguments"
    else:
        msg = await cmd
    response = MessagingResponse()
    response.message(sms_prefix + msg)
    return aiohttp.web.Response(text=str(response), content_type="text/xml")

# Manage the startup and shutdown of the asyncio loop.
def run_asyncio(thread_info, coro):
    loop = thread_info.loop
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(coro)
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.run_until_complete(loop.shutdown_default_executor())
        loop.close()
    #pending = asyncio.all_tasks(loop=loop)
    #for task in pending:
    #    task.cancel()
    #group = asyncio.gather(*pending, return_exceptions=True)
    #loop.run_until_complete(group)
    #loop.close()

# Initialize the cross-thread data and start each thread. Handle any
# exceptions with notifications, and ensure a clean shutdown of each thread.
# If there was a selenium exception, take a screenshot of the driver before
# re-raising. As the monitor may raise an error without network connectivity,
# wrap it in a retry function. This is the main entry point of the program.
def run\
    ( ids, id_type, ids_names
    , appt_info, twilio_info, selenium_info
    , vosk_info, server_info, other_info
    , notification_manager
    ):
    sound = Sound()
    sound.start()
    thread_info = ThreadInfo\
        ( appt=None
        , appt_blocklist=set()
        , appt_history=collections.deque(maxlen=10)
        , sched_avail=True
        , sched_tries=0
        # { location id: (appt available, how many, first date) }
        # None is a unique identifier that ensures that *any* input
        # will cause a change of state and therefore print the first
        # appointment.
        , id_state={ i:(None,None,None) for i in ids }
        , appt_lock=asyncio.Lock()
        , event_shutdown_program=asyncio.Event()
        , event_shutdown_asyncio=asyncio.Event()
        , loop=asyncio.get_event_loop()
        , queue=queue.Queue()
        , print_lock=asyncio.Lock()
        )
    server = threading.Thread\
        ( target=run_asyncio
        , args=
          ( thread_info
          , run_server
            ( id_type, ids_names
            , thread_info, twilio_info
            , server_info, other_info
            , notification_manager
            )
          )
        )
    server.start()
    try:
        p = "screenshots"
        os.makedirs(p, exist_ok=True)
        with selenium_info.driver:
            try:
                restart_logger = async_acquire\
                    (thread_info.print_lock, thread_info.loop)
                restart_monitor = retry_on_exception\
                    ( # x**3 from [60,600] to the nearest ten
                      # for a total of 19 minutes, 10 seconds
                      [60, 160, 330, 600]
                    , 60*60
                    , (requests.exceptions.ConnectionError,)
                    , restart_logger(notify_retryout)
                    , restart_logger(notify_retryin)
                    # Following passed to the notifiers:
                    , notification_manager
                    )(monitor)
                restart_monitor\
                    ( id_type, ids_names
                    , appt_info, twilio_info, thread_info
                    , selenium_info, vosk_info, other_info
                    , sound, notification_manager
                    )
            except WebDriverException:
                selenium_info.driver.save_screenshot\
                    (os.path.join(p, "error.png"))
                raise
    except Exception:
        # The notification configuration may result in a different set of
        # notifications being submitted. Therefore, acquire the lock.
        with async_acquire(thread_info.print_lock, thread_info.loop):
            try:
                notify_error(notification_manager, twilio_info)
            except requests.exceptions.ConnectionError:
                notify_untextable(notification_manager)
        raise
    except KeyboardInterrupt:
        with async_acquire(thread_info.print_lock, thread_info.loop):
            try:
                notify_shutdown\
                    ( "keyboard interrupt", notification_manager
                    , twilio_info=twilio_info
                    )
            except requests.exceptions.ConnectionError:
                notify_untextable(notification_manager)
    finally:
        thread_info.loop.call_soon_threadsafe\
            (thread_info.event_shutdown_asyncio.set)
        thread_info.loop.call_soon_threadsafe\
            (thread_info.event_shutdown_program.set)
        server.join()
        sound.shutdown()

def monitor\
    ( id_type, ids_names
    , appt_info, twilio_info, thread_info
    , selenium_info, vosk_info, other_info
    , sound, notification_manager
    ):
    # No lock necessary as 'id_state' is only modified by this thread and
    # read/read is thread-safe.
    state = thread_info.id_state
    index = 0
    sched_max = 4
    # Schedule the wait on the asyncio thread and in this thread, wait
    # on the future result - same effect.
    future_shutdown = asyncio.run_coroutine_threadsafe\
        ( thread_info.event_shutdown_asyncio.wait()
        , thread_info.loop
        )
    # Generate a logger which acquires 'print_lock' by calling the decorator
    # (and context-manager) manually. The first call to the notifier collects
    # the notification types; it is the second call (the actual notification
    # chain) that requires the lock. This logger should only be used when no
    # other lock is in effect.
    logger = async_acquire(thread_info.print_lock, thread_info.loop)\
        (notification_manager.notify_print
            (NotificationCategory.Logging, NotificationPriority.Important)
        )
    while True:
        t1 = time.monotonic()
        # Queue for any functions from another thread that must be executed
        # in this thread. The queue has its own lock.
        while True:
            try:
                f = thread_info.queue.get(block=False)
            except queue.Empty:
                break
            else:
                f(id_type=id_type, id_state=state)
        # Run the requests to generate the new state.
        changes, state_new = get_availabilities\
            (id_type, ids_names, state, logger)
        # Dispatch decisions based on the new state. As cross-thread state is
        # involved, acquire the coarse lock.
        with async_acquire(thread_info.appt_lock, thread_info.loop):
            # Reset decision variables if there were any changes
            if changes:
                thread_info.sched_tries = 0
                thread_info.sched_avail = True
            # Scheduling is time-sensitive and should occur first. Initially
            # 'sched_avail' is True if there are any matching appointments
            # but is set to False if an appointment has already been taken or
            # the maximum scheduling attemps have been exceeded. Therefore
            # 'sched_avail' indicates whether scheduling should be attempted.
            if (    thread_info.appt is None
                and thread_info.sched_avail
                and thread_info.sched_tries < sched_max
               ):
                thread_info.sched_tries += 1
                thread_info.appt, thread_info.sched_avail, sched_errors =\
                    schedule\
                    ( id_type, state_new
                    , appt_info, selenium_info, vosk_info
                    , thread_info.appt_blocklist
                    )
            else:
                thread_info.sched_avail = False
            if changes:
                notify_appointments\
                    ( index, state_new
                    , thread_info.appt, thread_info.sched_avail
                    , id_type, ids_names, notification_manager
                    , twilio_info
                    )
            # Beep only for more or sooner appointments
            if changes and sooner_availability(state_new, state):
                notification_manager.notify_sound\
                    ( NotificationCategory.Appointment
                    , NotificationPriority.Standard
                    )(sound)
            # Display any scheduling attempts after displaying the
            # appointments
            if thread_info.sched_avail:
                notify_schedule\
                    ( thread_info.appt, sched_errors
                    , thread_info.sched_tries, sched_max
                    , state_new, ids_names, notification_manager
                    , twilio_info, twilio_sleep=(2 if changes else 0)
                    )
            if thread_info.sched_avail and thread_info.appt is None:
                timeout = other_info.timeout_scheduling
            else:
                timeout = other_info.timeout_standard
            # Here the new state supplants the old state
            thread_info.id_state = state = state_new
        t2 = time.monotonic()
        sleep = max(timeout - t2 + t1, other_info.timeout_min)
        try:
            future_shutdown.result(sleep)
        except concurrent.futures.TimeoutError:
            pass
        else:
            break
        index += 1

def format_sched_message_print\
    ( appt, sched_errors
    , sched_tries, sched_max
    , id_state, ids_names
    ):
    w = max(len(ids_names[i]) for i in id_state) + 8
    a = sum(1 for s in id_state.values() if s[0])
    f = lambda i: " "*4 + ids_names[i] + " "*(w - len(ids_names[i]))
    m = [ "Update, Scheduling: {} available, {} scheduled{}".format
            ( a
            , 0 if appt is None else 1
            , f" ({sched_tries+1}/{sched_max})" if appt is None else ""
            )
        ]
    m.extend(f(i[0]) + "Error scheduling: " + i[2] for i in sched_errors)
    if appt is not None:
        m.append\
            ( f(appt[0])
            + appt[1].isoformat(" ", "minutes")
            + " => "
            + appt[2]
            )
    m = "\n".join(m)
    return m

def format_sched_message_sms\
    ( appt, sched_errors
    , sched_tries, sched_max
    , id_state, ids_names
    ):
    a = sum(1 for s in id_state.values() if s[0])
    m = [ "Scheduled {} of {} appointment{}{}:".format
            ( 0 if appt is None else 1
            , a
            , "s" if a != 1 else ""
            , f" ({sched_tries+1}/{sched_max})" if appt is None else ""
            )
        ]
    m.extend\
        ( "\n{}\n{}: {}".format(ids_names[i[0]], "Error scheduling", i[2])
          for i in sched_errors
        )
    if appt is not None:
        m.append("\n{}\n{} => {}".format\
            ( ids_names[appt[0]]
            , appt[1].isoformat(" ", "minutes")
            , appt[2]
            ))
    m = "\n".join(m)
    return m

def notify_schedule\
    ( appt, sched_errors
    , sched_tries, sched_max
    , id_state, ids_names
    , notification_manager
    , twilio_info, twilio_sleep=0
    ):
    m = format_sched_message_print\
        ( appt, sched_errors
        , sched_tries, sched_max
        , id_state, ids_names
        )
    notification_manager.notify_print\
        (NotificationCategory.Scheduling, NotificationPriority.Important)\
        (m)

    m = format_sched_message_sms\
        ( appt, sched_errors
        , sched_tries, sched_max
        , id_state, ids_names
        )
    notification_manager.notify_twilio\
        (NotificationCategory.Scheduling, NotificationPriority.Important)\
        (m, twilio_info, block_sleep=twilio_sleep)

def format_cancel_message_print(appt, id_state, ids_names):
    w = max(len(ids_names[i]) for i in id_state) + 8
    m = "\n".join(
        ( "Update, Scheduling: 1 cancellation"
        , "    {:{}}{} => {}".format\
            ( ids_names[appt[0]]
            , w
            , appt[1].isoformat(" ", "minutes")
            , appt[2]
            )
        ))
    return m

def format_cancel_message_sms(appt, ids_names):
    m = "Cancelled 1 appointment:\n\n{}\n{} => {}".format\
        (ids_names[appt[0]], *appt[1:])
    return m

def notify_cancel(appt, id_state, ids_names, notification_manager):
    m = format_cancel_message_print(appt, id_state, ids_names)
    notification_manager.notify_print\
        (NotificationCategory.Cancelling, NotificationPriority.Important)\
        (m)


# Scheduled 1 of 3 appointments (1/4):
#
# Lodi - Permits/License
# Error scheduling
#
# South Plainfield - Permits/License
# Error scheduling
#
# Bayonne - Permits/License
# 2021-10-05 12:15 => qIM98yxH0
#
#
# Cancelled 1 appointment:
#
# Bayonne - Permits/License
# 2021-10-05 12:15 => qIM98yxH0
#
#
# Appt history, newest first:
#
# #1:
# 2021-10-05 12:15 => qIM98yxH0
# Bayonne - Permits/License
#
# #2:
# 2021-10-03 09:15 => t8GAR4vf5
# Bayonne - Permits/License
#
# #10:
# 2021-09-18 11:30 => n468Asfh4
# South Plainfield - Permits/License


# Update, Appointments: 2021-09-11 19:10:45
#     Lodi - Permits/License                    1 available, 2021-09-18 09:15
#     South Plainfield - Permits/License        1 available, 2021-09-18 11:30
#     Bayonne - Permits/License                 1 available, 2021-09-18 12:15
#
# Update, Scheduling: 3 available, 1 scheduled (1/4)
#     Lodi - Permits/License                    Error scheduling
#     South Plainfield - Permits/License        Error scheduling
#     Bayonne - Permits/License                 2021-10-05 12:15 => qIM98yxH0
#
# Update, Scheduling: 1 cancellation
#     Bayonne - Permits/License                 2021-10-05 12:15 => qIM98yxH0


def cancel(appt):
    url_base = "https://telegov.njportal.com"
    url_stop = url_base + "/njmvc/CustomerCreateAppointments/Delete/{}"

    try:
        r = requests.get(url_stop.format(appt[2]))
    except requests.exceptions.ConnectionError:
        return False

    return r.status_code == 200

def get_status(confirmation):
    url_base = "https://telegov.njportal.com"
    url_edit = url_base + "/njmvc/AppointmentWizardConfirmation/{}/Edit"

    try:
        r = requests.get(url_edit.format(confirmation))
    except requests.exceptions.ConnectionError:
        return AppointmentStatus.Undefined

    if r.status_code != 200:
        return AppointmentStatus.Undefined

    m = re.search(r"""var\s+message\s*=\s*(["'])([^"']+)\1\s*;""", r.text)

    if m is None:
        return AppointmentStatus.Undefined

    if m.group(2) == "cancelled":
        return AppointmentStatus.Cancelled

    return AppointmentStatus.Scheduled

# For each location with a valid appointment, attempt to schedule that
# appointment. Stop when an appointment has been confirmed as scheduled.
def schedule\
    ( id_type, loc_id_state
    , appt_info, selenium_info, vosk_info
    , blocklist
    ):
    # If an appointment matched but couldn't be taken, append it to 'errors'.
    # If 'appt' is None, either there were no matches, or every match was an
    # error.
    appt = None
    match = False
    errors = []

    url_base = "https://telegov.njportal.com"
    url_appt = url_base + "/njmvc/AppointmentWizard/{tid}/{lid}/{d}/{t}"

    err_msgs =\
        { DecaptchaDOSError: "CAPTCHA Blocked"
        , DecaptchaFailError: "CAPTCHA Failed"
        , TimeoutException: "Timeout"
        }

    for loc_id,s in loc_id_state.items():
        if not s[0]:
            continue
        d = s[2]
        if not appt_info.check_calendar(d):
            continue
        if (loc_id, d) in blocklist:
            continue
        match = True

        u = url_appt.format\
            ( tid = id_type
            , lid = loc_id
            , d = s[2].strftime("%Y-%m-%d")
            , t = s[2].strftime("%H%M").lstrip("0")
            )
        selenium_info.driver.get(u)
        if selenium_info.driver.current_url != u:
            errors.append((loc_id, s[2], "Mismatched URLs"))
            continue

        try:
            confirmation = selenium_info.autofill\
                (appt_info, selenium_info, vosk_info)
        except tuple(err_msgs) as e:
            errors.append((loc_id, s[2], err_msgs[type(e)]))
            continue

        if not confirmation:
            errors.append((loc_id, s[2], "No confirmation"))
            continue

        # (location id, datetime, confirmation)
        appt = (loc_id, s[2], confirmation)
        break

    return (appt, match, errors)

def datetime_within(dt, d_t0, d_t1):
    # Check that dt is within [d_t0, d_t1] (inclusive).
    # A date without time includes the entire date
    if type(d_t0) is datetime.date:
        d_t0 = datetime.datetime.combine(d_t0, datetime.time.min)
    if type(d_t1) is datetime.date:
        d_t1 = datetime.datetime.combine(d_t1, datetime.time.max)
    if dt < d_t0 or dt > d_t1:
        return False
    return True

def road_autofill(appt_info, selenium_info, vosk_info, timeout=5):
    driver = selenium_info.driver

    driver.find_element_by_id("firstName")\
        .send_keys(appt_info.first_name)
    driver.find_element_by_id("lastName")\
        .send_keys(appt_info.last_name)
    driver.find_element_by_id("email")\
        .send_keys(appt_info.email)
    driver.find_element_by_id("phone")\
        .send_keys(appt_info.phone)
    driver.find_element_by_id("driverLicense")\
        .send_keys(appt_info.driver_license)
    driver.find_element_by_id("validationNum")\
        .send_keys(appt_info.permit_validation)

    driver.find_element_by_css_selector\
        ("input[name='Attest'][type='checkbox']").click()
    driver.find_element_by_css_selector\
        ("input[name='PtaAttest'][type='checkbox']").click()

    Select(driver.find_element_by_id("permitClass"))\
        .select_by_value("D")

    driver.find_element_by_css_selector("input.btn[value='submit'i]")\
        .click()

    # The road test form is protected by reCAPTCHA v3 with permissive score
    # handling, and so has yet to become an issue.

    # TimeoutException is handled in the parent.
    el = WebDriverWait(driver, timeout).until\
        (lambda d: d.find_element_by_css_selector("#divReview span"))

    # Though not documented, this appears to first strip the text.
    return el.text

def permit_autofill(appt_info, selenium_info, vosk_info, timeout=5):
    driver = selenium_info.driver

    driver.find_element_by_id("firstName")\
        .send_keys(appt_info.first_name)
    driver.find_element_by_id("lastName")\
        .send_keys(appt_info.last_name)
    driver.find_element_by_id("email")\
        .send_keys(appt_info.email)
    driver.find_element_by_id("phone")\
        .send_keys(appt_info.phone)
    driver.find_element_by_id("birthDate")\
        .send_keys(appt_info.birth_date)

    driver.find_element_by_css_selector\
        ("input[name='Attest'][type='checkbox']").click()
    driver.find_element_by_css_selector\
        ("input[name='PtaAttest'][type='checkbox']").click()

    Select(driver.find_element_by_id("permitType"))\
        .select_by_value("Class D")

    # Decaptcha errors are handled in the parent.
    decaptcha_v2(selenium_info, vosk_info, error=True)

    driver.find_element_by_css_selector("input.btn[value='submit'i]")\
        .click()

    # TimeoutException is handled in the parent.
    el = WebDriverWait(driver, timeout).until\
        (lambda d: d.find_element_by_css_selector("#divReview span"))

    return el.text

def knowledge_autofill(appt_info, selenium_info, vosk_info, timeout=5):
    driver = selenium_info.driver

    driver.find_element_by_id("firstName")\
        .send_keys(appt_info.first_name)
    driver.find_element_by_id("lastName")\
        .send_keys(appt_info.last_name)
    driver.find_element_by_id("email")\
        .send_keys(appt_info.email)
    driver.find_element_by_id("phone")\
        .send_keys(appt_info.phone)
    driver.find_element_by_id("driverLicense")\
        .send_keys(appt_info.driver_license)

    driver.find_element_by_css_selector\
        ("input[name='Attest'][type='checkbox']").click()
    driver.find_element_by_css_selector\
        ("input[name='PtaAttest'][type='checkbox']").click()

    Select(driver.find_element_by_id("test"))\
        .select_by_value("Auto")

    # Decaptcha errors are handled in the parent.
    decaptcha_v2(selenium_info, vosk_info, error=True)

    driver.find_element_by_css_selector("input.btn[value='submit'i]")\
        .click()

    # TimeoutException is handled in the parent.
    el = WebDriverWait(driver, timeout).until\
        (lambda d: d.find_element_by_css_selector("#divReview span"))

    return el.text

# Copy the state, and for each location update the appointment availability but
# only if there have been changes. First request the complete dataset from the
# frontend, then for each missing location fill in the availability data using
# a backend endpoint. Return the new state and the number of updates.
def get_availabilities(id_type, ids_names, id_state, logger):
    url_base = "https://telegov.njportal.com"
    url_appt = f"{url_base}/njmvc/AppointmentWizard/{id_type}"
    url_date = f"{url_base}/njmvc/CustomerCreateAppointments/GetNextAvailableDate"

    unavailable = "No Appointments Available"
    avail_ids = {}
    avail_all = {}
    headers = {"Referer": url_base}

    BreakError = type("BreakError", (Exception,), {})

    # Get the complete availability dataset and check for missing locations.
    try:
        r = requests.get(url_appt, headers=headers)
        if r.status_code != requests.codes.ok:
            m = "Error: unable to request collected availability"
            raise BreakError
        r = re.search(r"var\s+timeData\s*=\s*(\[.*\])", r.text)
        if not r:
            m = "Error: unknown format for collected availability"
            raise BreakError
        avail_all = json.loads(r.group(1))
        avail_all = {i["LocationId"]:i["FirstOpenSlot"] for i in avail_all}
        if len(avail_all) != len(ids_names):
            m = "Warning: collected availability contains {} of {} locations"
            m = m.format(len(avail_all), len(ids_names))
            raise BreakError
    except BreakError:
        logger(m, file=sys.stderr)

    # Get the relevant locations from the complete dataset, skipping any
    # unknown responses.
    for i in id_state.keys():
        if i not in avail_all:
            continue
        r = avail_all[i]
        n = get_availability(r)
        if not n[0] and r != unavailable:
            m = "Error: unknown response, skipping '{}' from collection"
            m = m.format(ids_names[i])
            logger(m, file=sys.stderr)
            continue
        avail_ids[i] = n

    # Get the data for each missing location, if possible. Skip locations on
    # network errors. Interpret unknown responses as lack of availability.
    for i in id_state.keys():
        if i in avail_ids:
            continue
        d = {"appointmentTypeId": id_type, "locationId": i}
        r = requests.post(url_date, d, headers=headers)
        if r.status_code != requests.codes.ok:
            m = "Error: unable to update availability for '{}'"
            m = m.format(ids_names[i])
            logger(m, file=sys.stderr)
            continue
        r = json.loads(r.text)["next"]
        n = get_availability(r)
        if not n[0] and r != unavailable:
            m = "Error: unknown response, assuming no availability for '{}'"
            m = m.format(ids_names[i])
            logger(m, file=sys.stderr)
        avail_ids[i] = n

    # Copy the state and update locations with differing appointment data. At
    # this point, 'avail_ids' is a subset of 'state' ie when network conditions
    # cause locations to be skipped.
    state_new = id_state.copy()
    changes = 0
    for i,n in avail_ids.items():
        if state_new[i] != n:
            changes += 1
            state_new[i] = n

    return changes, state_new

# An example response:
# { "next":
#     "66 Appointments Available <br/> Next Available: 11/03/2021 08:30 AM"
# }
def get_availability(s):
    c = re.search(r"^\s*(\d+)\s*appointment", s, re.IGNORECASE)
    d = re.search(r"next available:\s*(.*)", s, re.IGNORECASE)
    if c is None or d is None:
        return (False,None,None)
    c = c.group(1)
    d = d.group(1)
    d = dateutil.parser.parse(d)
    return (True,c,d)

def format_availability(s):
    if s[0]:
        m = "{} available, {}".format(s[1], s[2].isoformat(" ", "minutes"))
        return m
    return "No appointments available"

def format_appt_message_print(id_state, ids_names, when, appt):
    w = max(len(ids_names[i]) for i in id_state) + 8
    m = [ "Update, Appointments: {} ({} appointment{} scheduled)".format
            (when, *((0,"s") if appt is None else (1,"")))
        ]
    m.extend\
        ( ( " "*4 + ids_names[i] + " "*(w - len(ids_names[i]))
          + format_availability(id_state[i])
          )
          for i in id_state
        )
    m = "\n".join(m)
    return m

def format_appt_message_sms(id_state, ids_names, id_type, when, appt):
    url_base = "https://telegov.njportal.com"
    url_appt = f"{url_base}/njmvc/AppointmentWizard/{id_type}"

    m = ["Update: {}".format(when)]
    m.extend\
        ( "\n{}\n{}".format(ids_names[i], format_availability(id_state[i]))
          for i in id_state
        )
    m = "\n".join(m)
    m += "\n\n" + url_appt
    m += "\n\n" + "{} appointment{} scheduled".format\
        (*((0,"s") if appt is None else (1,"")))
    return m

# Return True if any location in 'is_new' offers a more recent
# appointment than the corresponding location in the 'is_old'.
def sooner_availability(is_new, is_old):
    for i,sn in is_new.items():
        so = is_old[i]
        if sn[0] and not so[0]:
            return True
        if sn[0] and so[0] and sn[2] < so[2]:
            return True
    return False

def notify_appointments\
    ( i, id_state, appt, sched_avail, id_type, ids_names
    , notification_manager, twilio_info, twilio_sleep=0
    ):
    when = datetime.datetime.now().isoformat(" ", "seconds")

    if sched_avail:
        priority = NotificationPriority.Important
    else:
        priority = NotificationPriority.Standard

    m = format_appt_message_print(id_state, ids_names, when, appt)
    notification_manager.notify_print\
        (NotificationCategory.Appointment, priority)\
        (m)

    # don't send initial state of only no appointments
    # no need to waste sms with useless information
    avail = any(s[0] for s in id_state.values())
    if i == 0 and not avail:
        return

    m = format_appt_message_sms(id_state, ids_names, id_type, when, appt)
    notification_manager.notify_twilio\
        (NotificationCategory.Appointment, priority)\
        (m, twilio_info, block_sleep=twilio_sleep)


# Update, Logging: 2021-09-11 19:10:45: Received keyboard interrupt
#   Shutting down
#
# Update, Logging: 2021-09-11 19:10:45: Received shutdown command
#   Shutting down
#
#
# Shutting down

# Update, Logging: 2021-09-11 19:10:45: Network interrupt, retrying (1/4)
#   Attempting monitor restart in 60 seconds

# Update, Logging: 2021-09-11 19:10:45: Shutdown during network interrupt
#   Unable to notify Twilio


def format_retryout_message_print(when, retry_idx, retry_max, name, delay):
    m = ( "Update, Logging: {}: Network interrupt, retrying ({}/{})\n"
          "{}Attempting restart of '{}' in {} seconds"
        )
    return m.format(when, retry_idx, retry_max, " "*4, name, delay)

def format_retryin_message_print(when, name):
    m = "Update, Logging: {}: Restarting '{}'"
    return m.format(when, name)

def notify_retryout(idx, timeouts, exc, f, notification_manager):
    when = datetime.datetime.now().isoformat(" ", "seconds")
    m = format_retryout_message_print\
        ( when
        , idx+1
        , len(timeouts)
        , f.__name__
        , timeouts[idx]
        )
    notification_manager.notify_print\
        (NotificationCategory.Logging, NotificationPriority.Important)\
        (m, file=sys.stderr)

def notify_retryin(idx, timeouts, exc, f, notification_manager):
    when = datetime.datetime.now().isoformat(" ", "seconds")
    m = format_retryin_message_print(when, f.__name__)
    notification_manager.notify_print\
        (NotificationCategory.Logging, NotificationPriority.Important)\
        (m, file=sys.stderr)

def format_shutdown_message_print(when, reason):
    msg = "Update, Logging: {}: Received {}\n{}Shutting down"
    return msg.format(when, reason, " "*4)

def format_shutdown_message_sms():
    return "Shutting down"

def notify_shutdown\
    ( reason, notification_manager
    , twilio_info=None, twilio_sleep=0, skip_print=False
    ):
    when = datetime.datetime.now().isoformat(" ", "seconds")
    t = (NotificationCategory.Logging, NotificationPriority.Important)

    if not skip_print:
        m = format_shutdown_message_print(when, reason)
        notification_manager.notify_print(*t)(m, file=sys.stderr)

    if twilio_info is not None:
        m = format_shutdown_message_sms()
        notification_manager.notify_twilio(*t)(m, twilio_info, block_sleep=twilio_sleep)

def notify_error(notification_manager, twilio_info, twilio_sleep=0):
    m = f"'{__title__}' shutting down unexpectedly..."
    notification_manager.notify_twilio\
        (NotificationCategory.Logging, NotificationPriority.Important)\
        (m, twilio_info, block_sleep=twilio_sleep)

def format_untextable_message_print(when):
    m = ( "Update, Logging: {}: Shutdown during network interrupt\n"
          "{}Unable to notify Twilio"
        )
    return m.format(when, " "*4)

def notify_untextable(notification_manager):
    when = datetime.datetime.now().isoformat(" ", "seconds")
    m = format_untextable_message_print(when)
    notification_manager.notify_print\
        (NotificationCategory.Logging, NotificationPriority.Standard)\
        (m, file=sys.stderr)


# Update, Logging: 2021-09-11 19:10:45: Entering timelock
#   TextMessage:Appointment:Standard: 00:00 - 03:00
#
#
# Entering timelock:
# 00:00 - 03:00
#
# TextMessage
# Appointment
# Standard
#
#
# Update, Logging: 2021-09-11 19:10:45: Rate-limited, dropped:
#   TextMessage:Appointment:Standard


def make_timelock_notify(twilio_info, twilio_sleep=0):
    def inner(timelock, request):
        return timelock_notify\
            ( twilio_info
            , timelock
            , request
            , twilio_sleep=twilio_sleep)
    return inner

def timelock_notify(twilio_info, timelock, request, twilio_sleep=0):
    when = datetime.datetime.now().isoformat(" ", "seconds")
    shift = "Entering" if timelock.locked else "Leaving"
    bound = [t.isoformat("minutes") for t in (timelock.start, timelock.stop)]
    msg_p = "Update, Logging: {}: {} timelock:\n{}{}:{}:{}: {} - {}".format\
            ( when
            , shift
            , " "*4
            , *(e.name for e in request.info())
            , *bound
            )
    msg_t = "{} timelock:\n{} - {}\n\n{}\n{}\n{}".format\
            ( shift
            , *bound
            , *(e.name for e in request.info())
            )
    t = (NotificationCategory.Logging, NotificationPriority.Important)
    request.manager.notify_print(*t)\
        (msg_p, file=sys.stderr)
    request.manager.notify_twilio(*t)\
        (msg_t, twilio_info, block_sleep=twilio_sleep)

def ratelimit_notify(ratelimit, request):
    when = datetime.datetime.now().isoformat(" ", "seconds")
    if ratelimit.current < 1:
        msg = "Update, Logging: {}: Rate-limited and dropped:\n{}{}:{}:{}"\
            .format(when, " "*4, *(e.name for e in request.info()))
        request.manager.notify_print\
            (NotificationCategory.Logging, NotificationPriority.Important)\
            (msg)

@NotificationManager.register_notifier(NotificationResource.Terminal)
def notify_print(message, **kwargs):
    print(message, end="\n\n", **kwargs)

@NotificationManager.register_notifier(NotificationResource.TextMessage)
def notify_twilio(message, twilio_info, block_sleep=0):
    # Workaround delay to ensure order between nearly-simultaneous sms. I am
    # unwilling to merge messages and unsure of alternative workarounds. Note
    # that time.sleep(0) does allow a context switch.
    time.sleep(block_sleep)
    message = "|\n\n" + message
    sms = twilio_info.client.messages.create\
        ( body=message
        , from_=twilio_info.from_
        , to=twilio_info.to
        )
    return sms

@NotificationManager.register_notifier(NotificationResource.Sound)
def notify_sound(sound):
    notes =\
        [ (261.63, 1.0) # C
        , (369.99, 1.0) # F#
        , (622.25, 2.0) # D#
        , (587.33, 0.5) # D
        , (622.25, 0.5)
        , (587.33, 0.5)
        , (622.25, 0.5)
        , (587.33, 0.5)
        , (622.25, 0.5)
        , (587.33, 0.5)
        ]
    sound.replace(notes)


def appt_data(update_globals=False, globals_conflict=True):
    url_base = "https://telegov.njportal.com"
    url_appt = url_base + "/njmvc/AppointmentWizard"

    atype_names =\
        [ ['renew', 'registration', 'mobile']
        , ['renew', 'registration']
        , ['renew', 'cdl']
        , ['renew']
        , ['title', 'registration', 'replace']
        , ['title', 'registration']
        , ['road', 'agricultural']
        , ['road', 'motorcycle']
        , ['road', 'moped']
        , ['road', 'cdl']
        , ['road']
        , ['permit', 'mobile']
        , ['permit', 'cdl']
        , ['permit']
        , ['knowledge', 'cdl']
        , ['knowledge']
        , ['nondriverid']
        , ['realid', 'mobile']
        , ['realid']
        , ['transfer']
        ]

    assert len(set("_".join(l) for l in atype_names)) == len(atype_names)

    atypes = []
    anames = {}
    seen_ids = set()
    seen_names = set()

    response = requests.get(url_appt, headers={"Referer": url_base})
    response.raise_for_status()
    soup = bs4.BeautifulSoup(response.text, "html.parser")
    tags = soup.select\
        ("#step-1 div[data-type][class~='cards'] > a[class*='button' i]")

    for tag in tags:
        url = tag["href"]
        tid = int(re.search(r"/(\d+)$", url).group(1))
        txt = tag.select_one("span[class*='title' i]").string.strip()
        try:
            txt = txt[:txt.index("(")]
        except ValueError:
            pass
        txt = ( txt.lower()
              . replace(" id", "id")
              . replace("renewal", "renew")
              . replace("replacement", "replace")
              )
        txt = "".join(c for c in txt if c.isalpha() or c.isspace())
        txt = txt.strip()
        if tid in seen_ids:
            continue
        if txt in seen_names:
            m = f"Duplicate appointment-type name: '{txt}'"
            raise ValueError(m)
        atypes.append(types.SimpleNamespace(id=tid, name_source=txt))
        seen_ids.add(tid)
        seen_names.add(txt)

    if len(atypes) != len(atype_names):
        m = "Unexpected number of appointment types"
        raise ValueError(m)

    cost = numpy.zeros((len(atype_names), len(atypes)))

    for atype in atypes:
        url = url_appt + "/" + str(atype.id)
        response = requests.get(url, headers={"Referer": url_base})
        response.raise_for_status()
        locations = json.loads\
            ( re.search(r"var\s+locationData\s*=\s*(\[.*\])", response.text)
            . group(1)
            )
        locations =\
            { l["Id"]:l["Name"]
              for l in sorted(locations, key=lambda l: l["Id"])}
        atype.locations = locations

    for i,atype in enumerate(atypes):
        for j,name in enumerate(atype_names):
            c = set(atype.name_source.split()) & set(name)
            cost[j,i] = len(c)

    result = scipy.optimize.linear_sum_assignment(cost, maximize=True)

    for r,c in zip(*result):
        atypes[c].name = "_".join(atype_names[r])

    for atype in atypes:
        anames[atype.name + "_id_type"] = atype.id
        anames[atype.name + "_locations"] = atype.locations

    g = globals()

    if update_globals and globals_conflict:
        for n in anames:
            if n in g:
                m = f"Globals unmodified, conflict on: '{n}'"
                raise ValueError(m)

    if update_globals:
        g.update(anames)

    return (atypes, anames)

appt_data(update_globals=True)
