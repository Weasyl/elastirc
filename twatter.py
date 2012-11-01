"I HATE TWITTER"
from twisted.web.http_headers import Headers
from oauth import oauth

import urlparse
import theresa
import urllib
import json
import re

defaultSignature = oauth.OAuthSignatureMethod_HMAC_SHA1()
defaultAPI = 'https://api.twitter.com/1.1/'

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

class Twatter(object):
    "Close to the most minimal twitter interface ever."
    def __init__(self, agent, twitterAPI=defaultAPI):
        self.agent = agent
        self.twitterAPI = twitterAPI

    def request(self, resource, **parameters):
        """Make a GET request from the twitter 1.1 API.

        `resource` is the part of the resource URL not including the API URL,
        e.g. 'statuses/show.json'. As everything gets decoded by `json.loads`,
        this should always end in '.json'. Any parameters passed in as keyword
        arguments will be added to the URL as the query string.
        """
        d = self.agent.request('GET', urlparse.urljoin(self.twitterAPI, resource), parameters=parameters)
        d.addCallback(theresa.receive, theresa.StringReceiver())
        d.addCallback(json.loads)
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
