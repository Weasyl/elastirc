from twisted.application import service
from twisted.application.internet import TimerService
from twisted.internet import task, protocol, defer, reactor, error
from twisted.python import log
from twisted.web.client import Agent, ResponseDone
from twisted.web.http import PotentialDataLoss
from twisted.web.http_headers import Headers
from twisted.words.protocols import irc

from lxml import etree, html
from twittytwister.twitter import Twitter

import collections
import random
import shlex
import re

def isWord(word, _pat=re.compile(r"[a-zA-Z']+$")):
    return _pat.match(word) is not None

twitter_regexp = re.compile(r'twitter\.com/(?:#!/)?[^/]+/status(?:es)?/(\d+)')
torrent_regexp = re.compile(r'-> (\S+) .*details\.php\?id=(\d+)')

default_twatter = Twitter()

class LxmlStreamReceiver(protocol.Protocol):
    def __init__(self):
        self.deferred = defer.Deferred()
        self._parser = etree.XMLParser()

    def dataReceived(self, data):
        self._parser.feed(data)

    def connectionLost(self, reason):
        if reason.check(ResponseDone):
            self.deferred.callback(self._parser.close())
        else:
            self.deferred.errback(reason)

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

def paragraphCount(l):
    return sum(1 for x in l if x.strip())

@defer.inlineCallbacks
def mspaCounts(agent, urls):
    ret = collections.Counter()
    for url in urls:
        resp = yield agent.request('GET', url)
        doc = html.fromstring((yield receive(resp, StringReceiver())))
        ret['pesterlines'] += paragraphCount(doc.xpath('//div[@class="spoiler"]//p//text()'))
        ret['paragraphs'] += paragraphCount(doc.xpath('//td[@bgcolor="#EEEEEE"]//center/p/text()'))
        ret['images'] += sum(1 for img in doc.xpath('//td[@bgcolor="#EEEEEE"]//img/@src') if 'storyfiles' in img)
        ret['flashes'] += sum(1 for src in doc.xpath('//td[@bgcolor="#EEEEEE"]//script/@src') if 'storyfiles' in src)
        ret['pages'] += 1

    defer.returnValue(ret)

class MSPAChecker(service.MultiService):
    def __init__(self, target):
        service.MultiService.__init__(self)
        self.target = target
        self.agent = Agent(reactor)
        self.timer = TimerService(120, self._doPoll)
        self.timer.setServiceParent(self)
        self._lastModified = self._lastLink = None

    def _doPoll(self):
        d = self._actuallyDoPoll()
        d.addErrback(log.err)
        return d

    @defer.inlineCallbacks
    def _actuallyDoPoll(self):
        headers = Headers()
        if self._lastModified is not None:
            headers.addRawHeader('If-Modified-Since', self._lastModified)
        try:
            resp = yield self.agent.request(
                'GET', 'http://www.mspaintadventures.com/rss/rss.xml',
                headers)
        except error.TimeoutError:
            log.msg('timeout requesting MSPA')
            return
        if resp.code == 304:
            return
        elif resp.code != 200:
            log.msg('strange HTTP code from MSPA: %r' % (resp.code,))
            return
        if resp.headers.hasHeader('Last-Modified'):
            self._lastModified, = resp.headers.getRawHeaders('Last-Modified')
        doc = yield receive(resp, LxmlStreamReceiver())
        prev = None
        newUrls = []
        for item in doc.xpath('/rss/channel/item'):
            link, = item.xpath('link/text()')
            title, = item.xpath('title/text()')
            if self._lastLink is None:
                self._lastLink = link
                return
            elif self._lastLink == link:
                if prev is None:
                    return
                break
            else:
                newUrls.append(link)
                prev = link, title
        self._lastLink, = doc.xpath('/rss/channel/item[1]/link/text()')
        newLink, newTitle = prev
        _, _, newTitle = newTitle.partition(' : ')
        log.msg('new MSPA: %r' % (newTitle,))
        targetClient = yield self.target.clientDeferred()
        targetClient.newMSPA(newLink, newTitle)
        counts = yield mspaCounts(self.agent, newUrls)
        targetClient.newMSPACounts(counts)

def _extractTwatText(twat):
    rt = getattr(twat, 'retweeted_status', None)
    if rt:
        return u'RT @%s: %s' % (rt.user.screen_name, rt.text)
    else:
        return twat.text

