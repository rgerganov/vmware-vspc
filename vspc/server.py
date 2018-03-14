#!/usr/bin/env python3
# Copyright (c) 2017 VMware Inc.
# All Rights Reserved.
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

import asyncio
import functools
import os
import ssl
import sys

from oslo_config import cfg
from oslo_log import log as logging

from vspc import async_telnet
from vspc.async_telnet import IAC, SB, SE, DO, DONT, WILL, WONT

opts = [
    cfg.StrOpt('host',
               default='0.0.0.0',
               help='Host on which to listen for incoming requests from VMs'),
    cfg.IntOpt('port',
               default=13370,
               help='Port on which to listen for incoming requests from VMs'),
    cfg.StrOpt('client_host',
               default='127.0.0.1',
               help='Host on which to listen for incoming requests from clients'),
    cfg.IntOpt('vm_start_port',
               default=20000,
               help='Start port for client connection listeners'),
    cfg.StrOpt('admin_host',
               default='127.0.0.1',
               help='Host on which to listen for admin requests'),
    cfg.IntOpt('admin_port',
               default=13371,
               help='Port on which to listen for admin requests'),
    cfg.BoolOpt('enable_clients',
               default=False,
               help='If enabled, accept client connections on "client_host" and '
                    'relay traffic between VMs and clients'),
    cfg.StrOpt('cert', help='SSL certificate file'),
    cfg.StrOpt('key', help='SSL key file (if separate from cert)'),
    cfg.StrOpt('uri', help='VSPC URI'),
    cfg.StrOpt('serial_log_dir', help='The directory where serial logs are '
                                      'saved'),
    ]

CONF = cfg.CONF
CONF.register_opts(opts)

LOG = logging.getLogger(__name__)

BINARY = bytes([0])  # 8-bit data path
SGA = bytes([3])  # suppress go ahead
VMWARE_EXT = bytes([232])

KNOWN_SUBOPTIONS_1 = bytes([0])
KNOWN_SUBOPTIONS_2 = bytes([1])
VMOTION_BEGIN = bytes([40])
VMOTION_GOAHEAD = bytes([41])
VMOTION_NOTNOW = bytes([43])
VMOTION_PEER = bytes([44])
VMOTION_PEER_OK = bytes([45])
VMOTION_COMPLETE = bytes([46])
VMOTION_ABORT = bytes([48])
VM_VC_UUID = bytes([80])
GET_VM_VC_UUID = bytes([81])
VM_NAME = bytes([82])
GET_VM_NAME = bytes([83])
DO_PROXY = bytes([70])
WILL_PROXY = bytes([71])
WONT_PROXY = bytes([73])

SUPPORTED_OPTS = (KNOWN_SUBOPTIONS_1 + KNOWN_SUBOPTIONS_2 + VMOTION_BEGIN +
                  VMOTION_GOAHEAD + VMOTION_NOTNOW + VMOTION_PEER +
                  VMOTION_PEER_OK + VMOTION_COMPLETE + VMOTION_ABORT +
                  VM_VC_UUID + GET_VM_VC_UUID + VM_NAME + GET_VM_NAME +
                  DO_PROXY + WILL_PROXY + WONT_PROXY)


