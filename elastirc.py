# Copyright (c) Aaron Gallagher <_@habnab.it>; Weasyl LLC
# See COPYING for details.

from twisted.internet import protocol, defer
from twisted.python.filepath import FilePath
from twisted.python.logfile import DailyLogFile
from twisted.python import log
from twisted.web.resource import Resource
from twisted.web import template
from twisted.words.protocols import irc
from txes.elasticsearch import ElasticSearch

from dateutil.parser import parse as parseTimestamp

import collections
import datetime
import re
import tempfile


ircFormattingCruftRegexp = re.compile('\x03[0-9]{1,2}(?:,[0-9]{1,2})?|[\x00-\x08\x0A-\x1F]')
def fixupMessage(message):
    return ircFormattingCruftRegexp.sub('', message).decode('utf-8', 'replace')

DATE_FORMAT = '%F'
TIME_FORMAT = '%T'


class NiceBulkingElasticSearch(ElasticSearch):
    failedBulkDataDirectory = None

    def _logFailedBulkData(self, reason, data):
        now = datetime.datetime.now()
        with tempfile.NamedTemporaryFile(prefix=now.isoformat() + '_',
                                         dir=self.failedBulkDataDirectory,
                                         delete=False) as outfile:
            outfile.write('\n'.join(data))
        log.err(reason, 'failed to submit bulk data; request saved to %s' % (outfile.name,))

    def forceBulk(self, evenOnOneOperation=False):
        if len(self.bulkData) <= 1 and not evenOnOneOperation:
            return defer.succeed(None)

        oldBulkData = self.bulkData
        d = super(NiceBulkingElasticSearch, self).forceBulk()
        if self.failedBulkDataDirectory is not None:
            d.addErrback(self._logFailedBulkData, oldBulkData)
        return d


class ElastircLogFile(DailyLogFile):
    def suffix(self, unixtime_or_tupledate):
        try:
            dt = datetime.datetime.fromtimestamp(unixtime_or_tupledate)
        except TypeError:
            dt = datetime.datetime(*unixtime_or_tupledate)
        return dt.strftime(DATE_FORMAT)


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


class ElastircProtocol(_IRCBase):
    sourceURL = 'https://github.com/Weasyl/elastirc'
    versionName = 'elastirc'
    versionNum = 'HEAD'
    versionEnv = 'twisted'

    logFactory = ElastircLogFile

    def signedOn(self):
        self.join(','.join(self.factory.channels))
        self.logfiles = {}
        _IRCBase.signedOn(self)

    def _getLogFile(self, channel):
        ret = self.logfiles.get(channel)
        if not ret:
            thisLogDir = self.factory.logDir.child(channel)
            if not thisLogDir.exists():
                thisLogDir.makedirs()
            ret = self.logfiles[channel] = self.logFactory(channel, thisLogDir.path)
        return ret

    def logDocument(self, channel, **document):
        if channel not in self.factory.channels:
            return
        channel = channel.lstrip('#&')
        now = datetime.datetime.now()
        document['receivedAt'] = now.isoformat()
        self._getLogFile(channel).write(
            '%s %s\n' % (now.strftime(TIME_FORMAT), document['formatted'].encode('utf-8')))
        d = self.factory.elasticSearch.index(
            document, '%s.%s' % (channel, now.strftime(DATE_FORMAT)), 'irc', bulk=True)
        d.addErrback(log.err, 'error indexing')

    def privmsg(self, user, channel, message):
        nick = user.partition('!')[0]
        message = fixupMessage(message)
        self.logDocument(channel, actor=nick, message=message, formatted='<%s> %s' % (nick, message))

    def action(self, user, channel, message):
        nick = user.partition('!')[0]
        message = fixupMessage(message)
        self.logDocument(channel, actor=nick, message=message, formatted='* %s %s' % (nick, message))

    def userJoined(self, user, channel):
        nick = user.partition('!')[0]
        self.logDocument(channel, actor=nick, formatted='(-) %s joined' % (nick,))
        _IRCBase.userJoined(self, user, channel)

    def userLeft(self, user, channel):
        nick = user.partition('!')[0]
        self.logDocument(channel, actor=nick, formatted='(-) %s parted' % (nick,))
        _IRCBase.userLeft(self, user, channel)

    def userQuit(self, user, quitMessage):
        nick = user.partition('!')[0]
        quitMessage = fixupMessage(quitMessage)
        for channel, users in self.channelUsers.iteritems():
            if nick in users:
                self.logDocument(
                    channel, actor=nick, reason=quitMessage,
                    formatted='(-) %s quit (%s)' % (nick, quitMessage))
        _IRCBase.userQuit(self, user, quitMessage)

    def userKicked(self, kickee, channel, kicker, message):
        kickeeNick = kickee.partition('!')[0]
        kickerNick = kicker.partition('!')[0]
        message = fixupMessage(message)
        self.logDocument(
            channel, actor=kickeeNick, kicker=kickerNick, reason=message,
            formatted='(-) %s was kicked by %s (%s)' % (kickeeNick, kickerNick, message))
        _IRCBase.userKicked(self, kickee, channel, kicker, message)

    def userRenamed(self, oldname, newname):
        for channel, users in self.channelUsers.iteritems():
            if oldname in users:
                self.logDocument(
                    channel, actor=newname, oldName=oldname,
                    formatted='(-) %s changed nick from %s' % (newname, oldname))
        _IRCBase.userRenamed(self, oldname, newname)

    def topicUpdated(self, user, channel, newTopic):
        nick = user.partition('!')[0]
        newTopic = fixupMessage(newTopic)
        self.logDocument(
            channel, actor=nick, topic=newTopic,
            formatted='(-) %s changed topic to %s' % (nick, newTopic))

    def modeChanged(self, user, channel, polarity, modes, args):
        nick = user.partition('!')[0]
        self.logDocument(
            channel, actor=nick,
            formatted='(-) %s set mode %s%s %s' % (
                nick, '+' if polarity else '-', modes, ' '.join(arg for arg in args if arg)))