class _IRCBase(irc.IRCClient):
    outstandingPings = 0
    _pinger = None
    nickserv_pw = None

    def _serverPing(self):
        if self.outstandingPings > 5:
            self.transport.loseConnection()
        self.sendLine('PING bollocks')
        self.outstandingPings += 1

    def irc_PONG(self, prefix, params):
        self.outstandingPings -= 1

    def signedOn(self):
        self._pinger = task.LoopingCall(self._serverPing)
        self._pinger.start(60)
        if self.nickserv_pw is not None:
            self.msg('nickserv', 'identify %s' % self.nickserv_pw)

    def connectionLost(self, reason):
        irc.IRCClient.connectionLost(self, reason)
        if self._pinger is not None:
            self._pinger.stop()

    def ctcpQuery(self, user, channel, messages):
        messages = [(a.upper(), b) for a, b in messages]
        irc.IRCClient.ctcpQuery(self, user, channel, messages)

    def noticed(self, user, channel, message):
        pass

class TheresaProtocol(_IRCBase):
    _buttReady = True
    warnMessage = None

    def signedOn(self):
        self.join(self.channel)
        _IRCBase.signedOn(self)
        self.factory.established(self)

    def connectionLost(self, reason):
        _IRCBase.connectionLost(self, reason)
        self.factory.unestablished()

    def newMSPA(self, link, title):
        self.msg(self.channel, '%s (%s)' % (title, link))

    def newMSPACounts(self, counts):
        self.msg(self.channel, 'new: %s' % '; '.join('%s %s' % (v, k) for k, v in counts.iteritems() if v))

    def privmsg(self, user, channel, message):
        if not channel.startswith('#'):
            return

        for m in twitter_regexp.finditer(message):
            self.showTwat(channel, m.group(1))

        if not message.startswith(','):
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

    def _twatDelegate(self, channel):
        return lambda twat: self.msg(
            channel,
            ('\x02<%s>\x02 %s' % (twat.user.screen_name, _extractTwatText(twat))).encode('utf-8'))

    def showTwat(self, channel, id):
        return self.factory.twatter.show(id, self._twatDelegate(channel))

    def command_twat(self, channel, user):
        return self.factory.twatter.user_timeline(
            self._twatDelegate(channel), user, params=dict(count='1', include_rts='true'))

    def warn(self):
        if self.warnMessage:
            self.msg(self.channel, self.warnMessage)

class TheresaFactory(protocol.ReconnectingClientFactory):
    protocol = TheresaProtocol

    def __init__(self, twatter=default_twatter):
        self.twatter = twatter
        self._client = None
        self._waiting = []

    def established(self, protocol):
        self._client = protocol
        for d in self._waiting:
            d.callback(protocol)
        self.resetDelay()

    def unestablished(self):
        self._client = None
        self._clientDeferred = defer.Deferred()

    def clientDeferred(self):
        if self._client is not None:
            return defer.succeed(self._client)
        d = defer.Deferred()
        self._waiting.append(d)
        return d

class TheresaSceneProtocol(_IRCBase):
    apiKey = None
    warningInterval = 20 * 60
    warningCall = None

    def signedOn(self):
        _IRCBase.signedOn(self)
        self.factory.resetDelay()
        self._resetWarning()

    def _resetWarning(self):
        if self.warningCall is None:
            self.warningCall = reactor.callLater(self.warningInterval, self._warn)
        else:
            self.warningCall.reset(self.warningInterval)

    def _warn(self):
        d = self.factory.target.clientDeferred()
        d.addCallback(lambda p: p.warn())
        d.addErrback(log.err)
        self.warningCall = None

    def isRelevant(self, name):
        return False

    def startDownload(self, url):
        return defer.succeed(None)

    def privmsg(self, user, channel, message):
        if channel != '#announce':
            return

        m = torrent_regexp.search(message)
        if not m:
            return
        self._resetWarning()
        name, id = m.groups()
        if not self.isRelevant(name):
            return
        url = 'http://sceneaccess.org/download/%s/%s/%s.torrent' % (id, self.apiKey, name)
        self.startDownload(url).addErrback(log.err)

class TheresaSceneFactory(protocol.ReconnectingClientFactory):
    protocol = TheresaSceneProtocol

    def __init__(self, target):
        self.target = target

