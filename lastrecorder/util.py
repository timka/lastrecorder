import logging
import time
import urllib2
try:
    from hashlib import md5
except ImportError:
    from md5 import md5

class BackoffDelay(object):
    def __init__(self, mult=4):
        self.mult = mult
        self.count = 0

    def sleep(self):
        seconds = self.mult * self.count * self.count
        log = logging.getLogger('BackoffDelay')
        log.info('Sleeping %s seconds', seconds)
        time.sleep(seconds)
        self.count += 1

    def reset(self):
        self.count = 0


def quote_url(url):
    q = urllib2.quote
    i = len('lastfm:')
    return url[:i] + q(url[i:])

__all__ = ['quote_url', 'BackoffDelay', 'md5']
