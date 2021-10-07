#!/usr/bin/env python3

# Back when I had issues with crackling audio I created this script to test the
# performance of various grouper implementations. The first two ('grouper1' and
# 'grouper2') win by a wide marge. On python 3.8.10 'grouper1' is slightly
# faster, but 'grouper2' has the edge on python 3.9.7.

import timeit
import itertools


setup = """
n = 48000
data = range(n*20)
"""

code = """
for i in grouper{}(data, n):
    for j in i:
        pass
"""

def grouper1(i, n):
    i = iter(i)
    return iter(lambda: list(itertools.islice(i, n)), [])

def grouper2(i, n):
    i = iter(i)
    return iter(lambda: tuple(itertools.islice(i, n)), ())

def grouper3(i, n):
    i = iter(i)
    s = False

    def inner():
        nonlocal s
        for _ in range(n):
            try:
                yield next(i)
            except StopIteration:
                s = True
                return

    while not s:
        yield inner()

# From: https://stackoverflow.com/q/24527006/2626865
def grouper4(iterable, size=10):
    iterator = iter(iterable)
    for first in iterator:
        def chunk():
            yield first
            for more in itertools.islice(iterator, size - 1):
                yield more
        yield chunk()


for i in range(1,5):
    r = timeit.timeit(code.format(i), setup=setup, globals=globals(), number=100)
    print("grouper{}: {}".format(i,r))
