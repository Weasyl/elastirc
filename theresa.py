from twisted.internet.error import ConnectionDone, ConnectionLost
from twisted.internet import protocol, defer
from twisted.python import log
from twisted.web.client import ResponseDone, ResponseFailed
from twisted.web.http import PotentialDataLoss
from twisted.words.protocols import irc

from lxml import html

import collections
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
        if ((reason.check(ResponseFailed) and any(exn.check(ConnectionDone, ConnectionLost)
                                                  for exn in reason.value.reasons))
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

    def signedOn(self):
        self.channelUsers = collections.defaultdict(set)
        self.nickPrefixes = ''.join(prefix for prefix, _ in self.supported.getFeature('PREFIX').itervalues())

    def irc_RPL_NAMREPLY(self, prefix, params):
        channel = params[2].lower()
        self.channelUsers[channel].update(nick.lstrip(self.nickPrefixes) for nick in params[3].split(' '))

    def userJoined(self, user, channel):
        nick, _, host = user.partition('!')
        self.channelUsers[channel.lower()].add(nick)

    def userLeft(self, user, channel):
        nick, _, host = user.partition('!')
        self.channelUsers[channel.lower()].discard(nick)

    def userQuit(self, user, quitMessage):
        nick, _, host = user.partition('!')
        for users in self.channelUsers.itervalues():
            users.discard(nick)

    def userKicked(self, kickee, channel, kicker, message):
        nick, _, host = kickee.partition('!')
        self.channelUsers[channel.lower()].discard(nick)

    def userRenamed(self, oldname, newname):
        for users in self.channelUsers.itervalues():
            if oldname in users:
                users.discard(oldname)
                users.add(newname)

class TheresaProtocol(_IRCBase):
    _lastURL = None
    channel = None
    channels = None

    sourceURL = 'https://github.com/habnabit/theresa-bot'
    versionName = 'theresa'
    versionNum = 'HEAD'
    versionEnv = 'twisted'

    def __init__(self):
        if self.channels is None:
            self.channels = self.channel,

    def signedOn(self):
        self.join(','.join(self.channels))
        _IRCBase.signedOn(self)

    def formatTwat(self, twat):
        return ' '.join([
                c(' Twitter ', WHITE, CYAN),
                b('@%s:' % (escapeControls(twat['user']['screen_name']),)),
                escapeControls(twatter.extractRealTwatText(twat))])

    def fetchFormattedTwat(self, channel, id):
        (self.factory.twatter
         .request('statuses/show.json', id=id, include_entities='true')
         .addCallback(self.formatTwat)
         .addCallback(self.messageChannels, [channel]))

    def fetchURLInfo(self, channel, url, fullInfo=False):
        d = urlInfo(self.factory.agent, url, fullInfo=fullInfo)
        @d.addCallback
        def _cb(r):
            if r is not None:
                return c(' Page title ', WHITE, NAVY) + ' ' + b(escapeControls(r))
        self._lastURL = url
        return d

    def scanMessage(self, channel, message):
        scannedDeferreds = []
        for m in urlRegex.finditer(message):
            url = m.group(0)
            twitter_match = twitter_regexp.search(url)
            if twitter_match:
                scannedDeferreds.append(self.fetchFormattedTwat(channel, twitter_match.group(1)))
            else:
                if not url.startswith(('http://', 'https://')):
                    url = 'http://' + url
                scannedDeferreds.append(self.fetchURLInfo(channel, url))
        if not scannedDeferreds:
            return
        d = defer.gatherResults(scannedDeferreds, consumeErrors=True)
        @d.addCallback
        def _cb(results):
            result = u' \xa6 '.encode('utf-8').join(result for result in results if result is not None)
            if result is not None:
                self.msg(channel, result)
        return d

    def privmsg(self, user, channel, message):
        if not channel.startswith('#'):
            return

        if not message.startswith((',', '!')):
            defer.maybeDeferred(self.scanMessage, channel, message).addErrback(log.err)
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

    def messageChannels(self, message, channels):
        for channel in channels:
            self.msg(channel, message)

    def command_twat(self, channel, user):
        return (self.factory.twatter
                .request('statuses/user_timeline.json',
                         screen_name=user, count='1', include_rts='true', include_entities='true')
                .addCallback(operator.itemgetter(0))
                .addCallback(self.formatTwat)
                .addCallback(self.messageChannels, [channel]))

    def command_url(self, channel, url=None):
        if url is None:
            url = self._lastURL
        return (self.fetchURLInfo(channel, url, fullInfo=True)
                .addCallback(self.messageChannels, [channel]))

class TheresaFactory(protocol.ReconnectingClientFactory):
    protocol = TheresaProtocol

    def __init__(self, agent, twatter):
        self.agent = agent
        self.twatter = twatter
