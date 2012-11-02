from twisted.internet.error import ConnectionDone
from twisted.internet import protocol, defer, reactor
from twisted.python import log
from twisted.web.client import Agent, ResponseDone, ResponseFailed
from twisted.web.http import PotentialDataLoss
from twisted.words.protocols import irc

from lxml import html

import traceback
import operator
import twatter
import shlex
import cgi
import re

# dang why doesn't this exist anywhere already
controlEquivalents = dict((i, unichr(0x2400 + i)) for i in xrange(0x20))
controlEquivalents[0x7f] = u'\u2421'
def escapeControls(s):
    return unicode(s).translate(controlEquivalents).encode('utf-8')

def b(text):
    return '\x02%s\x02' % (text,)
def c(text, *colors):
    return '\x03%s%s\x03' % (','.join(colors), text)
(WHITE, BLACK, NAVY, GREEN, RED, BROWN, PURPLE, ORANGE, YELLOW, LME, TEAL,
 CYAN, VLUE, PINK, GREY, SILVER) = (str(i) for i in range(16))

twitter_regexp = re.compile(r'twitter\.com/(?:#!/)?[^/]+/status(?:es)?/(\d+)')

class StringReceiver(protocol.Protocol):
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
        if ((reason.check(ResponseFailed) and any(exn.check(ConnectionDone) for exn in reason.value.reasons))
                or reason.check(ResponseDone, PotentialDataLoss)):
            self.deferred.callback(''.join(self._buffer))
        else:
            self.deferred.errback(reason)

def receive(response, receiver):
    response.deliverBody(receiver)
    return receiver.deferred

redirectsToFollow = set((301, 302, 303, 307))
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
                content_type, params = cgi.parse_header(resp.headers.getRawHeaders('content-type')[0])
                result = '%d: %s' % (resp.code, content_type)
                if content_type == 'text/html':
                    body = yield receive(resp, StringReceiver(4096))
                    if 'charset' in params:
                        body = body.decode(params['charset'].strip('"\''), 'replace')
                    doc = html.fromstring(body)
                    title_nodes = doc.xpath('//title/text()')
                    if title_nodes:
                        title = ' '.join(title_nodes[0].split())
                        if not fullInfo:
                            defer.returnValue(title)
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

class _IRCBase(irc.IRCClient):
    def ctcpQuery(self, user, channel, messages):
        messages = [(a.upper(), b) for a, b in messages]
        irc.IRCClient.ctcpQuery(self, user, channel, messages)

    def noticed(self, user, channel, message):
        pass

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
                self.msg(channel, c(' Page title ', WHITE, NAVY) + ' ' + b(escapeControls(r)))
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
                self.msg(channel, '%s in %s: %s' % (c(' Error ', YELLOW, RED), command, f.getErrorMessage()))
                return f
            d.addErrback(log.err)

    def twatDelegate(self, twat, channels):
        message = ' '.join([
                c(' Twitter ', WHITE, CYAN),
                b('@%s:' % (escapeControls(twat['user']['screen_name']),)),
                escapeControls(twatter.extractRealTwatText(twat))])
        for channel in channels:
            self.msg(channel, message)

    def showTwat(self, channel, id):
        (self.factory.twatter
         .request('statuses/show.json', id=id, include_entities='true')
         .addCallback(self.twatDelegate, [channel]))

    def command_twat(self, channel, user):
        return (self.factory.twatter
                .request('statuses/user_timeline.json',
                         screen_name=user, count='1', include_rts='true', include_entities='true')
                .addCallback(operator.itemgetter(0))
                .addCallback(self.twatDelegate, [channel]))

    def command_url(self, channel, url=None):
        if url is None:
            url = self._lastURL
        self.showURLInfo(channel, url, fullInfo=True)

class TheresaFactory(protocol.ReconnectingClientFactory):
    protocol = TheresaProtocol

    def __init__(self, twatter):
        self.twatter = twatter
