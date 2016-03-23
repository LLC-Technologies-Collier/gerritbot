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
import threading
import time
import yaml

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

    try:
        (opts, args) = getopt.getopt(sys.argv[1:],
                                     'vhc:f', ['verbose', 'help', 'config'])
    except getopt.GetoptError as err:
        # print help information and exit:
        print(str(err))
        usage()
        sys.exit(2)

    verbose = False
    foreground = False

    config_file = '/etc/gerritbot/gerritbot.yaml'

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

    if not os.path.exists(config_file):
        raise Exception('Unable to read config file at %s'
                        % config_file)

    print('Reading config from ' + config_file)
    config = yaml.load(open(config_file))

    gcfg = config['general']

    # read config files if they are not inline
    for filekey, confkey in {'channel_config': 'channels',
                             'log_config': 'logging',
                             'bot_config': 'ircbot',
                             'gerrit_config': 'gerrit'}.iteritems():
        if filekey in gcfg and gcfg[filekey] != 'inline':
            config[confkey] = yaml.load(open(gcfg[filekey]))

    logconfig = config['logging']

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

#        l['root']['level'] = logging.CRITICAL
        del l['irc']

    logging.config.dictConfig(logconfig)

    for qualname in logging.Logger.manager.loggerDict.keys():
        print ('qualname: ' + qualname)

    global log

    log = logging.getLogger('gerritbot')
    log.propogate = False

    log.debug('Logging initialized for gerritbot')

    if foreground is False:
        pidfile = '/tmp/gerritbot.pid'
        if 'pid' in config['ircbot']:
            pidfile = config['ircbot']['pid']

        log.debug('PID path: ' + pidfile)
        ensure_dir(pidfile)

        pid = pid_file_module.TimeoutPIDLockFile(pidfile, 10)
        log.debug('starting daemonized')
        with daemon.DaemonContext(pidfile=pid):
            _main(config)
    else:
        log.debug('starting in foreground')
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
        log.debug('initializing Gerrit object')

        cfg = config['gerrit']

        if 'sshlog' in cfg:
            paramiko.util.log_to_file(cfg['sshlog'])

        super(Gerrit, self).__init__()
        self.ircbot = ircbot
        self.channel_config = config['channels']
        self.server = cfg['host']
        self.username = cfg['user']
        self.port = int(cfg['port'])
        log.debug('key: ' + cfg['key'])
        self.keyfile = cfg['key']
        self.connected = False

    def connect(self):

        # Import here because it needs to happen after daemonization

        import gerritlib.gerrit
        for qualname in logging.Logger.manager.loggerDict.keys():
            print("qualname: " + qualname)

        try:
            self.gerrit = gerritlib.gerrit.Gerrit(self.server, self.username,
                                                  self.port, self.keyfile)
            self.gerrit.startWatching()
            log.info('Start watching Gerrit event stream.')
            self.connected = True
        except Exception:
            log.exception('Exception while connecting to gerrit')
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
            except Exception:
                log.exception('Exception encountered in event loop'
                              )
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
    d = os.path.dirname(f)
    if not os.path.exists(d):
        os.makedirs(d)


def usage():
    print('Usage: ' + sys.argv[0] + ' [options]\n' + 'Options:\n'
          + '-h --help           Show help\n'
          + '-f                  Run in foreground\n'
          + '-c --config <file>  Read configuration from <file>\n'
          + '                    (default: /etc/gerritbot/gerritbot.yaml)')

if __name__ == '__main__':
    main()
