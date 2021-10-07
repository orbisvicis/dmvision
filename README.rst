========
DMVision
========

The year is 2021 and basic social services in NJ (US) are as scarce as GPUs
worldwide. Enter DMVision, your personal appointment secretary for the NJ DMV
system.

The Example
===========

The example walks you through configuring the components and running the
server.

.. code:: python

    import datetime

    import dmvision
    import dateutil.parser


    # Select some location IDs described in the module.
    # Each list should be in order of decreasing priority.
    selected_ids =\
        { "permit": [123, 134, 145]
        , "knowledge": [223, 234, 245]
        , "nondriverid": [323, 334, 345]
        , "cdl": [423, 434, 445]
        }

    # Provide some personal information required to take an appointment.
    # The lambda below filters appointments that aren't some time between
    # 4 hours and 4 days from now. You can use the 'check_calendar' hook
    # to synchronize with, for example, your Google Calendar.
    appt_info = dmvision.AppointmentInfo\
        ( first_name = "John"
        , last_name = "Doe"
        , email = "test@test.test"
        , phone = "1234567890"
        , birth_date = "12/23/2001"
        , driver_license = "D1234" "12345" "12345"
        , check_calendar = lambda dt: dmvision.datetime_within
            ( dt
            , datetime.datetime.today() + datetime.timedelta(hours=4)
            , datetime.date.today() + datetime.timedelta(days=4)
            )
        )

    # Provide your Twilio account details,
    # required for using the Twilio service.
    twilio_info = dmvision.TwilioInfo\
        ( account_sid = "ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
        , auth_token = "12345"
        , from_ = "+11234567890"
        , to = "+11234567890"
        )

    # Provide enough information to launch the Mozilla
    # geckodriver and submit the appointment form.
    selenium_info = dmvision.SeleniumInfo\
        ( geckodriver_path = "/path/to/geckodriver"
        , autofill = dmvision.knowledge_autofill
        )

    # Configure the service as required though the
    # defaults should suffice and have been omitted.
    other_info = dmvision.OtherInfo()

    # Configure the web application. You will want to configure this
    # server (or a reverse proxy to this server such as ngrok) as the
    # webhook for your Twilio number.
    server_info = dmvision.ServerInfo\
        ( username = "test"
        , password = "test"
        )

    # Fine-tune the notifications. For example, drop standard
    # appointment text messages between midnight and 3 AM, and
    # rate-limit the remainder.
    notification_manager = dmvision.NotificationManager().register\
        ( resource = dmvision.NotificationResource.TextMessage
        , category = dmvision.NotificationCategory.Appointment
        , priority = dmvision.NotificationPriority.Standard
        , handlers =
          [ dmvision.Timelock
            ( start = dateutil.parser.parse("00:00").time()
            , stop = dateutil.parser.parse("03:00").time()
            , notifier = dmvision.make_timelock_notify(twilio_info)
            )
          , dmvision.Ratelimit
            ( capacity=20, rate=0.02
            , notifier=dmvision.ratelimit_notify
            )
          ]
        )

    # Finally, run the server.
    dmvision.run\
        ( selected_ids["knowledge"]
        , dmvision.knowledge_id_type
        , dmvision.knowledge_locations
        , appt_info, twilio_info, selenium_info, server_info, other_info
        , notification_manager
        )

Post-mortem
===========

While writing both threaded and async code was an interesting experience, I
should've switched to an async requests library such as the aiohttp client
library. That would have allowed merging the main and async threads, removing
nearly all synchronization primitives and simplifying the codebase. The sound
thread would have remained but any selenium calls would have been offloaded to
an executor.

I should've switched from text messages to a free chat protocol with a mobile
presence, such as Matrix via the async matrix-nio library. While text messages
are ubiquitous they are slow, lack formatting, expensive, and do not guarantee
ordering. Furthermore the Twilio library requires a public-facing webhook
server or use of a reverse proxy service such as ngrok. The Matrix libraries
represent a client interface. Though they don't need a web server the code
would be a drop-in replacement for my aiohttp server.

That said, the current codebase is performant and feature-complete.

Requirements
============

* Python: 3.9+
* module: `dateutil` (`@PyPI`__)
* module: `requests` (`@PyPI`__)
* module: `selenium` (`@PyPI`__)
* module: `aiohttp_basicauth` (`@PyPI`__)
* module: `aiohttp` (`@PyPI`__)
* module: `pyaudio` (`@PyPI`__)
* module: `twilio` (`@PyPI`__)

__ dateutil_PyPI_
__ requests_PyPI_
__ selenium_PyPI_
__ aiohttp_basicauth_PyPI_
__ aiohttp_PyPI_
__ pyaudio_PyPI_
__ twilio_PyPI_

Author
======

Yclept Nemo <pscjtwjdjtAhnbjm/dpn>

License
=======

GPLv3+; see `LICENSE.txt`

Links
=====

* `DMVision@GitHub`__
* `DMVision@PyPI`__

__ DMVision_GitHub_
__ DMVision_PyPI_


.. _dateutil_PyPI:          https://pypi.org/project/python-dateutil/
.. _requests_PyPI:          https://pypi.org/project/requests/
.. _selenium_PyPI:          https://pypi.org/project/selenium/
.. _aiohttp_basicauth_PyPI: https://pypi.org/project/aiohttp-basicauth/
.. _aiohttp_PyPI:           https://pypi.org/project/aiohttp/
.. _pyaudio_PyPI:           https://pypi.org/project/PyAudio/
.. _twilio_PyPI:            https://pypi.org/project/twilio/

.. _DMVision_PyPI:          https://pypi.org/project/dmvision/
.. _DMVision_GitHub:        https://github.com/orbisvicis/dmvision