class ElastircFactory(protocol.ReconnectingClientFactory):
    protocol = ElastircProtocol
    channel = None
    channels = None


    def __init__(self, logDir, elasticSearch):
        self.logDir = logDir
        self.elasticSearch = elasticSearch
        if self.channels is None:
            self.channels = self.channel,

    def buildWebResource(self):
        root = Resource()
        root.putChild('', ElastircSearchResource(self))
        return root


class ElastircSearchTemplate(template.Element):
    loader = template.XMLFile(FilePath('templates/search.xhtml'))

    def __init__(self, channelNames):
        template.Element.__init__(self)
        self.channelNames = channelNames

    @template.renderer
    def channels(self, request, tag):
        for channel in self.channelNames:
            yield tag.clone().fillSlots(channel=channel, indexName=channel.lstrip('#&'))


class ElastircSearchResultsTemplate(template.Element):
    loader = template.XMLFile(FilePath('templates/search-results.xhtml'))

    def __init__(self, resultsDeferred):
        template.Element.__init__(self)
        self.resultsDeferred = resultsDeferred

    @template.renderer
    @defer.inlineCallbacks
    def results(self, request, tag):
        results = yield self.resultsDeferred
        ret = []
        for result in results['hits']['hits']:
            timestamp = parseTimestamp(result['_source']['receivedAt'])
            ret.append(tag.clone().fillSlots(timestamp=timestamp.strftime(TIME_FORMAT), **result['_source']))
        defer.returnValue(ret)


class ElastircSearchResource(Resource):
    def __init__(self, elastircFactory):
        self.elastircFactory = elastircFactory
        self.template_GET = ElastircSearchTemplate(self.elastircFactory.channels)

    def render_GET(self, request):
        return template.renderElement(request, self.template_GET)

    def render_POST(self, request):
        indexes = None
        if 'index' in request.args:
            indexes = [index + '*' for index in request.args.pop('index')]

        queryArgs = dict((k, v[0]) for k, v in request.args.iteritems() if k in ('actor', 'formatted') and any(v))
        if not queryArgs:
            return self.render_GET(request)
        query = {'query': {'term': queryArgs}}

        return template.renderElement(
            request,
            ElastircSearchResultsTemplate(
                self.elastircFactory.elasticSearch.search(query, indexes=indexes, docType='irc')))
