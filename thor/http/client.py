#!/usr/bin/env python

"""
Thor HTTP Client

This library allow implementation of an HTTP/1.1 client that is
"non-blocking," "asynchronous" and "event-driven" -- i.e., it achieves very
high performance and concurrency, so long as the application code does not
block (e.g., upon network, disk or database access). Blocking on one response
will block the entire client.

"""

__author__ = "Mark Nottingham <mnot@mnot.net>"
__copyright__ = """\
Copyright (c) 2005-2011 Mark Nottingham

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""

from urlparse import urlsplit, urlunsplit

import thor
from thor.events import EventEmitter, on
from thor.tcp import TcpClient

from common import HttpMessageHandler, \
    CLOSE, COUNTED, NOBODY, \
    WAITING, \
    idempotent_methods, no_body_status, hop_by_hop_hdrs, \
    get_header
from error import UrlError, ConnectError, \
    ReadTimeoutError, HttpVersionError

req_rm_hdrs = hop_by_hop_hdrs + ['host']

# TODO: proxy support
# TODO: next-hop version cache for Expect/Continue, etc.

class HttpClient(object):
    "An asynchronous HTTP client."

    tcp_client_class = TcpClient
    idle_timeout = 60 # in seconds
    connect_timeout = None
    read_timeout = None
    retry_limit = 2
    retry_delay = 500 # in ms

    def __init__(self, loop=None):
        self.loop = loop or thor.loop._loop
        self._conns = {}
        self.loop.on('stop', self._close_conns)

    def exchange(self):
        return HttpClientExchange(self)

    def _attach_conn(self, host, port, handle_connect,
               handle_connect_error, connect_timeout):
        "Find an idle connection for (host, port), or create a new one."
        while True:
            try:
                tcp_conn = self._conns[(host, port)].pop()
            except (IndexError, KeyError):
                tcp_client = self.tcp_client_class(self.loop)
                tcp_client.on('connect', handle_connect)
                tcp_client.on('connect_error', handle_connect_error)
                tcp_client.connect(host, port, connect_timeout)
                break
            if tcp_conn.tcp_connected:
                if hasattr(tcp_conn, "_idler"):
                    tcp_conn._idler.delete()
                handle_connect(tcp_conn)
                break

    def _release_conn(self, tcp_conn):
        "Add an idle connection back to the pool."
        tcp_conn.removeListeners('data', 'pause', 'close')
        tcp_conn.pause(True)
        if tcp_conn.tcp_connected:
            def idle_close():
                "Remove the connection from the pool when it closes."
                try:
                    if hasattr(tcp_conn, "_idler"):
                        tcp_conn._idler.delete()                    
                    self._conns[
                        (tcp_conn.host, tcp_conn.port)
                    ].remove(tcp_conn)
                except (KeyError, ValueError):
                    pass
            tcp_conn.on('close', idle_close)
            if self.idle_timeout:
                tcp_conn._idler = self.loop.schedule(
                    self.idle_timeout, tcp_conn.close
                )
            if not self._conns.has_key((tcp_conn.host, tcp_conn.port)):
                self._conns[(tcp_conn.host, tcp_conn.port)] = [tcp_conn]
            else:
                self._conns[(tcp_conn.host, tcp_conn.port)].append(tcp_conn)

    def _close_conns(self):
        "Close all idle HTTP connections."
        for conn_list in self._conns.values():
            for conn in conn_list:
                try:
                    conn.close()
                except:
                    pass
        self._conns = {}
        # TODO: probably need to close in-progress conns too.


class HttpClientExchange(HttpMessageHandler, EventEmitter):

    def __init__(self, client):
        HttpMessageHandler.__init__(self)
        EventEmitter.__init__(self)
        self.client = client
        self.method = None
        self.res_version = None
        self._host = None
        self._port = None
        self.tcp_conn = None
        self._conn_reusable = False
        self._retries = 0
        self._read_timeout_ev = None
        self._output_buffer = []

    def request_start(self, method, uri, req_hdrs):
        """
        Start a request to uri using method, where
        req_hdrs is a list of (field_name, field_value) for
        the request headers.
        """
        req_hdrs = [i for i in req_hdrs if not i[0].lower() in req_rm_hdrs]
        (scheme, authority, path, query, fragment) = urlsplit(uri)
        if scheme.lower() != 'http':
            self.input_error(UrlError("Only HTTP URLs are supported"))
            return
        if "@" in authority:
            userinfo, authority = authority.split("@", 1)
        if ":" in authority:
            self._host, port = authority.rsplit(":", 1)
            try:
                self._port = int(port)
            except ValueError:
                self.input_error(UrlError("Non-integer port in URL"))
                return
        else:
            self._host, self._port = authority, 80
        if path == "":
            path = "/"
        req_target = urlunsplit(('', '', path, query, ''))
        self.method = method
        req_hdrs.append(("Host", authority))
        req_hdrs.append(("Connection", "keep-alive"))
        try:
            body_len = int(get_header(req_hdrs, "content-length").pop(0))
            delimit = COUNTED
        except (IndexError, ValueError):
            body_len = None
            delimit = NOBODY
        # FIXME: chunked encoding
        self.output_start("%s %s HTTP/1.1" % (self.method, req_target),
            req_hdrs, delimit
        )
        self.client._attach_conn(self._host, self._port, self._handle_connect,
            self._handle_connect_error, self.client.connect_timeout
        )
    # TODO: if we sent Expect: 100-continue, don't wait forever
    # (i.e., schedule something)

    def request_body(self, chunk):
        "Send part of the request body. May be called zero to many times."
        self.output_body(chunk)

    def request_done(self, trailers):
        """
        Signal the end of the request, whether or not there was a body. MUST
        be called exactly once for each request.
        """
        self.output_end(trailers)

    def res_body_pause(self, paused):
        "Temporarily stop / restart sending the response body."
        if self.tcp_conn and self.tcp_conn.tcp_connected:
            self.tcp_conn.pause(paused)

    # Methods called by tcp

    def _handle_connect(self, tcp_conn):
        "The connection has succeeded."
        self.tcp_conn = tcp_conn
        self._set_read_timeout('connect')
        tcp_conn.on('data', self.handle_input)
        tcp_conn.on('close', self._conn_closed)
        tcp_conn.on('pause', self._req_body_pause)
        self.output("") # kick the output buffer
        tcp_conn.pause(False)

    def _handle_connect_error(self, err_type, err):
        "The connection has failed."
        self.input_error(ConnectError(err))

    def _conn_closed(self):
        "The server closed the connection."
        self._clear_read_timeout()
        if self._input_buffer:
            self.handle_input("")
        if self._input_delimit == CLOSE:
            self.input_end()
        elif self._input_state == WAITING:
            if self.method in idempotent_methods:
                if self._retries < self.client.retry_limit:
                    self.client.loop.schedule(
                        (self.client.retry_delay / 1000), self._retry
                    )
                else:
                    self.input_error(
                        ConnectError(
                            "Tried to connect %s times." % (self._retries + 1)
                        )
                    )
            else:
                self.input_error(
                    ConnectError("Can't retry %s method" % self.method)
                )
        else:
            self.input_error(ConnectError(
                "Server dropped connection before the response was complete."
            ))

    def _retry(self):
        "Retry the request."
        self._clear_read_timeout()
        self._retries += 1
        self.client._attach_conn(self._host, self._port, self._handle_connect,
            self._handle_connect_error, self.client.connect_timeout
        )

    def _req_body_pause(self, paused):
        "The client needs the application to pause/unpause the request body."
        self.emit('pause', paused)

    # Methods called by common.HttpMessageHandler

    def input_start(self, top_line, hdr_tuples, conn_tokens,
        transfer_codes, content_length):
        """
        Take the top set of headers from the input stream, parse them
        and queue the request to be processed by the application.
        """
        self._clear_read_timeout()
        try:
            proto_version, status_txt = top_line.split(None, 1)
            proto, self.res_version = proto_version.rsplit('/', 1)
        except (ValueError, IndexError):
            self.input_error(HttpVersionError(top_line))
            raise ValueError
        if proto != "HTTP" or self.res_version not in ["1.0", "1.1"]:
            self.input_error(HttpVersionError(proto_version))
            raise ValueError
        try:
            res_code, res_phrase = status_txt.split(None, 1)
        except ValueError:
            res_code = status_txt.rstrip()
            res_phrase = ""
        if 'close' not in conn_tokens:
            if (
              self.res_version == "1.0" and 'keep-alive' in conn_tokens) or \
              self.res_version in ["1.1"]:
                self._conn_reusable = True
        self._set_read_timeout('start')
        self.emit('response_start',
                  res_code,
                  res_phrase,
                  hdr_tuples
        )
        allows_body = (res_code not in no_body_status) \
            and (self.method != "HEAD")
        return allows_body

    def input_body(self, chunk):
        "Process a response body chunk from the wire."
        self._clear_read_timeout()
        self.emit('response_body', chunk)
        self._set_read_timeout('body')

    def input_end(self, trailers):
        "Indicate that the response body is complete."
        self._clear_read_timeout()
        if self.tcp_conn:
            if self.tcp_conn.tcp_connected and self._conn_reusable:
                self.client._release_conn(self.tcp_conn)
            else:
                self.tcp_conn.close()
            self.tcp_conn = None
        self.emit('response_done', trailers)

    def input_error(self, err):
        "Indicate an error state."
        if self.inspecting: # we want to get the rest of the response.
            self._conn_reusable = False
        else:
            self._clear_read_timeout()
            if self.tcp_conn:
                self.tcp_conn.close()
                self.tcp_conn = None
        self.emit('error', err)

    def output(self, chunk):
        self._output_buffer.append(chunk)
        if self.tcp_conn and self.tcp_conn.tcp_connected:
            self.tcp_conn.write("".join(self._output_buffer))
            self._output_buffer = []

    # misc

    def _set_read_timeout(self, kind):
        "Set the read timeout."
        if self.client.read_timeout:
            self._read_timeout_ev = self.client.loop.schedule(
                self.client.read_timeout, self.input_error,
                ReadTimeoutError(kind)
            )

    def _clear_read_timeout(self):
        "Clear the read timeout."
        if self.client.read_timeout:
            self._read_timeout_ev.delete()


def test_client(request_uri, out, err):
    "A simple demonstration of a client."
    from thor.loop import stop, run

    c = HttpClient()
    c.connect_timeout = 5
    x = c.exchange()

    @on(x)
    def response_start(status, phrase, headers):
        "Print the response headers."
        print "HTTP/%s %s %s" % (x.res_version, status, phrase)
        print "\n".join(["%s:%s" % header for header in headers])
        print

    @on(x)
    def error(err_msg):
        if err_msg:
            err("*** ERROR: %s (%s)\n" %
                (err_msg.desc, err_msg.detail)
            )
        stop()

    x.on('response_body', out)

    @on(x)
    def response_done(trailers):
        stop()

    x.request_start("GET", request_uri, [])
    x.request_done([])
    run()


if __name__ == "__main__":
    import sys
    test_client(sys.argv[1], sys.stdout.write, sys.stderr.write)
