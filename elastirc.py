# Copyright (c) Aaron Gallagher <_@habnab.it>; Weasyl LLC
# See COPYING for details.

from __future__ import division

from twisted.cred.portal import IRealm
from twisted.internet import protocol
from twisted.python.filepath import FilePath
from twisted.python.logfile import BaseLogFile
from twisted.web.resource import Resource, ForbiddenResource, IResource
from twisted.web import template, static
from twisted.words.protocols import irc

from zope.interface import implementer
import whoosh.fields
from whoosh.qparser import QueryParser
from whoosh import query

import collections
import datetime
import operator
import os.path
import re


ircFormattingCruftRegexp = re.compile('\x03[0-9]{1,2}(?:,[0-9]{1,2})?|[\x00-\x08\x0A-\x1F]')
def fixupMessage(message):
    return ircFormattingCruftRegexp.sub('', message).decode('utf-8', 'replace')

DATE_FORMAT = '%F'
TIME_FORMAT = '%T'

def unprefixedChannel(channel):
    return channel.lstrip('#&')


whooshSchema = whoosh.fields.Schema(
    formatted=whoosh.fields.TEXT(stored=True),
    receivedAt=whoosh.fields.DATETIME(stored=True),
    channel=whoosh.fields.ID(stored=True),
    actor=whoosh.fields.ID(),

    message=whoosh.fields.TEXT(),
    topic=whoosh.fields.TEXT(),
    reason=whoosh.fields.TEXT(),
    oldName=whoosh.fields.ID(),
    kicker=whoosh.fields.ID(),
)



class DatestampedLogFile(BaseLogFile, object):
    """A LogFile which always logs to files suffixed with the current date.

    The date format used as a suffix is controlled by the `datestampFormat`
    attribute.
    """

    datestampFormat = DATE_FORMAT

    def __init__(self, name, directory, defaultMode=None):
        self.basePath = os.path.join(directory, name)
        BaseLogFile.__init__(self, name, directory, defaultMode)
        self.lastPath = self.path

    def _getPath(self):
        "The logfile path will always be the base path with a dated suffix."
        return '%s.%s' % (self.basePath, self.suffix())

    def _setPath(self, ignored):
        "Assignment to the `path` attribute is ignored."
        pass

    path = property(_getPath, _setPath)

    def shouldRotate(self):
        """Returns True if the log should be rotated.

        Logs are only rotated when the current date is different from the date
        of the last log message.
        """

        return self.path != self.lastPath

    def rotate(self):
        "Rotate the log files."

        # Since logs aren't _actually_ rotated (since they're created with the
        # name they'll always have), just close the old log file and open the
        # new one.
        self.reopen()

    def suffix(self, when=None):
        """Determine the suffix for log files, optionally for a given datetime.

        By default, this is derived from the `datestampFormat` attribute.
        """

        if when is None:
            when = datetime.datetime.now()
        return when.strftime(self.datestampFormat)


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

    def signedOn(self):
        self.join(','.join(self.factory.channels))
        _IRCBase.signedOn(self)

    def logDocument(self, channel, **document):
        self.factory.logDocument(channel, document)

    def privmsg(self, user, channel, message):
        nick = user.partition('!')[0]
        self.logDocument(channel, actor=nick, message=message, formatted='<%s> %s' % (nick, message))

    def action(self, user, channel, message):
        nick = user.partition('!')[0]
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
        for channel, users in self.channelUsers.iteritems():
            if nick in users:
                self.logDocument(
                    channel, actor=nick, reason=quitMessage,
                    formatted='(-) %s quit (%s)' % (nick, quitMessage))
        _IRCBase.userQuit(self, user, quitMessage)

    def userKicked(self, kickee, channel, kicker, message):
        kickeeNick = kickee.partition('!')[0]
        kickerNick = kicker.partition('!')[0]
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
        self.logDocument(
            channel, actor=nick, topic=newTopic,
            formatted='(-) %s changed topic to %s' % (nick, newTopic))

    def modeChanged(self, user, channel, polarity, modes, args):
        nick = user.partition('!')[0]
        self.logDocument(
            channel, actor=nick,
            formatted='(-) %s set mode %s%s %s' % (
                nick, '+' if polarity else '-', modes, ' '.join(arg for arg in args if arg)))


@implementer(IRealm)
class ElastircFactory(protocol.ReconnectingClientFactory):
    protocol = ElastircProtocol
    logFactory = DatestampedLogFile

    channel = None
    channels = None

    def __init__(self, logDir, writer, userAllowedChannels=None):
        self.logDir = logDir
        self.writer = writer
        self.logfiles = {}
        if self.channels is None:
            self.channels = self.channel,
        self.logDirResource = static.File(self.logDir.path, defaultType='text/plain; charset=utf-8')
        self.userAllowedChannels = userAllowedChannels

    def getLogFile(self, channel):
        """Return the LogFile for the given channel.

        If there's no current log file for the channel, make one using the
        `logFactory` attribute first.
        """

        channel = unprefixedChannel(channel)
        ret = self.logfiles.get(channel)
        if not ret:
            thisLogDir = self.logDir.child(channel)
            if not thisLogDir.exists():
                thisLogDir.makedirs()
            ret = self.logfiles[channel] = self.logFactory(channel, thisLogDir.path)
        return ret

    def logDocument(self, channel, document):
        """Log a document from a particular channel.

        This will immediately write out the plaintext log to disk and queue up
        the ElasticSearch item for inserting. The document can contain as many
        keys as are relevant, but must at least contain a unicode string under
        the key `formatted`, which will be written out to the log file and
        displayed in search results.
        """

        if channel not in self.channels:
            return
        channel = unprefixedChannel(channel)
        now = datetime.datetime.now()
        for k, v in document.iteritems():
            document[k] = fixupMessage(v)
        self.getLogFile(channel).write(
            '%s %s\n' % (now.strftime(TIME_FORMAT), document['formatted'].encode('utf-8')))
        document['receivedAt'] = now
        document['channel'] = channel.decode()
        self.writer.add_document(**document)

    def buildWebResource(self, allowedChannels=None):
        """Make a Resource that exposes logs and log search.

        The plaintext logs are available under `/logs` and the search is
        available at `/`. If `allowedChannels` is provided, search and log
        browsing will be limited to only those channels.
        """

        root = Resource()
        root.putChild('', ElastircSearchResource(self, allowedChannels))
        root.putChild('logs', ElastircLogsResource(self.logDirResource, allowedChannels))
        return root

    def requestAvatar(self, username, mind, *interfaces):
        if IResource not in interfaces or self.userAllowedChannels is None:
            raise NotImplementedError()
        if not self.userAllowedChannels.get(username):
            ret = ForbiddenResource()
        else:
            ret = self.buildWebResource(self.userAllowedChannels[username])
        return IResource, ret, lambda: None


