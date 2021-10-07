#!/usr/bin/env python3

# Source    : modified from the twilio tutorial with argparse
# License   : undefined but probably MIT like the rest of Twilio's Python code


import argparse
import urllib
import os

import requests

# Download the twilio-python library from:
# twilio.com/docs/python/install
from twilio.request_validator import RequestValidator
from requests.auth import HTTPDigestAuth
from requests.auth import HTTPBasicAuth


class AuthAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, self.action(*values))

    @classmethod
    def with_action(cls, action):
        def inner(*args, **kwargs):
            self = cls(*args, **kwargs)
            self.action = action
            return self
        return inner


parser = argparse.ArgumentParser\
    (description="Test the validity of the Twilio webhook signature")
parser.add_argument("token")
parser.add_argument("url")
group = parser.add_mutually_exclusive_group()
group.add_argument\
    ( "--basicauth", "-b"
    , metavar=("u","p"), nargs=2, dest="auth"
    , action=AuthAction.with_action(HTTPBasicAuth)
    )
group.add_argument\
    ( "--digestauth", "-d"
    , metavar=("u","p"), nargs=2, dest="auth"
    , action=AuthAction.with_action(HTTPDigestAuth)
    )
args = parser.parse_args()


# Your Auth Token from twilio.com/user/account as a command-line argument (not
# as secure as an environment variable). Remember never to hard code your auth
# token in code, browser Javascript, or distribute it in mobile apps
#auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
validator = RequestValidator(args.token)

# User credentials if required by your web server.
# Change to "HTTPBasicAuth" if needed
auth = args.auth
# Replace this URL with your unique URL
url = args.url

params = {
    "CallSid": "CA1234567890ABCDE",
    "Caller": "+12349013030",
    "Digits": "1234",
    "From": "+12349013030",
    "To": "+18005551212"
}

def test_url(method, url, params, valid):
    if method == "GET":
        url = url + "?" + urllib.parse.urlencode(params)
        params = {}

    if valid:
        signature = validator.compute_signature(url, params)
    else:
        signature = validator.compute_signature("http://invalid.com", params)

    headers = {"X-Twilio-Signature": signature}
    response = requests.request\
        ( method, url
        , headers=headers, data=params, auth=auth
        )
    print("HTTP {} with {} signature returned {}".format
        ( method
        , "valid" if valid else "invalid"
        , response.status_code
        ))


test_url("GET", url, params, True)
test_url("GET", url, params, False)
test_url("POST", url, params, True)
test_url("POST", url, params, False)
