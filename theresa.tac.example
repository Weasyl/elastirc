# -*- python -*-
import theresa
from twisted.application import internet, service

class Theresa(theresa.TheresaProtocol):
    nickname = 'theresa'
    nickserv_pw = 'goatse'
    channel = '#theresa-test'

class TheresaFactory(theresa.TheresaFactory):
    protocol = Theresa

application = service.Application("echo")
theresaFac = TheresaFactory()
internet.TCPClient('irc.esper.net', 5555, theresaFac).setServiceParent(application)
theresa.MSPAChecker(theresaFac).setServiceParent(application)
