"twisted.cred integration for Weasyl."

from zope.interface import implementer

from twisted.cred.checkers import ICredentialsChecker
from twisted.cred.credentials import IUsernamePassword
from twisted.cred.error import UnauthorizedLogin
from twisted.internet import defer, protocol
from twisted.internet.error import ConnectionDone, ConnectionLost
from twisted.web.client import ResponseDone, ResponseFailed
from twisted.web.http import PotentialDataLoss
from twisted.web.http_headers import Headers

import json
import time


class StringReceiver(protocol.Protocol):
    "Collects all of the bytes recieved by a Protocol into a string."

    def __init__(self, byteLimit=None):
        self.bytesRemaining = byteLimit
        self.deferred = defer.Deferred()
        self._buffer = []

    def dataReceived(self, data):
        data = data[:self.bytesRemaining]
        self._buffer.append(data)
        if self.bytesRemaining is not None:
            self.bytesRemaining -= len(data)
            if not self.bytesRemaining:
                self.transport.stopProducing()

    def connectionLost(self, reason):
        if ((reason.check(ResponseFailed) and any(exn.check(ConnectionDone, ConnectionLost)
                                                  for exn in reason.value.reasons))
                or reason.check(ResponseDone, PotentialDataLoss)):
            self.deferred.callback(''.join(self._buffer))
        else:
            self.deferred.errback(reason)

def receive(response, receiver):
    response.deliverBody(receiver)
    return receiver.deferred


class WeirdHTTPStatusError(Exception):
    pass

@implementer(ICredentialsChecker)
class WeasylAPIChecker(object):
    """An ICredentialsChecker implementation that queries the Weasyl API.

    The `agent` parameter is a twisted.web.client Agent used to make the HTTP
    request to Weasyl. This checker only works on IUsernamePassword, where the
    password is a Weasyl API key and the username is must match the owner of
    the API key.

    The `weasylInfoAPI` attribute is the URL to the Weasyl whoami API, which
    returns a json blob of the API user's login name and userid.
    """

    credentialInterfaces = IUsernamePassword,
    weasylInfoAPI = 'https://www.weasyl.com/api/whoami'

    def __init__(self, agent, cacheLength=None):
        self.agent = agent
        self.cacheLength = cacheLength
        self._cache = {}
        self._fetching = {}

    def requestAvatarId(self, credentials):
        """Authenticate a user against the Weasyl API.

        Expects an IUsernamePassword implementer where the password is a Weasyl
        API key and the username is the owner of the API key. Returns a
        deferred that will fire with the login name as returned by the API if
        it matches the provided username. The deferred will errback with
        UnauthorizedLogin if the username doesn't match or Weasyl didn't
        recognize the API key, or with WeirdHTTPStatusError if Weasyl's API
        returned an unexpected code.
        """

        credKey = credentials.username, credentials.password
        if credKey in self._cache and self.cacheLength is not None:
            cachedAt, value = self._cache[credKey]
            if cachedAt + self.cacheLength > time.time():
                return defer.succeed(value)
            del self._cache[credKey]

        d = defer.Deferred()
        if credKey in self._fetching:
            self._fetching[credKey].append(d)
        else:
            self._requestAvatarIdFromWeasyl(credentials, credKey)
            self._fetching[credKey] = [d]
        return d

    def _requestAvatarIdFromWeasyl(self, credentials, credKey):
        headers = Headers()
        headers.addRawHeader('x-weasyl-api-key', credentials.password)
        d = self.agent.request('GET', self.weasylInfoAPI, headers)
        d.addCallback(self._trapBadStatuses)
        d.addCallback(receive, StringReceiver())
        d.addCallback(json.loads)
        d.addCallback(self._verifyUsername, credentials.username)
        d.addBoth(self._gotResult, credKey)

    def _trapBadStatuses(self, response):
        if response.code == 403:
            raise UnauthorizedLogin()
        elif response.code != 200:
            raise WeirdHTTPStatusError(response.code, response.phrase)
        return response

    def _verifyUsername(self, userinfo, username):
        login = userinfo['login'].encode('utf-8')
        if login != username.lower():
            raise UnauthorizedLogin()
        return login

    def _gotResult(self, result, credKey):
        if self.cacheLength is not None:
            self._cache[credKey] = time.time(), result
        for d in self._fetching.pop(credKey):
            d.callback(result)
