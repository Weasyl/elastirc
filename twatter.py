"I HATE TWITTER"
from twisted.protocols.policies import TimeoutMixin
from twisted.web.http_headers import Headers
from twisted.protocols.basic import LineOnlyReceiver
from twisted.internet.error import TimeoutError
from twisted.web.client import ResponseDone
from twisted.web.http import PotentialDataLoss
from twisted.internet import defer
from twisted.python import log
from oauth import oauth

import urlparse
import theresa
import urllib
import json
import re

defaultSignature = oauth.OAuthSignatureMethod_HMAC_SHA1()
defaultTwitterAPI = 'https://api.twitter.com/1.1/'
defaultStreamingAPI = 'https://userstream.twitter.com/1.1/'

class UnexpectedHTTPStatus(Exception):
    pass

def trapBadStatuses(response, goodStatuses=(200,)):
    if response.code not in goodStatuses:
        raise UnexpectedHTTPStatus(response.code, response.phrase)
    return response

class OAuthAgent(object):
    "An Agent wrapper that adds OAuth authorization headers."
    def __init__(self, agent, consumer, token, signatureMethod=defaultSignature):
        self.agent = agent
        self.consumer = consumer
        self.token = token
        self.signatureMethod = signatureMethod

    def request(self, method, uri, headers=None, bodyProducer=None, parameters=None, addAuthHeader=True):
        """Make a request, optionally signing it.

        Any query string passed in `uri` will get clobbered by the urlencoded
        version of `parameters`.
        """
        if headers is None:
            headers = Headers()
        if parameters is None:
            parameters = {}
        if addAuthHeader:
            req = oauth.OAuthRequest.from_consumer_and_token(
                self.consumer, token=self.token,
                http_method=method, http_url=uri, parameters=parameters)
            req.sign_request(self.signatureMethod, self.consumer, self.token)
            for header, value in req.to_header().iteritems():
                headers.addRawHeader(header, value)
        parsed = urlparse.urlparse(uri)
        uri = urlparse.urlunparse(parsed._replace(query=urllib.urlencode(parameters)))
        return self.agent.request(method, uri, headers, bodyProducer)

class TwitterStream(LineOnlyReceiver, TimeoutMixin):
    "Receive a stream of JSON in twitter's weird streaming format."
    def __init__(self, delegate, timeoutPeriod=60):
        self.delegate = delegate
        self.deferred = defer.Deferred(self._cancel)
        self.setTimeout(timeoutPeriod)
        self._done = False

    def _cancel(self, ign):
        "A Deferred canceler that drops the connection."
        self._done = True
        self.transport.stopProducing()

    def dataReceived(self, data):
        "Reset the timeout and parse the received data."
        self.resetTimeout()
        LineOnlyReceiver.dataReceived(self, data)

    def lineReceived(self, line):
        "Ignoring empty-line keepalives, inform the delegate about new data."
        if not line:
            return
        obj = json.loads(line)
        try:
            self.delegate(obj)
        except:
            log.err(None, 'error in stream delegate %r' % (self.delegate,))

    def timeoutConnection(self):
        "We haven't received data in too long, so drop the connection."
        self._done = True
        self.transport.stopProducing()
        self.deferred.errback(TimeoutError())

    def connectionLost(self, reason):
        "Report back how the connection was lost unless we already did."
        self.setTimeout(None)
        if self._done:
            return
        if reason.check(ResponseDone, PotentialDataLoss):
            self.deferred.callback(None)
        else:
            self.deferred.errback(reason)

class Twatter(object):
    "Close to the most minimal twitter interface ever."
    def __init__(self, agent, twitterAPI=defaultTwitterAPI, streamingAPI=defaultStreamingAPI):
        self.agent = agent
        self.twitterAPI = twitterAPI
        self.streamingAPI = streamingAPI

    def _makeRequest(self, whichAPI, resource, parameters):
        d = self.agent.request('GET', urlparse.urljoin(whichAPI, resource), parameters=parameters)
        d.addCallback(trapBadStatuses)
        return d

    def request(self, resource, **parameters):
        """Make a GET request from the twitter 1.1 API.

        `resource` is the part of the resource URL not including the API URL,
        e.g. 'statuses/show.json'. As everything gets decoded by `json.loads`,
        this should always end in '.json'. Any parameters passed in as keyword
        arguments will be added to the URL as the query string. The `Deferred`
        returned will fire with the decoded JSON.
        """
        d = self._makeRequest(self.twitterAPI, resource, parameters)
        d.addCallback(theresa.receive, theresa.StringReceiver())
        d.addCallback(json.loads)
        return d

    def stream(self, resource, delegate, **parameters):
        """Receive from the twitter 1.1 streaming API.

        `resource` and keyword arguments are treated the same as the in
        `request`, and `delegate` will be called with each JSON object which is
        received from the stream. The `Deferred` returned will fire when the
        stream has ended.
        """
        d = self._makeRequest(self.streamingAPI, resource, parameters)
        d.addCallback(theresa.receive, TwitterStream(delegate))
        return d

entityReplacements = [
    ('media', 'media_url_https'),
    ('urls', 'expanded_url'),
]

# SERIOUSLY why the FUCK do I have to do this
dumbCrapReplacements = {
    '&amp;': '&',
    '&lt;': '<',
    '&gt;': '>',
}
dumbCrapRegexp = re.compile('|'.join(re.escape(s) for s in dumbCrapReplacements))

def extractRealTwatText(twat):
    "Oh my god why is this necessary."
    if 'retweeted_status' in twat:
        rt = twat['retweeted_status']
        return u'RT @%s: %s' % (rt['user']['screen_name'], extractRealTwatText(rt))
    replacements = sorted(
        (entity['indices'], entity[replacement])
        for entityType, replacement in entityReplacements
        if entityType in twat['entities']
        for entity in twat['entities'][entityType])
    mutableText = list(twat['text'])
    for (l, r), replacement in reversed(replacements):
        mutableText[l:r] = replacement
    text = u''.join(mutableText)
    return dumbCrapRegexp.sub(lambda m: dumbCrapReplacements[m.group()], text)
