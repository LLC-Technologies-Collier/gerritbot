#!/usr/bin/python
# -*- coding: utf-8 -*-

#    Copyright 2011 OpenStack LLC
#    Copyright 2012 Hewlett-Packard Development Company, L.P.
#    Copyright 2016 The Linux Foundation
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

# For examples of gerritbot configuration files, see:
#  https://github.com/LLC-Technologies-Collier/gerritbot/tree/master/doc/examples/gerritbot.yaml
#  https://github.com/LLC-Technologies-Collier/gerritbot/tree/master/doc/examples/channels.yaml

import daemon
import getopt
import irc
import irc.bot
import logging
import logging.config
import os
import paramiko
import re
import ssl
import sys
import socket
import threading
import time
import yaml
from pathlib2 import Path

try:
    import daemon.pidlockfile
    pid_file_module = daemon.pidlockfile
except Exception:

    # as of python-daemon 1.6 it doesn't bundle pidlockfile anymore
    # instead it depends on lockfile-0.9.1

    import daemon.pidfile
    pid_file_module = daemon.pidfile

# https://bitbucket.org/jaraco/irc/issue/34/
# irc-client-should-not-crash-on-failed
# ^ This is why pep8 is a bad idea.

irc.client.ServerConnection.buffer_class.errors = 'replace'

log = None


def main():
    print('Launching gerritbot')

    verbose = False
    foreground = False

    config_file = None

    try:
        (opts, args) = getopt.getopt(sys.argv[1:],
                                     'vhc:f', ['verbose', 'help', 'config'])
    except getopt.GetoptError as err:
        # print help information and exit:
        print(str(err))
        usage()
        sys.exit(2)

    # process command line arguments
    for (o, a) in opts:
        if o in ('-v', '--verbose'):
            verbose = True
        elif o in ('-h', '--help'):
            usage()
            sys.exit(1)
        elif o in ('-c', '--config'):
            config_file = a
        elif o == '-f':
            foreground = True

    config = ensure_config(config_file, verbose=verbose)

    if foreground:
        log.debug('starting in foreground')
        _main(config)
    else:
        pidfile = '/tmp/gerritbot.pid'
        if 'pid' in config['general']:
            pidfile = config['general']['pid']

        log.debug('PID path: ' + pidfile)
        ensure_dir(pidfile)

        pid = pid_file_module.TimeoutPIDLockFile(pidfile, 10)
        log.debug('starting daemonized')
        with daemon.DaemonContext(pidfile=pid):
            _main(config)


class GerritBot(irc.bot.SingleServerIRCBot):

    def __init__(self, config):

        log.debug('initializing GerritBot object')
        self.channel_list = config['channels']

        botconfig = config['ircbot']

        self.password = botconfig['pass']
        self.port = int(botconfig['port'])
        self.server = botconfig['server']

        if 'server_password' not in botconfig:
            botconfig['server_password'] = None
        if 'force_ssl' not in botconfig:
            botconfig['force_ssl'] = 'False'
        if 'nick' not in botconfig:
            botconfig['nick'] = 'gerritbot'
        if 'realname' not in botconfig:
            botconfig['realname'] = botconfig['nick']

        self.server_password = botconfig['server_password']
        self.force_ssl = bool(botconfig['force_ssl'])
        self.nickname = botconfig['nick']
        self.realname = botconfig['realname']

        sspec = irc.bot.ServerSpec(self.server,
                                   self.port,
                                   self.server_password)

        fmt = 'connecting to server %s %s ssl'
        c = irc.connection

        if self.force_ssl or self.port == 6697:
            log.debug(fmt % (self.server, 'with'))
            factory = c.Factory(wrapper=ssl.wrap_socket)
        else:
            log.debug(fmt % (self.server, 'without'))
            factory = c.Factory()

        super(GerritBot, self).__init__([sspec],
                                        self.nickname,
                                        self.realname,
                                        connect_factory=factory)

    def on_nicknameinuse(self, c, e):
        log.info('Nick previously in use, recovering.')
        c.nick(c.get_nickname() + '_')
        c.privmsg('nickserv', 'identify %s ' % self.password)
        c.privmsg('nickserv', 'ghost %s %s' % (self.nickname,
                                               self.password))
        c.privmsg('nickserv', 'release %s %s' % (self.nickname,
                                                 self.password))
        time.sleep(1)
        c.nick(self.nickname)
        log.info('Nick previously in use, recovered.')

    def on_welcome(self, c, e):
        log.info('Identifying with IRC server.')
        c.privmsg('nickserv', 'identify %s ' % self.password)
        log.info('Identified with IRC server.')
        for channel in self.channel_list:
            c.join(channel)
            log.info('Joined channel %s' % channel)
            time.sleep(0.5)

    def send(self, channel, msg):
        log.info('Sending "%s" to %s' % (msg, channel))
        try:
            self.connection.privmsg(channel, msg)
            time.sleep(0.5)
        except Exception:
            log.exception('Exception sending message:')
            self.connection.reconnect()


