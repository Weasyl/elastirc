from twisted.application import service
from twisted.application.internet import TimerService
from twisted.internet import task, protocol, defer, reactor, error
from twisted.python import log
from twisted.web.client import Agent, ResponseDone
from twisted.web.http_headers import Headers
from twisted.words.protocols import irc

from lxml import etree
from twittytwister.twitter import Twitter

import shlex
import re

twitter_regexp = re.compile(r'twitter\.com/#!/[^/]+/status/(\d+)')

twatter = Twitter()

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
        streamer = LxmlStreamReceiver()
        resp.deliverBody(streamer)
        doc = yield streamer.deferred
        prev = None
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
                prev = link, title
        self._lastLink, = doc.xpath('/rss/channel/item[1]/link/text()')
        newLink, newTitle = prev
        _, _, newTitle = newTitle.partition(' : ')
        log.msg('new MSPA: %r' % (newTitle,))
        targetClient = yield self.target.clientDeferred()
        targetClient.newMSPA(newLink, newTitle)

class TheresaProtocol(irc.IRCClient):
    outstandingPings = 0
    _pinger = None

    def _serverPing(self):
        if self.outstandingPings > 5:
            self.loseConnection()
        self.sendLine('PING bollocks')
        self.outstandingPings += 1

    def irc_PONG(self, prefix, params):
        self.outstandingPings -= 1

    def signedOn(self):
        self.factory.established(self)
        self.msg('nickserv', 'identify %s' % self.nickserv_pw)
        self.join(self.channel)
        self._pinger = task.LoopingCall(self._serverPing)
        self._pinger.start(60)

    def connectionLost(self, reason):
        irc.IRCClient.connectionLost(self, reason)
        self.factory.unestablished()
        if self._pinger is not None:
            self._pinger.stop()

    def ctcpQuery(self, user, channel, messages):
        messages = [(a.upper(), b) for a, b in messages]
        irc.IRCClient.ctcpQuery(self, user, channel, messages)

    def newMSPA(self, link, title):
        self.msg(self.channel, '%s (%s)' % (title, link))

    def noticed(self, user, channel, message):
        pass

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
            ('<%s> %s' % (twat.user.screen_name, twat.text)).encode('utf-8'))

    def showTwat(self, channel, id):
        return twatter.show(id, self._twatDelegate(channel))

    def command_twat(self, channel, user):
        return twatter.user_timeline(self._twatDelegate(channel), user,
                                     params=dict(count='1', include_rts='true'))

class TheresaFactory(protocol.ReconnectingClientFactory):
    protocol = TheresaProtocol

    def __init__(self):
        self._clientDeferred = defer.Deferred()
        self._client = None

    def established(self, protocol):
        self._client = protocol
        self._clientDeferred.callback(protocol)
        self.resetDelay()

    def unestablished(self):
        self._client = None
        self._clientDeferred = defer.Deferred()

    def clientDeferred(self):
        if self._client is not None:
            return defer.succeed(self._client)
        d = defer.Deferred()
        self._clientDeferred.chainDeferred(d)
        return d
