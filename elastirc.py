# Copyright (c) Aaron Gallagher <_@habnab.it>; Weasyl LLC
# See COPYING for details.

from twisted.internet import protocol
from twisted.python.logfile import DailyLogFile
from twisted.words.protocols import irc

import collections
import datetime
import re

ircFormattingCruft = re.compile('\x03[0-9]{1,2}(?:,[0-9]{1,2})?|[\x00-\x08\x0A-\x1F]')
def stripFormattingCruft(s):
    return ircFormattingCruft.sub('', s)

class ElastircLogFile(DailyLogFile):
    timestampFormat = '%T'

    def writeTimestampedLine(self, data):
        now = datetime.datetime.now()
        self.write('%s %s\n' % (now.strftime(self.timestampFormat), data))

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
    channel = None
    channels = None

    sourceURL = 'https://github.com/Weasyl/elastirc'
    versionName = 'elastirc'
    versionNum = 'HEAD'
    versionEnv = 'twisted'

    logFactory = ElastircLogFile

    def __init__(self):
        if self.channels is None:
            self.channels = self.channel,

    def signedOn(self):
        self.join(','.join(self.channels))
        self.logfiles = {}
        _IRCBase.signedOn(self)

    def getLogFile(self, channel):
        channel = channel.lstrip('#&')
        ret = self.logfiles.get(channel)
        if not ret:
            thisLogDir = self.factory.logDir.child(channel)
            if not thisLogDir.exists():
                thisLogDir.makedirs()
            ret = self.logfiles[channel] = self.logFactory(channel, thisLogDir.path)
        return ret

    def logLine(self, channel, message):
        if channel not in self.channels:
            return
        self.getLogFile(channel).writeTimestampedLine(stripFormattingCruft(message))

    def privmsg(self, user, channel, message):
        nick = user.partition('!')[0]
        self.logLine(channel, '<%s> %s' % (nick, message))

    def action(self, user, channel, message):
        nick = user.partition('!')[0]
        self.logLine(channel, '* %s %s' % (nick, message))

    def userJoined(self, user, channel):
        nick = user.partition('!')[0]
        self.logLine(channel, '(-) %s joined' % (nick,))
        _IRCBase.userJoined(self, user, channel)

    def userLeft(self, user, channel):
        nick = user.partition('!')[0]
        self.logLine(channel, '(-) %s parted' % (nick,))
        _IRCBase.userLeft(self, user, channel)

    def userQuit(self, user, quitMessage):
        nick = user.partition('!')[0]
        for channel, users in self.channelUsers.iteritems():
            if nick in users:
                self.logLine(channel, '(-) %s quit (%s)' % (nick, quitMessage))
        _IRCBase.userQuit(self, user, quitMessage)

    def userKicked(self, kickee, channel, kicker, message):
        kickeeNick = kickee.partition('!')[0]
        kickerNick = kicker.partition('!')[0]
        self.logLine(channel, '(-) %s was kicked by %s (%s)' % (kickeeNick, kickerNick, message))
        _IRCBase.userKicked(self, kickee, channel, kicker, message)

    def userRenamed(self, oldname, newname):
        for channel, users in self.channelUsers.iteritems():
            if oldname in users:
                self.logLine(channel, '(-) %s changed nick from %s' % (newname, oldname))
        _IRCBase.userRenamed(self, oldname, newname)

    def topicUpdated(self, user, channel, newTopic):
        nick = user.partition('!')[0]
        self.logLine(channel, '(-) %s changed topic to %s' % (nick, newTopic))

class ElastircFactory(protocol.ReconnectingClientFactory):
    protocol = ElastircProtocol

    def __init__(self, logDir):
        self.logDir = logDir