class Gerrit(threading.Thread):

    def __init__(self, ircbot, config):
        # Import here because it needs to happen after daemonization

        global gerritlib
        import gerritlib.gerrit

        log.debug('initializing Gerrit object')

        cfg = config['gerrit']

        if 'sshlog' in cfg:
            ensure_dir(cfg['sshlog'])
            paramiko.util.log_to_file(cfg['sshlog'])

        self.channel_config = config['channels']
        self.ircbot = ircbot
        self.connected = False

        self.server = cfg['server']
        self.username = cfg['username']
        self.port = int(cfg['port'])
        self.keyfile = cfg['keyfile']

        super(Gerrit, self).__init__()

    def connect(self):

        try:
            self.gerrit = gerritlib.gerrit.Gerrit(self.server, self.username,
                                                  self.port, self.keyfile)
            self.gerrit.startWatching()
            log.info('Start watching Gerrit event stream.')
            self.connected = True
        except (IOError, paramiko.AuthenticationException) as e:
            errstr = "Exception connecting to %s:%s with key %s" % (
                self.hostname, self.port, self.keyfile)
            log.exception(errstr)
            print(errstr)
            exit(1)
        except Exception as msg:
            log.exception('Exception while connecting to gerrit: %s' % msg)
            self.connected = False

            # Delay before attempting again.

            time.sleep(1)

    def patchset_created(self, channel, data):
        changevals = data['patchSet']['uploader']['name'], (
            data['change'][k] for k in ['project', 'subject', 'url'])
        msg = '%s proposed %s: %s  %s' % changevals
        log.info('Compiled Message %s: %s' % (channel, msg))
        self.ircbot.send(channel, msg)

    def ref_updated(self, channel, data):
        refName = data['refUpdate']['refName']
        m = re.match(r'(refs/tags)/(.*)', refName)

        if m:
            tag = m.group(2)
            msg = '%s tagged project %s with %s' \
                  % (data['submitter']['username'],
                     data['refUpdate']['project'],
                     tag)
            log.info('Compiled Message %s: %s' % (channel, msg))
            self.ircbot.send(channel, msg)

    def comment_added(self, channel, data):
        msg = 'A comment has been added to a proposed change to %s: %s  %s' \
            % (data['change']['project'],
               data['change']['subject'],
               data['change']['url'])
        log.info('Compiled Message %s: %s' % (channel, msg))
        self.ircbot.send(channel, msg)

        for approval in data.get('approvals', []):
            if approval['type'] == 'VRIF' and approval['value'] == '-2' \
                and channel \
                in self.channel_config.events.get('x-vrif-minus-2',
                                                  set()):
                msg = 'Verification of a change to %s failed: %s  %s' \
                    % (data['change']['project'],
                       data['change']['subject'],
                       data['change']['url'])
                log.info('Compiled Message %s: %s' % (channel,
                                                      msg))
                self.ircbot.send(channel, msg)

            if approval['type'] == 'VRIF' and approval['value'] == '2' \
                and channel \
                in self.channel_config.events.get('x-vrif-plus-2',
                                                  set()):
                msg = 'Verification of a change to %s succeeded: %s  %s' \
                    % (data['change']['project'],
                       data['change']['subject'],
                       data['change']['url'])
                log.info('Compiled Message %s: %s' % (channel,
                                                      msg))
                self.ircbot.send(channel, msg)

            if approval['type'] == 'CRVW' and approval['value'] == '-2' \
                and channel \
                in self.channel_config.events.get('x-crvw-minus-2',
                                                  set()):
                msg = 'A change to %s has been rejected: %s  %s' \
                    % (data['change']['project'],
                       data['change']['subject'],
                       data['change']['url'])
                log.info('Compiled Message %s: %s' % (channel,
                                                      msg))
                self.ircbot.send(channel, msg)

            if approval['type'] == 'CRVW' and approval['value'] == '2' \
                and channel \
                in self.channel_config.events.get('x-crvw-plus-2',
                                                  set()):
                msg = 'A change to %s has been approved: %s  %s' \
                    % (data['change']['project'],
                       data['change']['subject'],
                       data['change']['url'])
                log.info('Compiled Message %s: %s' % (channel,
                                                      msg))
                self.ircbot.send(channel, msg)

    def change_merged(self, channel, data):
        msg = 'Merged %s: %s  %s' \
              % (data['change']['project'],
                 data['change']['subject'],
                 data['change']['url'])
        log.info('Compiled Message %s: %s' % (channel, msg))
        self.ircbot.send(channel, msg)

    def _read(self, data):
        try:
            if data['type'] == 'ref-updated':
                channel_set = self.channel_config.events.get('ref-updated')
            else:
                channel_set = self.channel_config.projects.get(data['change'
                                                                    ]['project'], set()) \
                    & self.channel_config.events.get(data['type'],
                                                     set()) \
                    & self.channel_config.branches.get(data['change'
                                                            ]['branch'], set())
        except KeyError:

            # The data we care about was not present, no channels want
            # this event.

            channel_set = set()
        log.info('Potential channels to receive event notification: %s'
                 % channel_set)
        for channel in channel_set:
            if data['type'] == 'comment-added':
                self.comment_added(channel, data)
            elif data['type'] == 'patchset-created':
                self.patchset_created(channel, data)
            elif data['type'] == 'change-merged':
                self.change_merged(channel, data)
            elif data['type'] == 'ref-updated':
                self.ref_updated(channel, data)

    def run(self):
        while True:
            while not self.connected:
                self.connect()
            try:
                event = self.gerrit.getEvent()
                log.info('Received event: %s' % event)
                self._read(event)
            except Exception as msg:
                log.exception('Exception encountered in event loop: %s' % msg)
                if not self.gerrit.watcher_thread.is_alive():

                    # Start new gerrit connection. Don't need to restart IRC
                    # bot, it will reconnect on its own.

                    self.connected = False


