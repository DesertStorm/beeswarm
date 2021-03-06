# Copyright (C) 2014 Johnny Vestergaard <jkv@unixcluster.dk>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import os
import logging
import json
import sys

import requests
from requests.exceptions import Timeout, ConnectionError
import gevent
import zmq.green as zmq
import zmq.auth
from zmq.utils.monitor import recv_monitor_message

import beeswarm
import beeswarm.shared
from beeswarm.shared.message_enum import Messages
from beeswarm.shared.helpers import extract_keys, send_zmq_push, asciify, get_most_likely_ip
from beeswarm.drones.honeypot.honeypot import Honeypot
from beeswarm.drones.client.client import Client
from beeswarm.shared.socket_enum import SocketNames

logger = logging.getLogger(__name__)


class Drone(object):
    """ Aggregates a honeypot or client. """

    def __init__(self, work_dir, config, key='server.key', cert='server.crt', local_pull_socket=None, **kwargs):
        """

        :param work_dir: Working directory (usually the current working directory)
        :param config: Beeswarm configuration dictionary, None if no configuration was supplied.
        :param key: Key file used for SSL enabled capabilities
        :param cert: Cert file used for SSL enabled capabilities
        """

        # write ZMQ keys to files - as expected by pyzmq
        extract_keys(work_dir, config)
        self.work_dir = work_dir
        self.config = config
        self.config_file = os.path.join(work_dir, 'beeswarmcfg.json')
        self.key = key
        self.cert = cert
        self.id = self.config['general']['id']
        self.local_pull_socket = local_pull_socket

        # Honeypot / Client
        self.drone = None
        self.drone_greenlet = None
        self.outgoing_msg_greenlet = None
        self.incoming_msg_greenlet = None

        # messages from server relayed to internal listeners
        ctx = beeswarm.shared.zmq_context
        self.internal_server_relay = ctx.socket(zmq.PUSH)
        self.internal_server_relay.bind(SocketNames.SERVER_COMMANDS.value)

        if self.config['general']['fetch_ip']:
            try:
                url = 'http://api.externalip.net/ip'
                req = requests.get(url)
                self.ip = req.text
                logger.info('Fetched {0} as external ip for Honeypot.'.format(self.ip))
            except (Timeout, ConnectionError) as e:
                logger.warning('Could not fetch public ip: {0}'.format(e))
        else:
            self.ip = ''
        self.greenlets = []
        self.config_received = gevent.event.Event()

    def start(self):
        """ Starts services. """
        cert_path = os.path.join(self.work_dir, 'certificates')
        public_keys_dir = os.path.join(cert_path, 'public_keys')
        private_keys_dir = os.path.join(cert_path, 'private_keys')

        client_secret_file = os.path.join(private_keys_dir, "client.key")
        client_public, client_secret = zmq.auth.load_certificate(client_secret_file)
        server_public_file = os.path.join(public_keys_dir, "server.key")
        server_public, _ = zmq.auth.load_certificate(server_public_file)

        self.outgoing_msg_greenlet = gevent.spawn(self.outgoing_server_comms, server_public,
                                                  client_public, client_secret)
        self.outgoing_msg_greenlet.link_exception(self.on_exception)
        self.incoming_msg_greenlet = gevent.spawn(self.incoming_server_comms, server_public,
                                                  client_public, client_secret)
        self.incoming_msg_greenlet.link_exception(self.on_exception)

        logger.info('Waiting for detailed configuration from Beeswarm server.')
        gevent.joinall([self.outgoing_msg_greenlet])

    def _start_drone(self):
        """
        Restarts the drone
        """

        with open(self.config_file, 'r') as config_file:
            self.config = json.load(config_file, object_hook=asciify)

        mode = None
        if self.config['general']['mode'] == '' or self.config['general']['mode'] is None:
            logger.info('Drone has not been configured, awaiting configuration from Beeswarm server.')
        elif self.config['general']['mode'] == 'honeypot':
            mode = Honeypot
        elif self.config['general']['mode'] == 'client':
            mode = Client

        if mode:
            self.drone = mode(self.work_dir, self.config)
            self.drone_greenlet = gevent.spawn(self.drone.start)
            self.drone_greenlet.link_exception(self.on_exception)
            logger.info('Drone configured and running. ({0})'.format(self.id))

    def stop(self):
        """Stops services"""
        logging.debug('Stopping drone, hang on.')
        if self.drone is not None:
            self.drone_greenlet.unlink(self.on_exception)
            self.drone.stop()
            self.drone_greenlet.kill()
            self.drone = None
        # just some time for the drone to powerdown to be nice.
        gevent.sleep(2)
        if self.drone_greenlet is not None:
            self.drone_greenlet.kill(timeout=5)

    def on_exception(self, dead_greenlet):
        logger.error('Stopping because {0} died: {1}'.format(dead_greenlet, dead_greenlet.exception))
        self.stop()
        sys.exit(1)

    # command from server
    def incoming_server_comms(self, server_public, client_public, client_secret):
        context = beeswarm.shared.zmq_context
        # data (commands) received from server
        server_receiving_socket = context.socket(zmq.SUB)

        # setup receiving tcp socket
        server_receiving_socket.curve_secretkey = client_secret
        server_receiving_socket.curve_publickey = client_public
        server_receiving_socket.curve_serverkey = server_public
        server_receiving_socket.setsockopt(zmq.RECONNECT_IVL, 2000)
        # only subscribe to messages to this specific drone
        server_receiving_socket.setsockopt(zmq.SUBSCRIBE, str(self.id))

        # data from local socket
        local_receiving_socket = context.socket(zmq.PULL)
        if self.local_pull_socket:
            local_receiving_socket.bind('ipc://{0}'.format(self.local_pull_socket))

        logger.debug(
            'Trying to connect receiving socket to server on {0}'.format(
                self.config['beeswarm_server']['zmq_command_url']))

        outgoing_proxy = context.socket(zmq.PUSH)
        outgoing_proxy.connect(SocketNames.SERVER_RELAY.value)

        server_receiving_socket.connect(self.config['beeswarm_server']['zmq_command_url'])
        gevent.spawn(self.monitor_worker, server_receiving_socket.get_monitor_socket(), 'incomming socket ({0}).'
                     .format(self.config['beeswarm_server']['zmq_command_url']))

        poller = zmq.Poller()
        poller.register(server_receiving_socket, zmq.POLLIN)

        while True:
            # .recv() gives no context switch - why not? using poller with timeout instead
            socks = dict(poller.poll(1))
            # hmm, do we need to sleep here (0.1) works, gevnet.sleep() does not work
            gevent.sleep(0.1)

            if server_receiving_socket in socks and socks[server_receiving_socket] == zmq.POLLIN:
                message = server_receiving_socket.recv()
                # expected format for drone commands are:
                # DRONE_ID COMMAND OPTIONAL_DATA
                # DRONE_ID and COMMAND must not contain spaces
                drone_id, command, data = message.split(' ', 2)
                logger.debug('Received {0} command.'.format(command))
                assert (drone_id == str(self.id))
                # if we receive a configuration we restart the drone
                if command == Messages.CONFIG.value:
                    send_zmq_push(SocketNames.SERVER_RELAY.value, '{0}'.format(Messages.PING.value))
                    config = json.loads(data, object_hook=asciify)
                    if self.config != config or not self.config_received.isSet():
                        logger.debug('Setting config.')
                        self.config = config
                        with open(self.config_file, 'w') as local_config:
                            local_config.write(json.dumps(config, indent=4))
                        self.stop()
                        self._start_drone()
                        self.config_received.set()
                elif command == Messages.DRONE_DELETE.value:
                    self._handle_delete()
                else:
                    self.internal_server_relay.send('{0} {1}'.format(command, data))
            elif local_receiving_socket in socks and socks[local_receiving_socket] == zmq.POLLIN:
                data = local_receiving_socket.recv()
                outgoing_proxy.send('{0} {1}'.format(self.id, data))

        logger.warn('Command listener exiting.')

    def outgoing_server_comms(self, server_public, client_public, client_secret):
        context = beeswarm.shared.zmq_context
        sending_socket = context.socket(zmq.PUSH)

        # setup sending tcp socket
        sending_socket.curve_secretkey = client_secret
        sending_socket.curve_publickey = client_public
        sending_socket.curve_serverkey = server_public
        sending_socket.setsockopt(zmq.RECONNECT_IVL, 2000)
        logger.debug(
            'Trying to connect sending socket to server on {0}'.format(self.config['beeswarm_server']['zmq_url']))
        sending_socket.connect(self.config['beeswarm_server']['zmq_url'])
        gevent.spawn(self.monitor_worker, sending_socket.get_monitor_socket(), 'outgoing socket ({0}).'
                     .format(self.config['beeswarm_server']['zmq_url']))

        # retransmits everything received to beeswarm server using sending_socket
        internal_server_relay = context.socket(zmq.PULL)
        internal_server_relay.bind(SocketNames.SERVER_RELAY.value)

        poller = zmq.Poller()
        poller.register(internal_server_relay, zmq.POLLIN)

        while True:
            # .recv() gives no context switch - why not? using poller with timeout instead
            socks = dict(poller.poll(1))
            # hmm, do we need to sleep here (0.1) works, gevnet.sleep() does not work
            gevent.sleep(0.1)
            if internal_server_relay in socks and socks[internal_server_relay] == zmq.POLLIN:
                message = internal_server_relay.recv()
                # inject own id into the message
                data_split = message.split(' ', 1)
                if len(data_split) == 1:
                    topic = data_split[0]
                    new_message = '{0} {1}'.format(topic, self.id)
                else:
                    topic, data = data_split
                    # inject drone id into the message
                    new_message = '{0} {1} {2}'.format(topic, self.id, data)
                logger.debug('Relaying {0} message to server.'.format(topic))
                sending_socket.send(new_message)

        logger.warn('Command sender exiting.')

    def monitor_worker(self, monitor_socket, log_name):
        monitor_socket.linger = 0
        poller = zmq.Poller()
        poller.register(monitor_socket, zmq.POLLIN)
        while True:
            socks = poller.poll(1)
            gevent.sleep(0.1)
            if len(socks) > 0:
                data = recv_monitor_message(monitor_socket)
                event = data['event']
                if event == zmq.EVENT_CONNECTED:
                    logger.info('Connected to {0}'.format(log_name))
                    # always ask for config to avoid race condition.
                    send_zmq_push(SocketNames.SERVER_RELAY.value, '{0}'.format(Messages.DRONE_WANT_CONFIG.value))
                    if 'outgoing' in log_name:
                        send_zmq_push(SocketNames.SERVER_RELAY.value, '{0}'.format(Messages.PING.value))
                        own_ip = get_most_likely_ip()
                        send_zmq_push(SocketNames.SERVER_RELAY.value, '{0} {1}'.format(Messages.IP.value, own_ip))
                    elif 'incomming':
                        pass
                    else:
                        assert False
                elif event == zmq.EVENT_DISCONNECTED:
                    logger.warning('Disconnected from {0}, will reconnect in {1} seconds.'.format(log_name, 5))
            gevent.sleep()

    def _handle_delete(self):
        if self.drone:
            self.drone.stop()
            logger.warning('Drone has been deleted by the beeswarm server.')
        sys.exit(0)