class VspcServer(object):
    def __init__(self):
        self._uuid_to_vm_writer = dict()
        self._uuid_to_client_listener = dict()
        self._uuid_to_client_writers = dict()
        self._uuid_to_port = dict()
        self._loop = asyncio.get_event_loop()

    @asyncio.coroutine
    def handle_known_suboptions(self, writer, data):
        socket = writer.get_extra_info('socket')
        peer = socket.getpeername()
        LOG.debug("<< %s KNOWN-SUBOPTIONS-1 %s", peer, data)
        LOG.debug(">> %s KNOWN-SUBOPTIONS-2 %s", peer, SUPPORTED_OPTS)
        writer.write(IAC + SB + VMWARE_EXT + KNOWN_SUBOPTIONS_2 +
                     SUPPORTED_OPTS + IAC + SE)
        LOG.debug(">> %s GET-VM-VC-UUID", peer)
        writer.write(IAC + SB + VMWARE_EXT + GET_VM_VC_UUID + IAC + SE)
        yield from writer.drain()

    @asyncio.coroutine
    def handle_do_proxy(self, writer, data):
        socket = writer.get_extra_info('socket')
        peer = socket.getpeername()
        dir, uri = data[0], data[1:].decode('ascii')
        LOG.debug("<< %s DO-PROXY %c %s", peer, dir, uri)
        if chr(dir) != 'S' or uri != CONF.uri:
            LOG.debug(">> %s WONT-PROXY", peer)
            writer.write(IAC + SB + VMWARE_EXT + WONT_PROXY + IAC + SE)
            yield from writer.drain()
            writer.close()
        else:
            LOG.debug(">> %s WILL-PROXY", peer)
            writer.write(IAC + SB + VMWARE_EXT + WILL_PROXY + IAC + SE)
            yield from writer.drain()

    def handle_vm_vc_uuid(self, socket, data):
        peer = socket.getpeername()
        uuid = data.decode('ascii')
        LOG.debug("<< %s VM-VC-UUID %s", peer, uuid)
        uuid = uuid.replace(' ', '')
        uuid = uuid.replace('-', '')
        return uuid

    @asyncio.coroutine
    def handle_vmotion_begin(self, writer, data):
        socket = writer.get_extra_info('socket')
        peer = socket.getpeername()
        LOG.debug("<< %s VMOTION-BEGIN %s", peer, data)
        secret = os.urandom(4)
        LOG.debug(">> %s VMOTION-GOAHEAD %s %s", peer, data, secret)
        writer.write(IAC + SB + VMWARE_EXT + VMOTION_GOAHEAD +
                     data + secret + IAC + SE)
        yield from writer.drain()

    @asyncio.coroutine
    def handle_vmotion_peer(self, writer, data):
        socket = writer.get_extra_info('socket')
        peer = socket.getpeername()
        LOG.debug("<< %s VMOTION-PEER %s", peer, data)
        LOG.debug("<< %s VMOTION-PEER-OK %s", peer, data)
        writer.write(IAC + SB + VMWARE_EXT + VMOTION_PEER_OK + data + IAC + SE)
        yield from writer.drain()

    def handle_vmotion_complete(self, socket, data):
        peer = socket.getpeername()
        LOG.debug("<< %s VMOTION-COMPLETE %s", peer, data)

    @asyncio.coroutine
    def handle_do(self, writer, opt):
        socket = writer.get_extra_info('socket')
        peer = socket.getpeername()
        LOG.debug("<< %s DO %s", peer, opt)
        if opt in (BINARY, SGA):
            LOG.debug(">> %s WILL", peer)
            writer.write(IAC + WILL + opt)
            yield from writer.drain()
        else:
            LOG.debug(">> %s WONT", peer)
            writer.write(IAC + WONT + opt)
            yield from writer.drain()

    @asyncio.coroutine
    def handle_will(self, writer, opt):
        socket = writer.get_extra_info('socket')
        peer = socket.getpeername()
        LOG.debug("<< %s WILL %s", peer, opt)
        if opt in (BINARY, SGA, VMWARE_EXT):
            LOG.debug(">> %s DO", peer)
            writer.write(IAC + DO + opt)
            yield from writer.drain()
        else:
            LOG.debug(">> %s DONT", peer)
            writer.write(IAC + DONT + opt)
            yield from writer.drain()

    @asyncio.coroutine
    def option_handler(self, cmd, opt, writer, data=None, vm_uuid_rcvd=None):
        socket = writer.get_extra_info('socket')
        if cmd == SE and data[0:1] == VMWARE_EXT:
            vmw_cmd = data[1:2]
            if vmw_cmd == KNOWN_SUBOPTIONS_1:
                yield from self.handle_known_suboptions(writer, data[2:])
            elif vmw_cmd == DO_PROXY:
                yield from self.handle_do_proxy(writer, data[2:])
            elif vmw_cmd == VM_VC_UUID:
                vm_uuid = self.handle_vm_vc_uuid(socket, data[2:])
                vm_uuid_rcvd.set_result(vm_uuid)
            elif vmw_cmd == VMOTION_BEGIN:
                yield from self.handle_vmotion_begin(writer, data[2:])
            elif vmw_cmd == VMOTION_PEER:
                yield from self.handle_vmotion_peer(writer, data[2:])
            elif vmw_cmd == VMOTION_COMPLETE:
                self.handle_vmotion_complete(socket, data[2:])
            else:
                LOG.error("Unknown VMware cmd: %s %s", vmw_cmd, data[2:])
                writer.close()
        elif cmd == DO:
            yield from self.handle_do(writer, opt)
        elif cmd == WILL:
            yield from self.handle_will(writer, opt)

    def save_to_log(self, uuid, data):
        fpath = os.path.join(CONF.serial_log_dir, uuid)
        with open(fpath, 'ab') as f:
            f.write(data)

    @asyncio.coroutine
    def handle_client(self, reader, writer, uuid):
        LOG.info("Client connected for VM with UUID='%s'", uuid)
        client_writers = self._uuid_to_client_writers.get(uuid, [])
        client_writers.append(writer)
        self._uuid_to_client_writers[uuid] = client_writers
        data = yield from reader.read(1024)
        try:
            while data:
                vm_writer = self._uuid_to_vm_writer.get(uuid)
                if not vm_writer:
                    break
                vm_writer.write(data)
                yield from vm_writer.drain()
                data = yield from reader.read(1024)
        finally:
            client_writers = self._uuid_to_client_writers.get(uuid)
            if client_writers:
                client_writers.remove(writer)
        LOG.info("Client disconnected for VM with UUID='%s'", uuid)
        writer.close()

    def _find_port(self):
        for port in range(CONF.vm_start_port, 65535):
            if port not in self._uuid_to_port.values():
                return port
        raise Exception("Unable to find free port")

    @asyncio.coroutine
    def _start_client_listener(self, uuid):
        port = self._find_port()
        client_handler = functools.partial(self.handle_client, uuid=uuid)
        self._uuid_to_port[uuid] = port
        try:
            coro = asyncio.start_server(client_handler, CONF.client_host, port, loop=self._loop)
            client_listener = yield from asyncio.wait_for(coro, None)
            self._uuid_to_client_listener[uuid] = client_listener
        except Exception:
            LOG.error("Unable to start client listener on port %d for VM with UUID='%s'", port, uuid)
            del self._uuid_to_vm_writer[uuid]
            del self._uuid_to_port[uuid]
            raise
        LOG.info("Started client listener on port %d for VM with UUID='%s'", port, uuid)

    @asyncio.coroutine
    def _stop_client_listener(self, uuid):
        port = self._uuid_to_port.pop(uuid)
        LOG.info("Stopping client listener on port %d for VM with UUID='%s'", port, uuid)
        client_listener = self._uuid_to_client_listener.pop(uuid)
        client_listener.close()
        yield from asyncio.wait_for(client_listener.wait_closed(), None)
        client_writers = self._uuid_to_client_writers.pop(uuid, [])
        for client_writer in client_writers:
            client_writer.close()

    @asyncio.coroutine
    def _dispatch_to_client_writers(self, data, uuid):
        client_writers = self._uuid_to_client_writers.get(uuid, [])
        for client_writer in client_writers:
            client_writer.write(data)
            yield from client_writer.drain()

    @asyncio.coroutine
    def handle_telnet(self, reader, writer):
        vm_uuid_rcvd = asyncio.Future()
        opt_handler = functools.partial(self.option_handler, writer=writer,
                                        vm_uuid_rcvd=vm_uuid_rcvd)
        telnet = async_telnet.AsyncTelnet(reader, opt_handler)
        socket = writer.get_extra_info('socket')
        peer = socket.getpeername()
        LOG.info("%s connected", peer)

        read_task = self._loop.create_task(telnet.read_some())
        try:
            uuid = yield from asyncio.wait_for(vm_uuid_rcvd, 2)
        except asyncio.TimeoutError:
            LOG.error("%s didn't present UUID", peer)
            writer.close()
            return

        self._uuid_to_vm_writer[uuid] = writer
        if CONF.enable_clients:
            yield from self._start_client_listener(uuid)
        data = yield from asyncio.wait_for(read_task, None)
        try:
            while data:
                self.save_to_log(uuid, data)
                if CONF.enable_clients:
                    self._dispatch_to_client_writers(data, uuid)
                data = yield from telnet.read_some()
        finally:
            del self._uuid_to_vm_writer[uuid]
            if CONF.enable_clients:
                yield from self._stop_client_listener(uuid)
        LOG.info("%s disconnected", peer)
        writer.close()

    @asyncio.coroutine
    def handle_admin(self, reader, writer):
        line_b = yield from reader.readline()
        line = line_b.decode('ascii').strip()
        if line == 'LIST':
            for uuid, port in self._uuid_to_port.items():
                ans = "%s -> %s:%d\n" % (uuid, CONF.client_host, port)
                writer.write(ans.encode('ascii'))
            yield from writer.drain()
            writer.close()
            return
        parts = line.split()
        if parts[0] == 'GET':
            vm_uuid = parts[1]
            port = self._uuid_to_port.get(vm_uuid)
            if port:
                ans = "%s:%d\n" % (CONF.client_host, port)
                writer.write(ans.encode('ascii'))
                yield from writer.drain()
            else:
                writer.write("None\n".encode('ascii'))
                yield from writer.drain()
        writer.close()

    def start(self):
        ssl_context = None
        if CONF.cert:
            ssl_context = ssl.SSLContext(ssl.PROTOCOL_SSLv23)
            ssl_context.load_cert_chain(certfile=CONF.cert, keyfile=CONF.key)
        coro = asyncio.start_server(self.handle_telnet,
                                    CONF.host,
                                    CONF.port,
                                    ssl=ssl_context,
                                    loop=self._loop)
        server = self._loop.run_until_complete(coro)

        if CONF.enable_clients:
            coro = asyncio.start_server(self.handle_admin,
                                        CONF.admin_host,
                                        CONF.admin_port,
                                        ssl=ssl_context,
                                        loop=self._loop)
            admin_server = self._loop.run_until_complete(coro)

        # Serve requests until Ctrl+C is pressed
        LOG.info("Serving on %s", server.sockets[0].getsockname())
        LOG.info("Log directory: %s", CONF.serial_log_dir)
        try:
            self._loop.run_forever()
        except KeyboardInterrupt:
            pass

        # Close the server
        server.close()
        self._loop.run_until_complete(server.wait_closed())

        if CONF.enable_clients:
            admin_server.close()
            self._loop.run_until_complete(admin_server.wait_closed())

        for vm_writer in self._uuid_to_vm_writer.values():
            vm_writer.close()

        for task in asyncio.Task.all_tasks():
            #task.print_stack()
            self._loop.run_until_complete(task)

        self._loop.close()


def main():
    logging.register_options(CONF)
    CONF(sys.argv[1:], prog='vspc')
    logging.setup(CONF, "vspc")
    if not CONF.serial_log_dir:
        LOG.error("serial_log_dir is not specified")
        sys.exit(1)
    if not os.path.exists(CONF.serial_log_dir):
        LOG.info("Creating log directory: %s", CONF.serial_log_dir)
        os.makedirs(CONF.serial_log_dir)
    srv = VspcServer()
    srv.start()


if __name__ == '__main__':
    main()