class ChannelConfig(object):

    def __init__(self, data):
        log.debug('initializing ChannelConfig object')
        self.data = data
        keys = data.keys()
        for key in keys:
            if key[0] != '#':
                data['#' + key] = data.pop(key)
        self.channels = data.keys()
        self.projects = {}
        self.events = {}
        self.branches = {}

        for (channel, val) in iter(self.data.items()):
            for event in val['events']:
                event_set = self.events.get(event, set())
                event_set.add(channel)
                self.events[event] = event_set

            for project in val['projects']:
                project_set = self.projects.get(project, set())
                project_set.add(channel)
                self.projects[project] = project_set

            for branch in val['branches']:
                branch_set = self.branches.get(branch, set())
                branch_set.add(channel)
                self.branches[branch] = branch_set


def _main(config):
    log.debug('running _main')

    log.debug('instantiating bot object')
    bot = GerritBot(config)

    log.debug('instantiating gerrit object')
    g = Gerrit(bot, config)

    g.start()
    bot.start()


def ensure_dir(f):
    d = Path(os.path.dirname(f))
    d.mkdir(mode=0o770, parents=True, exist_ok=True)


def ensure_service(name, host, port, close_socket=True):
    s = None
    sa = None
    results = None

    # attempt to resolve hostname
    try:
        results = socket.getaddrinfo(host, port,
                                     socket.AF_UNSPEC, socket.SOCK_STREAM, socket.IPPROTO_TCP)
    except socket.error as msg:
        print('could not resolve address for %s service (%s:%i) specified in config file' %
              (name, host, port))
        print msg
        exit(1)

    for res in results:
        af, socktype, proto, canonname, sa = res

        # attempt to establish a socket at specified host:port
        try:
            s = socket.socket(af, socktype, proto)
            break
        except socket.error as msg:
            continue

    if s is None:
        print('could not open TCP socket with %s service (%s:%i) specified in config file' %
              (name, host, port))
        exit(1)

    print('Verified %s service on host %s listening at address %s:%d' %
          (name, host, str(sa[0]), int(sa[1])))

    if close_socket:
        s.close()
    else:
        return s


def ensure_log_config(logconfig, verbose=False):
    if 'handlers' in logconfig and 'file' in logconfig['handlers']:
        ensure_dir(logconfig['handlers']['file']['filename'])

    if verbose:
        l = logconfig['loggers']
        print("Turning verbosity up to 11")

        maxdebug = {'handlers': ['file', 'syslog', 'console'],
                    'level': logging.DEBUG,
                    'qualname': None}

        for k in logconfig['handlers'].keys():
            logconfig['handlers'][k]['level'] = logging.DEBUG

        for qualname in logging.Logger.manager.loggerDict.keys():
            logger = logging.getLogger(qualname)
            logger.handlers = []
            logger.propogate = False
            l[qualname] = maxdebug.copy()
            l[qualname]['qualname'] = qualname

        for qualname in l.keys():
            l[qualname] = maxdebug.copy()
            l[qualname]['qualname'] = qualname

    logging.config.dictConfig(logconfig)

    global log

    log = logging.getLogger('gerritbot')
    log.propogate = False

    log.debug('Logging initialized for gerritbot')


