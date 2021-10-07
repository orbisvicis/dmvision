#!/usr/bin/env python3

# Script used to investigate a SIGINT issue with selenium on Windows and
# Cygwin but not Linux. The communication between selenium and the webdriver
# is interrupted so that 'quit' hangs and the webdriver may not be closed.
# This is evident when DMVision handles a keyboard interrupt on affected
# platforms. The issue is still open at:
#
# https://github.com/SeleniumHQ/selenium/issues/9835

import time
import signal
import datetime
import cProfile
import pstats
import io
import sys

import selenium.webdriver

from selenium.webdriver.firefox.options import Options


p = cProfile.Profile()

opts = Options()
opts.log.level = "trace"

driver_options = {"options": opts}

try:
    driver_options["executable_path"] = sys.argv[1]
except IndexError:
    # Let selenium search PATH
    pass

driver = selenium.webdriver.Firefox(**driver_options)


def log(s):
    print\
        ( "{}: {}".format(datetime.datetime.now().isoformat(" ", "seconds"), s)
        , file=sys.stderr
        )

def log_stats():
    s = io.StringIO()
    #sb = pstats.SortKey.CUMULATIVE
    sb = pstats.SortKey.TIME
    ps = pstats.Stats(p, stream=s).sort_stats(sb)
    #ps.print_stats(0.1)
    ps.print_stats()
    print(s.getvalue())

try:
    log("Hello")
    time.sleep(2)
    log("Goodbye")
    # No effect:
    #raise KeyboardInterrupt
except:
    log("caught exception, reraising")
    raise
finally:
    # Shows default signal handler:
    #log(id(signal.getsignal(signal.SIGINT)))
    log("driver quiting")
    p.enable()
    driver.quit()
    p.disable()
    log("driver quit")
    log_stats()
