from twisted.internet import protocol, defer, reactor
from twisted.python import log
from twisted.web.client import Agent, ResponseDone
from twisted.web.http import PotentialDataLoss
from twisted.words.protocols import irc

from twittytwister.twitter import Twitter
from BeautifulSoup import BeautifulSoup

import traceback
import shlex
import re

# dang why doesn't this exist anywhere already
control_equivalents = {i: unichr(0x2400 + i) for i in xrange(0x20)}
del control_equivalents[0x02]  # irc bold
control_equivalents[0x7f] = u'\u2421'

def lowQuote(s):
    # kind of gross, but this function is called in weird places, so I can't
    # really do _much_ better for trying to encode on output.
    if isinstance(s, str):
        s = s.decode('utf-8')
    return s.translate(control_equivalents).encode('utf-8')
irc.lowQuote = lowQuote

twitter_regexp = re.compile(r'twitter\.com/(?:#!/)?[^/]+/status(?:es)?/(\d+)')
default_twatter = Twitter()

class StringReceiver(protocol.Protocol):
    def __init__(self):
        self.deferred = defer.Deferred()
        self._buffer = []

    def dataReceived(self, data):
        self._buffer.append(data)

    def connectionLost(self, reason):
        if reason.check(ResponseDone, PotentialDataLoss):
            self.deferred.callback(''.join(self._buffer))
        else:
            self.deferred.errback(reason)

def receive(response, receiver):
    response.deliverBody(receiver)
    return receiver.deferred

redirectsToFollow = {301, 302, 303, 307}
@defer.inlineCallbacks
def urlInfo(agent, url, redirectFollowCount=3, fullInfo=True):
    results = [url]
    try:
        for _ in xrange(redirectFollowCount):
            resp = yield agent.request('GET', url)
            if resp.code in redirectsToFollow:
                url = resp.headers.getRawHeaders('location')[0]
                results.append('%d: %s' % (resp.code, url))
                continue
            elif resp.code == 200:
                content_type = resp.headers.getRawHeaders('content-type')[0].split(';')[0]
                result = '%d: %s' % (resp.code, content_type)
                if content_type == 'text/html':
                    doc = BeautifulSoup(
                        (yield receive(resp, StringReceiver())), convertEntities=True)
                    title_obj = doc.title
                    if title_obj:
                        title = ' '.join(title_obj.string.split())
                        if not fullInfo:
                            defer.returnValue('title: %s' % (title,))
                        result = '%s -- %s' % (result, title)
                results.append(result)
                break
            else:
                results.append(str(resp.code))
                break
    except Exception:
        log.err(None, 'error in URL info for %r' % (url,))
        results.append(traceback.format_exc(limit=0).splitlines()[-1])
    if not fullInfo:
        defer.returnValue(None)
    defer.returnValue(' => '.join(results))

urlRegex = re.compile(
    u'(?i)\\b((?:[a-z][\\w-]+:(?:/{1,3}|[a-z0-9%])|www\\d{0,3}[.]|[a-z0-9.\\-]'
    u'+[.][a-z]{2,4}/)(?:[^\\s()<>]+|\\(([^\\s()<>]+|(\\([^\\s()<>]+\\)))*\\))'
    u'+(?:\\(([^\\s()<>]+|(\\([^\\s()<>]+\\)))*\\)|[^\\s`!()\\[\\]{};:\'".,<>?'
    u'\xab\xbb\u201c\u201d\u2018\u2019]))'
)

def _extractTwatText(twat):
    rt = getattr(twat, 'retweeted_status', None)
    if rt:
        return u'RT @%s: %s' % (rt.user.screen_name, rt.text)
    else:
        return twat.text

class _IRCBase(irc.IRCClient):
    def ctcpQuery(self, user, channel, messages):
        messages = [(a.upper(), b) for a, b in messages]
        irc.IRCClient.ctcpQuery(self, user, channel, messages)

    def noticed(self, user, channel, message):
        pass

    def msg(self, user, message, length=None):
        # don't want to split messages; just encode the newlines
        irc.IRCClient.msg(self, user, lowQuote(message), length)

class TheresaProtocol(_IRCBase):
    _lastURL = None
    channel = None
    channels = None

    def __init__(self):
        self.agent = Agent(reactor)
        if self.channels is None:
            self.channels = self.channel,

    def signedOn(self):
        self.join(','.join(self.channels))
        _IRCBase.signedOn(self)

    def showURLInfo(self, channel, url, fullInfo=False):
        d = urlInfo(self.agent, url, fullInfo=fullInfo)
        @d.addCallback
        def _cb(r):
            if r is not None:
                self.msg(channel, r)
        d.addErrback(log.err)
        self._lastURL = url

    def privmsg(self, user, channel, message):
        if not channel.startswith('#'):
            return

        for m in urlRegex.finditer(message):
            url = m.group(0)
            twitter_match = twitter_regexp.search(url)
            if twitter_match:
                self.showTwat(channel, twitter_match.group(1))
            else:
                if not url.startswith(('http://', 'https://')):
                    url = 'http://' + url
                self.showURLInfo(channel, url)

        if not message.startswith((',', '!')):
            return

        splut = shlex.split(message[1:])
        command, params = splut[0], splut[1:]
        meth = getattr(self, 'command_%s' % (command.lower(),), None)
        if meth is not None:
            d = defer.maybeDeferred(meth, channel, *params)
            @d.addErrback
            def _eb(f):
                self.msg(channel, 'error in %s: %s' % (command, f.getErrorMessage()))
                return f
            d.addErrback(log.err)

    def _twatDelegate(self, channels):
        def _actualTwatDelegate(twat):
            for channel in channels:
                self.msg(
                    channel,
                    ('\x02<%s>\x02 %s' % (twat.user.screen_name, _extractTwatText(twat))))
        return _actualTwatDelegate

    def showTwat(self, channel, id):
        return self.factory.twatter.show(id, self._twatDelegate([channel]))

    def command_twat(self, channel, user):
        return self.factory.twatter.user_timeline(
            self._twatDelegate([channel]), user, params=dict(count='1', include_rts='true'))

    def command_url(self, channel, url=None):
        if url is None:
            url = self._lastURL
        self.showURLInfo(channel, url, fullInfo=True)

class TheresaFactory(protocol.ReconnectingClientFactory):
    protocol = TheresaProtocol

    def __init__(self, twatter=default_twatter):
        self.twatter = twatter