def ensure_gerrit_config(gerrit_config, verbose=False):
    ssh_key = Path(gerrit_config['keyfile'])
    if not ssh_key.exists():
        print("ssh private key specified in config file (%s) does not exist" %
              str(ssh_key))
        exit(1)

    # make sure host name resolves and port is listening
    socket = ensure_service('gerrit', gerrit_config[
        'server'], int(gerrit_config['port']), close_socket=False)

    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.WarningPolicy())

    try:
        client.connect(gerrit_config['server'],
                       port=int(gerrit_config['port']),
                       username='gerrit',
                       password=None,
                       pkey=None,
                       key_filename=str(ssh_key),
                       timeout=2.0,
                       allow_agent=False,
                       look_for_keys=False,
                       compress=False,
                       sock=None,
                       gss_auth=False,
                       gss_kex=False,
                       gss_deleg_creds=False,
                       gss_host=None,
                       banner_timeout=0.5)
    except paramiko.AuthenticationException as e:
        print(
            "Authentication error connecting to %s:%s with key %s: %s" % (
                gerrit_config['server'], gerrit_config['port'], ssh_key, e))
        exit(1)

    except paramiko.SSHException as e:
        print("I hate paramiko.");

#        print('SSH exception of type %s caught' % type(e))
#        raise
    except Exception as e:
        print("Unexpected %s error: %s" % (type(e), e))
        raise
    else:
        client.close()
        socket.close()
        print(
            'ssh session was successfully established using config file settings')

    log.debug('gerrit configuration validated')


def ensure_irc_config(irc_config, verbose=False):

    # make sure host name resolves and port is listening
    ensure_service('irc', irc_config['server'], int(irc_config['port']))

    # TODO: verify irc nick registration and credentials
    # TODO: verify SSL handshake

    log.debug('irc configuration validated')


def ensure_config(config_file, verbose=False):

    if config_file is None:
        if verbose:
            print('Configuration not specified.  Searching for one now...')

        userconfdir = Path.home() / '.config'

        dirname = Path(os.path.dirname(sys.argv[0]))

        drive, norm_path = os.path.splitdrive(str(dirname))

        searchpath = [userconfdir, Path('/etc')]

        if re.match(str(Path('/bin')) + '$', str(dirname)):
            searchpath.append(dirname / '..' / 'etc')

        if norm_path.find(str(Path('/opt'))) == 0:
            searchpath.append(Path('/etc/opt'))

        for path in searchpath:
            if path.exists() and path.is_dir():
                gbpath = path / 'gerritbot'
                if gbpath.exists() and gbpath.is_dir():
                    gbyaml = gbpath / 'gerritbot.yaml'
                    if gbyaml.is_file():
                        print("Found config file " + str(gbyaml))
                        break
                    elif verbose:
                        print("Did not find config file " + str(gbyaml))
                elif verbose:
                    print("Potential gerritbot config dir %s does not exist" %
                          str(gbpath))
            elif verbose:
                print("Potential gerritbot config dir parent %s does not exist" %
                      str(path))

    if config_file is None or not os.path.exists(config_file):
        print('Configuration file could not be determined')
        usage()
        sys.exit(1)

    print('Reading config from ' + config_file)
    config = yaml.load(open(config_file))

    general_cfg = config['general']

    # read config files if they are not inline
    for filekey, confkey in {'channel_config': 'channels',
                             'log_config': 'logging',
                             'bot_config': 'ircbot',
                             'gerrit_config': 'gerrit'}.iteritems():
        if filekey in general_cfg and general_cfg[filekey] != 'inline':
            config[confkey] = yaml.load(open(general_cfg[filekey]))

    ensure_log_config(config['logging'], verbose)
    ensure_irc_config(config['ircbot'], verbose)
    ensure_gerrit_config(config['gerrit'], verbose)

    log.debug('all configuration validated')

    return config


def usage():
    print('Usage: ' + sys.argv[0] + ' [options]\n' + 'Options:\n'
          + '-h --help           Show help\n'
          + '-f                  Run in foreground\n'
          + '-v                  Verbose\n'
          + '-c --config <file>  Read configuration from <file>\n'
          + '                    (default: /etc/gerritbot/gerritbot.yaml)')

if __name__ == '__main__':
    main()