class ElastircSearchTemplate(template.Element):
    "A template for the search form."

    loader = template.XMLFile(FilePath('templates/search.xhtml'))

    def __init__(self, channelNames):
        template.Element.__init__(self)
        self.channelNames = channelNames

    @template.renderer
    def channels(self, request, tag):
        "Drop in each searchable channel."
        for channel in self.channelNames:
            yield tag.clone().fillSlots(channel=channel, channelName=unprefixedChannel(channel))


class ElastircSearchResultFileTemplate(template.Element):
    "A template for displaying the all the matches for a particular log file."

    loader = template.XMLFile(FilePath('templates/search-result-file.xhtml'))

    def __init__(self, logfile, hits):
        template.Element.__init__(self)
        self.logfile = logfile
        self.hits = hits

    @template.renderer
    def content(self, request, tag):
        "Drop in information about the log file."
        channel, logDate = self.logfile
        logName = '%s.%s' % (channel, logDate.strftime(DATE_FORMAT))
        return tag.fillSlots(
            logName=logName,
            logPath='/logs/%s/%s' % (channel, logName))

    @template.renderer
    def logLines(self, request, tag):
        "Drop in each matched line from the log file."
        self.hits.sort(key=operator.itemgetter('receivedAt'))
        for result in self.hits:
            yield tag.clone().fillSlots(
                timestamp=result['receivedAt'].strftime(TIME_FORMAT),
                **result)


class ElastircSearchResultsTemplate(template.Element):
    "A template for the search results page."

    loader = template.XMLFile(FilePath('templates/search-results.xhtml'))

    def __init__(self, searchResults):
        template.Element.__init__(self)
        self.searchResults = searchResults

    @template.renderer
    def results(self, request, tag):
        resultsByChannel = collections.defaultdict(list)
        for result in self.searchResults:
            logfile = result['channel'], result['receivedAt'].date()
            resultsByChannel[logfile].append(result)

        ret = []
        for logfile, hits in resultsByChannel.iteritems():
            ret.append(ElastircSearchResultFileTemplate(logfile, hits))
        ret.append(tag.fillSlots(took='%0.3g' % (self.searchResults.runtime,)))
        return ret


class ElastircSearchResource(Resource):
    "A Resource for searching the ElasticSearch backend."

    def __init__(self, elastircFactory, channels=None):
        Resource.__init__(self)
        self.elastircFactory = elastircFactory
        if channels is None:
            channels = self.elastircFactory.channels
        self.channels = set(channels)
        self.unprefixedChannels = set(unprefixedChannel(channel) for channel in self.channels)
        self.template_GET = ElastircSearchTemplate(self.channels)

    def render_GET(self, request):
        "Show the template for the search form."
        request.setHeader('content-type', 'text/html; charset=utf8')
        return template.renderElement(request, self.template_GET)

    def render_POST(self, request):
        "Perform the actual search."
        channels = self.unprefixedChannels
        if 'channel' in request.args:
            channels.intersection_update(request.args.pop('channel'))
        queryArgs = dict(
            (k, v[0].decode('utf-8', 'replace'))
            for k, v in request.args.iteritems()
            if k in ('actor', 'formatted') and any(v))
        if not queryArgs or not channels:
            return self.render_GET(request)

        q = query.And([
            query.Or([query.Term('channel', channel.decode('utf-8', 'replace')) for channel in channels]),
            query.And([QueryParser(k, schema=whooshSchema).parse(v) for k, v in queryArgs.iteritems()]),
        ])

        request.setHeader('content-type', 'text/html; charset=utf-8')
        with self.elastircFactory.writer.searcher() as s:
            results = s.search(q)
            return template.renderElement(request, ElastircSearchResultsTemplate(results))


class ElastircLogsResource(Resource):
    def __init__(self, logDirResource, allowedChannels=None):
        Resource.__init__(self)
        self.logDirResource = logDirResource
        allowed = allowedChannels
        if allowed is not None:
            allowed = set(unprefixedChannel(channel) for channel in allowedChannels)
        self.allowed = allowed

    def render(self, request):
        return ForbiddenResource().render(request)

    def getChild(self, name, request):
        if not name or (self.allowed is not None and name not in self.allowed):
            return ForbiddenResource()
        return self.logDirResource.getChildWithDefault(name, request)
