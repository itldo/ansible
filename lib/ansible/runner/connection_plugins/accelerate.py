# (c) 2012, Michael DeHaan <michael.dehaan@gmail.com>
#
# This file is part of Ansible
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.

import json
import os
import base64
import socket
import struct
import time
from ansible.callbacks import vvv, vvvv
from ansible.runner.connection_plugins.ssh import Connection as SSHConnection
from ansible.runner.connection_plugins.paramiko_ssh import Connection as ParamikoConnection
from ansible import utils
from ansible import errors
from ansible import constants

# the chunk size to read and send, assuming mtu 1500 and
# leaving room for base64 (+33%) encoding and header (8 bytes)
# ((1400-8)/4)*3) = 1044
# which leaves room for the TCP/IP header. We set this to a 
# multiple of the value to speed up file reads.
CHUNK_SIZE=1044*20

class Connection(object):
    ''' raw socket accelerated connection '''

    def __init__(self, runner, host, port, user, password, private_key_file, *args, **kwargs):

        self.runner = runner
        self.host = host
        self.context = None
        self.conn = None
        self.user = user
        self.key = utils.key_for_hostname(host)
        self.port = port[0]
        self.accport = port[1]
        self.is_connected = False
        self.has_pipelining = False

        if not self.port:
            self.port = constants.DEFAULT_REMOTE_PORT
        elif not isinstance(self.port, int):
            self.port = int(self.port)

        if not self.accport:
            self.accport = constants.ACCELERATE_PORT
        elif not isinstance(self.accport, int):
            self.accport = int(self.accport)

        if self.runner.original_transport == "paramiko":
            self.ssh = ParamikoConnection(
                runner=self.runner,
                host=self.host,
                port=self.port,
                user=self.user,
                password=password,
                private_key_file=private_key_file
            )
        else:
            self.ssh = SSHConnection(
                runner=self.runner,
                host=self.host,
                port=self.port,
                user=self.user,
                password=password,
                private_key_file=private_key_file
            )

        # attempt to work around shared-memory funness
        if getattr(self.runner, 'aes_keys', None):
            utils.AES_KEYS = self.runner.aes_keys

    def _execute_accelerate_module(self):
        args = "password=%s port=%s debug=%d ipv6=%s" % (base64.b64encode(self.key.__str__()), str(self.accport), int(utils.VERBOSITY), self.runner.accelerate_ipv6)
        inject = dict(password=self.key)
        if getattr(self.runner, 'accelerate_inventory_host', False):
            inject = utils.combine_vars(inject, self.runner.inventory.get_variables(self.runner.accelerate_inventory_host))
        else:
            inject = utils.combine_vars(inject, self.runner.inventory.get_variables(self.host))
        vvvv("attempting to start up the accelerate daemon...")
        self.ssh.connect()
        tmp_path = self.runner._make_tmp_path(self.ssh)
        return self.runner._execute_module(self.ssh, tmp_path, 'accelerate', args, inject=inject)

    def connect(self, allow_ssh=True):
        ''' activates the connection object '''

        try:
            if not self.is_connected:
                tries = 3
                self.conn = socket.socket()
                self.conn.settimeout(constants.ACCELERATE_CONNECT_TIMEOUT)
                vvvv("attempting connection to %s via the accelerated port %d" % (self.host,self.accport))
                while tries > 0:
                    try:
                        self.conn.connect((self.host,self.accport))
                        break
                    except:
                        vvvv("failed, retrying...")
                        time.sleep(0.1)
                        tries -= 1
                if tries == 0:
                    vvv("Could not connect via the accelerated connection, exceeded # of tries")
                    raise errors.AnsibleError("Failed to connect")
                self.conn.settimeout(constants.ACCELERATE_TIMEOUT)
        except:
            if allow_ssh:
                vvv("Falling back to ssh to startup accelerated mode")
                res = self._execute_accelerate_module()
                if not res.is_successful():
                    raise errors.AnsibleError("Failed to launch the accelerated daemon on %s (reason: %s)" % (self.host,res.result.get('msg')))
                return self.connect(allow_ssh=False)
            else:
                raise errors.AnsibleError("Failed to connect to %s:%s" % (self.host,self.accport))
        self.is_connected = True
        return self

    def send_data(self, data):
        packed_len = struct.pack('!Q',len(data))
        return self.conn.sendall(packed_len + data)

    def recv_data(self):
        header_len = 8 # size of a packed unsigned long long
        data = b""
        try:
            vvvv("%s: in recv_data(), waiting for the header" % self.host)
            while len(data) < header_len:
                d = self.conn.recv(header_len - len(data))
                if not d:
                    vvvv("%s: received nothing, bailing out" % self.host)
                    return None
                data += d
            vvvv("%s: got the header, unpacking" % self.host)
            data_len = struct.unpack('!Q',data[:header_len])[0]
            data = data[header_len:]
            vvvv("%s: data received so far (expecting %d): %d" % (self.host,data_len,len(data)))
            while len(data) < data_len:
                d = self.conn.recv(data_len - len(data))
                if not d:
                    vvvv("%s: received nothing, bailing out" % self.host)
                    return None
                data += d
            vvvv("%s: received all of the data, returning" % self.host)
            return data
        except socket.timeout:
            raise errors.AnsibleError("timed out while waiting to receive data")

    def exec_command(self, cmd, tmp_path, sudo_user=None, sudoable=False, executable='/bin/sh', in_data=None, su=None, su_user=None):
        ''' run a command on the remote host '''

        if su or su_user:
            raise errors.AnsibleError("Internal Error: this module does not support running commands via su")

        if in_data:
            raise errors.AnsibleError("Internal Error: this module does not support optimized module pipelining")

        if executable == "":
            executable = constants.DEFAULT_EXECUTABLE

        if self.runner.sudo and sudoable and sudo_user:
            cmd, prompt, success_key = utils.make_sudo_cmd(sudo_user, executable, cmd)

        vvv("EXEC COMMAND %s" % cmd)

        data = dict(
            mode='command',
            cmd=cmd,
            tmp_path=tmp_path,
            executable=executable,
        )
        data = utils.jsonify(data)
        data = utils.encrypt(self.key, data)
        if self.send_data(data):
            raise errors.AnsibleError("Failed to send command to %s" % self.host)
        
        while True:
            # we loop here while waiting for the response, because a 
            # long running command may cause us to receive keepalive packets
            # ({"pong":"true"}) rather than the response we want. 
            response = self.recv_data()
            if not response:
                raise errors.AnsibleError("Failed to get a response from %s" % self.host)
            response = utils.decrypt(self.key, response)
            response = utils.parse_json(response)
            if "pong" in response:
                # it's a keepalive, go back to waiting
                vvvv("%s: received a keepalive packet" % self.host)
                continue
            else:
                vvvv("%s: received the response" % self.host)
                break

        return (response.get('rc',None), '', response.get('stdout',''), response.get('stderr',''))

    def put_file(self, in_path, out_path):

        ''' transfer a file from local to remote '''
        vvv("PUT %s TO %s" % (in_path, out_path), host=self.host)

        if not os.path.exists(in_path):
            raise errors.AnsibleFileNotFound("file or module does not exist: %s" % in_path)

        fd = file(in_path, 'rb')
        fstat = os.stat(in_path)
        try:
            vvv("PUT file is %d bytes" % fstat.st_size)
            last = False
            while fd.tell() <= fstat.st_size and not last:
                vvvv("file position currently %ld, file size is %ld" % (fd.tell(), fstat.st_size))
                data = fd.read(CHUNK_SIZE)
                if fd.tell() >= fstat.st_size:
                    last = True
                data = dict(mode='put', data=base64.b64encode(data), out_path=out_path, last=last)
                if self.runner.sudo:
                    data['user'] = self.runner.sudo_user
                data = utils.jsonify(data)
                data = utils.encrypt(self.key, data)

                if self.send_data(data):
                    raise errors.AnsibleError("failed to send the file to %s" % self.host)

                response = self.recv_data()
                if not response:
                    raise errors.AnsibleError("Failed to get a response from %s" % self.host)
                response = utils.decrypt(self.key, response)
                response = utils.parse_json(response)

                if response.get('failed',False):
                    raise errors.AnsibleError("failed to put the file in the requested location")
        finally:
            fd.close()
            vvvv("waiting for final response after PUT")
            response = self.recv_data()
            if not response:
                raise errors.AnsibleError("Failed to get a response from %s" % self.host)
            response = utils.decrypt(self.key, response)
            response = utils.parse_json(response)

            if response.get('failed',False):
                raise errors.AnsibleError("failed to put the file in the requested location")

    def fetch_file(self, in_path, out_path):
        ''' save a remote file to the specified path '''
        vvv("FETCH %s TO %s" % (in_path, out_path), host=self.host)

        data = dict(mode='fetch', in_path=in_path)
        data = utils.jsonify(data)
        data = utils.encrypt(self.key, data)
        if self.send_data(data):
            raise errors.AnsibleError("failed to initiate the file fetch with %s" % self.host)

        fh = open(out_path, "w")
        try:
            bytes = 0
            while True:
                response = self.recv_data()
                if not response:
                    raise errors.AnsibleError("Failed to get a response from %s" % self.host)
                response = utils.decrypt(self.key, response)
                response = utils.parse_json(response)
                if response.get('failed', False):
                    raise errors.AnsibleError("Error during file fetch, aborting")
                out = base64.b64decode(response['data'])
                fh.write(out)
                bytes += len(out)
                # send an empty response back to signify we 
                # received the last chunk without errors
                data = utils.jsonify(dict())
                data = utils.encrypt(self.key, data)
                if self.send_data(data):
                    raise errors.AnsibleError("failed to send ack during file fetch")
                if response.get('last', False):
                    break
        finally:
            # we don't currently care about this final response,
            # we just receive it and drop it. It may be used at some
            # point in the future or we may just have the put/fetch
            # operations not send back a final response at all
            response = self.recv_data()
            vvv("FETCH wrote %d bytes to %s" % (bytes, out_path))
            fh.close()

    def close(self):
        ''' terminate the connection '''
        # Be a good citizen
        try:
            self.conn.close()
        except:
            pass

